"""Lightweight RSS monitor: desktop alerts and local Markdown archives."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

from rss_core import FetchResult, Store, fetch_feed, safe_url, utc_now
from rss_notifications import availability_error, deliver_pending, send_notification
from rss_naming import NamingConfig, NamingWorker
from rss_feeds import export_feed_index, load_feeds
from rss_report import atomic_write, export_reports
from rss_version import __version__


ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("rss_update")


@dataclass(frozen=True)
class Config:
    path: Path
    feed_url: str
    interval: int
    timeout: int
    data_dir: Path
    notifications: bool
    naming: NamingConfig = NamingConfig()
    feeds_file: Path | None = None


def load_config(path):
    path = Path(path).resolve()
    values = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(values, dict):
        raise ValueError("Config must be a JSON object")
    allowed = {"feed_url", "feeds_file", "poll_interval_seconds", "request_timeout_seconds", "data_dir", "notifications", "naming"}
    if values.keys() - allowed:
        raise ValueError(f"Unknown config fields: {sorted(values.keys() - allowed)}")
    url = values.get("feed_url", "")
    registry = values.get("feeds_file")
    if registry is not None and (not isinstance(registry, str) or not registry.strip() or url):
        raise ValueError("Use either feeds_file or feed_url, not both")
    if registry is None and (not isinstance(url, str) or not safe_url(url)):
        raise ValueError("feed_url must be an HTTP(S) URL")
    interval = values.get("poll_interval_seconds", 120)
    timeout = values.get("request_timeout_seconds", 20)
    if type(interval) is not int or not 30 <= interval <= 86400:
        raise ValueError("poll_interval_seconds must be an integer from 30 to 86400")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("request_timeout_seconds must be an integer from 1 to 60")
    notifications = values.get("notifications", True)
    if type(notifications) is not bool:
        raise ValueError("notifications must be true or false")
    directory = values.get("data_dir", "data")
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("data_dir must be a nonempty path")
    data_dir = (path.parent / directory).resolve()
    naming = values.get("naming", {})
    if not isinstance(naming, dict) or naming.keys() - {"enabled", "timeout_seconds", "command", "wsl_distribution"}:
        raise ValueError("Invalid naming configuration")
    naming_enabled = naming.get("enabled", False)
    naming_timeout = naming.get("timeout_seconds", 90)
    naming_command = naming.get("command", [])
    distro = naming.get("wsl_distribution", "")
    if type(naming_enabled) is not bool or type(naming_timeout) is not int or not 10 <= naming_timeout <= 300:
        raise ValueError("naming.enabled must be boolean; timeout_seconds must be 10–300")
    if not isinstance(naming_command, list) or any(not isinstance(part, str) or not part for part in naming_command):
        raise ValueError("naming.command must be an argument array")
    if not isinstance(distro, str):
        raise ValueError("naming.wsl_distribution must be a string")
    return Config(path, safe_url(url), interval, timeout, data_dir, notifications,
                  NamingConfig(naming_enabled, naming_timeout, tuple(naming_command), distro),
                  (path.parent / registry).resolve() if registry else None)


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def instance_lock(data_dir, lock_name="monitor.lock"):
    runtime = data_dir / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    # Never delete this file: replacing a live lock inode defeats mutual exclusion.
    with (runtime / lock_name).open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise AlreadyRunning("A monitor or one-off check already owns this data directory") from error
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def is_running(config):
    try:
        with instance_lock(config.data_dir):
            return False
    except AlreadyRunning:
        return True


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def configure_logging(data_dir, quiet=False):
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for handler in LOGGER.handlers[:]:
        handler.close()
        LOGGER.removeHandler(handler)
    handlers = [RotatingFileHandler(log_dir / "monitor.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")]
    if not quiet and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)


def check_once(config, store, feed_file=None):
    ok = False
    previous_error = store.get("last_error")
    try:
        if feed_file:
            result = FetchResult(Path(feed_file).read_bytes())
        else:
            # Older databases have no channel timestamp baseline; get a full snapshot once.
            tracking = store.get("timestamp_tracking")
            result = fetch_feed(config.feed_url, config.timeout,
                                store.get("etag") if tracking else "", store.get("last_modified") if tracking else "")
        baseline = not store.get("initialized")
        changes = store.record_success(result)
        if previous_error:
            LOGGER.info("RSS fetch recovered")
        if changes or baseline:
            LOGGER.info("%s — %s: %s records saved", config.feed_url, "Initial snapshot (silent)" if baseline else "RSS changes", changes)
        ok = True
    except (OSError, ValueError, ET.ParseError) as error:
        store.record_error(error)
        if str(error)[:600] != previous_error:
            LOGGER.warning("RSS fetch failed for %s: %s", config.feed_url, error)
    previous_notification_error = store.get("notification_error")
    sent = deliver_pending(store, config.notifications)
    notification_error = store.get("notification_error")
    if notification_error and notification_error != previous_notification_error:
        LOGGER.warning("Notification failed; pending changes retained: %s", notification_error)
    elif previous_notification_error and not notification_error:
        LOGGER.info("Desktop notification delivery recovered")
    if sent:
        LOGGER.info("Submitted one desktop notification for %s revisions", sent)
    try:
        export_reports(store, config.data_dir)
        with store.db:
            store.set("export_error", "")
    except OSError as error:
        with store.db:
            store.set("export_error", str(error))
        LOGGER.error("Readable archive export failed; database retained: %s", error)
        ok = False
    return ok


def sync_sources(feeds, sources):
    desired = {spec.id: spec for spec in feeds if spec.enabled}
    for key in list(sources):
        if key not in desired or sources[key]["spec"] != desired[key]:
            sources[key]["worker"].close()
            sources[key]["store"].close()
            del sources[key]
    errors = {}
    for key, spec in desired.items():
        if key in sources:
            continue
        try:
            store = Store(spec.config.data_dir, spec.config.feed_url)
            with store.db:
                store.set("label", spec.name)
                store.set("notifications_enabled", str(spec.config.notifications).lower())
                store.set("naming_enabled", str(spec.config.naming.enabled).lower())
                if not spec.config.naming.enabled:
                    store.set("naming_error", "")
                warning = availability_error() if spec.config.notifications else ""
                if warning or not spec.config.notifications or not store.pending():
                    store.set("notification_error", warning)
            if warning:
                LOGGER.warning("%s desktop notifications: %s", key, warning)
            sources[key] = {"spec": spec, "store": store, "worker": NamingWorker(spec.config.naming), "next_poll": 0, "failures": 0}
        except (OSError, ValueError, sqlite3.Error) as error:
            errors[key] = str(error)
    return errors


def run_monitor(config, once=False, feed_file=None, quiet=False, source_id=None):
    with instance_lock(config.data_dir):
        configure_logging(config.data_dir, quiet)
        runtime = config.data_dir / ".runtime"
        token = uuid4().hex
        metadata = {"instance_id": token, "pid": os.getpid(), "started_at": utc_now(),
                    "platform": sys.platform, "mode": "once" if once else "run"}
        atomic_write(runtime / "process.json", json.dumps(metadata, ensure_ascii=False))
        sources = {}
        stopped = threading.Event()
        old_signals = {}
        try:
            feeds = load_feeds(config)
            if source_id:
                feeds = [spec for spec in feeds if spec.id == source_id and spec.enabled]
                if not feeds:
                    raise ValueError(f"Enabled feed not found: {source_id}")
            if feed_file and len([spec for spec in feeds if spec.enabled]) != 1:
                raise ValueError("Use --source ID when importing a snapshot with multiple feeds")
            metadata["feed_errors"] = sync_sources(feeds, sources)
            metadata["active_feeds"] = list(sources)
            for signum in (signal.SIGINT, signal.SIGTERM):
                old_signals[signum] = signal.signal(signum, lambda *_: stopped.set())
            metadata["ready"] = True
            atomic_write(runtime / "process.json", json.dumps(metadata, ensure_ascii=False))
            LOGGER.info("Monitoring %s feeds; archive: %s", len(sources), config.data_dir / "index.md")
            next_reload = time.monotonic() + 5
            while not stopped.is_set():
                if read_json(runtime / "stop.json").get("instance_id") == token:
                    break
                if not once and time.monotonic() >= next_reload:
                    try:
                        updated = load_feeds(config)
                        errors = sync_sources(updated, sources)
                        if updated != feeds:
                            LOGGER.info("Reloaded subscription list: %s enabled feeds", len(sources))
                        feeds = updated
                        metadata["registry_error"] = ""
                        metadata["feed_errors"] = errors
                        metadata["active_feeds"] = list(sources)
                    except (OSError, ValueError) as error:
                        if str(error) != metadata.get("registry_error"):
                            LOGGER.warning("Keeping last valid subscription list: %s", error)
                        metadata["registry_error"] = str(error)
                    atomic_write(runtime / "process.json", json.dumps(metadata, ensure_ascii=False))
                    next_reload = time.monotonic() + 5
                all_ok = not metadata.get("feed_errors")
                for key, source in sources.items():
                    if stopped.is_set() or read_json(runtime / "stop.json").get("instance_id") == token:
                        stopped.set()
                        break
                    if time.monotonic() >= source["next_poll"]:
                        cfg = source["spec"].config
                        ok = check_once(cfg, source["store"], feed_file)
                        all_ok = all_ok and ok
                        source["failures"] = 0 if ok else min(source["failures"] + 1, 5)
                        delay = cfg.interval if ok else max(cfg.interval, min(cfg.interval * 2 ** source["failures"], 1800))
                        source["next_poll"] = time.monotonic() + delay
                export_feed_index(config, feeds, metadata.get("registry_error", "") or str(metadata.get("feed_errors") or ""))
                if once:
                    return 0 if all_ok else 1
                # Serial model calls across all feeds; HTTP polling never waits for a model result.
                for source in sources.values():
                    worker = source["worker"]
                    if worker.busy or not any(other["worker"].busy for other in sources.values()):
                        try:
                            if worker.tick(source["store"]):
                                export_reports(source["store"], source["spec"].config.data_dir)
                        except OSError as error:
                            LOGGER.error("Archive rename failed; retry on next check: %s", error)
                stopped.wait(0.5)
            LOGGER.info("Monitor stopped")
            return 0
        finally:
            for source in sources.values():
                source["worker"].close()
                source["store"].close()
            for signum, previous in old_signals.items():
                signal.signal(signum, previous)
            # Metadata is diagnostic only; the OS-held lock determines liveness.
            metadata["stopped_at"] = utc_now()
            atomic_write(runtime / "process.json", json.dumps(metadata, ensure_ascii=False))


def say(message):
    if sys.stdout is not None:
        print(message)


def start_monitor(config, announce=True):
    if is_running(config):
        if announce:
            say("RSS Update is already running for this data directory.")
        return 0
    runtime = config.data_dir / ".runtime"
    command = [sys.executable, str(ROOT / "rss_update.py"), "--config", str(config.path), "run", "--quiet"]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
    with (runtime / "startup.log").open("ab") as startup_log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=startup_log, cwd=ROOT, **options)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        metadata = read_json(runtime / "process.json")
        if is_running(config) and metadata.get("ready") and not metadata.get("stopped_at"):
            if announce:
                say(f"RSS Update started. Archive: {config.data_dir / 'index.md'}")
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.1)
    raise RuntimeError(f"Background startup not confirmed; check {runtime / 'startup.log'} and data/logs/monitor.log")


def stop_monitor(config):
    if not is_running(config):
        say("RSS Update is not running.")
        return 0
    runtime = config.data_dir / ".runtime"
    metadata = read_json(runtime / "process.json")
    if not metadata.get("instance_id") or metadata.get("stopped_at"):
        raise RuntimeError("The monitor is starting; retry stop in a moment")
    atomic_write(runtime / "stop.json", json.dumps({"instance_id": metadata["instance_id"]}))
    say("Stop requested. The monitor will exit after the current fetch/notification completes.")
    return 0


def source_status(config):
    result = {"archive": str(config.data_dir / "index.md")}
    database = config.data_dir / "state.sqlite3"
    if database.exists():
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            result["state"] = dict(connection.execute("SELECT key,value FROM metadata WHERE key NOT IN ('etag','last_modified')"))
            result["pending_notifications"] = connection.execute("SELECT COUNT(*) FROM revisions WHERE notification='pending'").fetchone()[0]
        finally:
            connection.close()
    return result


def status_monitor(config):
    running = is_running(config)
    metadata = read_json(config.data_dir / ".runtime" / "process.json")
    result = {"running": running, "process": metadata if running else None, "archive": str(config.data_dir / "index.md")}
    try:
        result["feeds"] = [dict(id=spec.id, name=spec.name, enabled=spec.enabled, **source_status(spec.config)) for spec in load_feeds(config)]
    except (OSError, ValueError) as error:
        result["registry_error"] = str(error)
    say(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def rename_archives(config):
    if not config.naming.enabled:
        raise ValueError("Enable naming in the config before running rename")
    with instance_lock(config.data_dir):
        configure_logging(config.data_dir)
        feeds = load_feeds(config)
        success = True
        for spec in feeds:
            if not spec.enabled:
                continue
            store = Store(spec.config.data_dir, spec.config.feed_url)
            worker = NamingWorker(spec.config.naming)
            try:
                worker.tick(store)
                while worker.busy:
                    time.sleep(0.2)
                    if worker.tick(store):
                        export_reports(store, spec.config.data_dir)
                export_reports(store, spec.config.data_dir)
                error = store.get("naming_error")
                say(f"{spec.id}: original filenames retained: {error}" if error else f"{spec.id}: archive names updated.")
                success = success and not error
            finally:
                worker.close()
                store.close()
        export_feed_index(config, feeds)
        return 0 if success else 1


def list_feeds(config):
    say(json.dumps([{"id": spec.id, "name": spec.name, "url": spec.config.feed_url,
                     "enabled": spec.enabled, "directory": str(spec.config.data_dir)}
                    for spec in load_feeds(config)], ensure_ascii=False, indent=2))
    return 0


def configure_path(remove=False):
    from rss_path import register_user_path
    changed, warning = register_user_path(ROOT, remove)
    if remove:
        say("Removed this RSS directory from the user PATH." if changed else "This RSS directory is not in the user PATH.")
    else:
        say("Added this RSS directory to the user PATH." if changed else "This RSS directory is already in the user PATH.")
        say("Commands: rss, rss -tui, rss tui, rss-tui, rss status, rss stop")
    say("Close and reopen Windows Terminal / PowerShell / CMD to refresh PATH.")
    if warning:
        say(f"PATH was saved, but Windows did not acknowledge the update: {warning}")
        say("If reopening the terminal does not refresh it, sign out and sign in again.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="RSS desktop alerts with local Markdown archives")
    parser.add_argument("--version", action="version", version=f"RSS {__version__}")
    parser.add_argument("-tui", "--tui", action="store_true", help="Open the TUI (alias for the tui command)")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="JSON config; paths are relative to this file")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="Monitor in the foreground; Ctrl+C stops it")
    run.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    once = commands.add_parser("once", help="Check once, archive, notify if changed, then exit")
    once.add_argument("--feed-file", type=Path, help="Import a saved RSS snapshot instead of making an HTTP request")
    once.add_argument("--source", help="Check/import only the specified feed ID")
    for name, help_text in (("start", "Start a detached background monitor"), ("stop", "Request a graceful stop"),
                            ("tui", "Browse RSS titles, read entries and manage subscriptions"),
                            ("install-path", "Add this directory to the current Windows user's PATH"),
                            ("uninstall-path", "Remove this directory from the current Windows user's PATH"),
                            ("status", "Show process, feed and notification status"), ("feeds", "List configured subscriptions and directories"),
                            ("rename", "Name existing archives with Codex (background must be stopped)"),
                            ("test-notification", "Send one explicitly labeled test notification")):
        commands.add_parser(name, help=help_text)
    args = parser.parse_args(argv)
    if args.tui and args.command not in (None, "tui"):
        parser.error("-tui / --tui cannot be combined with another command")
    try:
        command = args.command or ("tui" if args.tui else "run")
        if command in ("install-path", "uninstall-path"):
            return configure_path(remove=command == "uninstall-path")
        config = load_config(args.config)
        if command == "tui":
            try:
                from rss_tui import RSSApp
            except ImportError as error:
                raise RuntimeError("TUI dependencies are missing; run: py -m pip install -r requirements.txt") from error
            RSSApp(config).run()
            return 0
        if command == "run":
            return run_monitor(config, quiet=getattr(args, "quiet", False))
        if command == "once":
            return run_monitor(config, once=True, feed_file=args.feed_file, source_id=args.source)
        if command == "test-notification":
            feeds = load_feeds(config)
            url = feeds[0].config.feed_url if feeds else "https://status.deepseek.com"
            send_notification("RSS Update · 测试通知", "这是一条测试消息，不代表订阅源发生了新的事件。", url)
            say("Test notification submitted to the operating system.")
            return 0
        return {"start": start_monitor, "stop": stop_monitor, "status": status_monitor, "rename": rename_archives, "feeds": list_feeds}[command](config)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        if LOGGER.handlers:
            LOGGER.error("%s", error)
        say(f"Error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
