from dataclasses import replace
from html import escape
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from rss_core import FetchResult, MAX_FEED_BYTES, Store, fetch_feed, parse_feed
from rss_notifications import deliver_pending, native_notify
from rss_naming import NamingConfig, NamingWorker, codex_command, request_names, title_id, validate_names
from rss_feeds import load_feeds
from rss_report import export_reports
from rss_update import AlreadyRunning, Config, check_once, instance_lock, load_config


URL = "https://status.example.test/feed.rss"
ROOT = Path(__file__).resolve().parents[1]


def feed(status="monitoring", title="服务异常", guid="incident-1", published="Tue, 03 Nov 2026 16:58:16 +0800", body=None):
    content = body if body is not None else f"<p><strong>Status:</strong> {status}</p><p>状态正文：{status}</p>"
    return f'''<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>
      <title>Example 服务</title><lastBuildDate>ignored</lastBuildDate><item>
      <title>{escape(title)}</title><guid>{escape(guid)}</guid><link>https://status.example.test/incidents/1</link>
      <pubDate>{published}</pubDate><description>{escape(content)}</description>
      </item></channel></rss>'''.encode()


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = Store(self.directory, URL)
        self.config = Config(self.directory / "config.json", URL, 120, 5, self.directory, True)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ingest(self, data, **kwargs):
        return self.store.record_success(FetchResult(data, **kwargs))

    def test_first_snapshot_silent_then_same_identity_update_notifies(self):
        self.assertEqual(self.ingest(feed()), 1)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.ingest(feed()), 0)
        self.assertEqual(self.ingest(feed("resolved")), 1)
        self.assertEqual(len(self.store.snapshot()["items"]), 1)
        self.assertEqual(len(self.store.snapshot()["revisions"]), 2)
        sent = []
        self.assertEqual(deliver_pending(self.store, sender=lambda *args: sent.append(args)), 1)
        self.assertEqual(sent[0][0], "服务异常")
        self.assertIn("已恢复", sent[0][1])
        self.assertEqual(sent[0][2], "https://status.example.test/incidents/1")
        self.assertFalse(self.store.pending())

    def test_restart_persists_dedup_and_history(self):
        self.ingest(feed())
        self.ingest(feed("resolved"))
        deliver_pending(self.store, sender=lambda *args: None)
        self.store.close()
        self.store = Store(self.directory, URL)
        self.assertEqual(self.ingest(feed("resolved")), 0)
        self.assertFalse(self.store.pending())
        self.assertEqual(len(self.store.snapshot()["revisions"]), 2)

    def test_date_only_updates_and_reverted_content_are_saved(self):
        self.ingest(feed())
        self.assertEqual(self.ingest(feed(published="Wed, 04 Nov 2026 16:58:16 +0800")), 1)
        self.assertEqual(self.store.pending()[0]["kind"], "time_updated")
        self.ingest(feed("resolved"))
        self.assertEqual(self.ingest(feed()), 1)
        self.assertEqual(len(self.store.snapshot()["revisions"]), 4)

    def test_same_title_different_guid_is_new_event(self):
        self.ingest(feed())
        self.ingest(feed(guid="incident-2"))
        self.assertEqual(len(self.store.snapshot()["items"]), 2)
        self.assertEqual(self.store.pending()[0]["kind"], "new")

    def test_failed_notification_is_retried_after_restart_and_304(self):
        self.ingest(feed())
        self.ingest(feed("resolved"))
        def fail(*args):
            raise RuntimeError("desktop unavailable")
        self.assertEqual(deliver_pending(self.store, sender=fail), 0)
        self.assertEqual(len(self.store.pending()), 1)
        self.store.close()
        self.store = Store(self.directory, URL)
        self.store.record_success(FetchResult(None))
        self.assertEqual(deliver_pending(self.store, sender=lambda *args: None), 1)
        self.assertFalse(self.store.get("notification_error"))

    def test_batch_updates_submit_only_one_notification(self):
        self.ingest(feed())
        self.ingest(feed("resolved"))
        self.ingest(feed(guid="incident-2"))
        sent = []
        self.assertEqual(deliver_pending(self.store, sender=lambda *args: sent.append(args)), 2)
        self.assertEqual(len(sent), 1)
        self.assertIn("2 条更新", sent[0][1])

    def test_batch_notification_uses_newest_title_even_if_feed_lists_newest_first(self):
        self.ingest(b"<rss><channel><title>Empty</title></channel></rss>")
        newest = feed(title="最新事件", guid="newest")
        older = feed(title="较早事件", guid="older", published="Mon, 02 Nov 2026 16:58:16 +0800")
        combined = newest.replace(b"</channel>", older[older.index(b"<item>"):older.index(b"</item>") + 7] + b"</channel>")
        self.ingest(combined)
        sent = []
        deliver_pending(self.store, sender=lambda *args: sent.append(args))
        self.assertEqual(sent[0][0], "最新事件")

    def test_invalid_feed_does_not_initialize_or_poison_cache(self):
        with self.assertRaises(ValueError):
            self.ingest(b"<html>upstream unavailable</html>", etag='"bad"')
        self.assertFalse(self.store.get("initialized"))
        self.assertFalse(self.store.get("etag"))
        self.ingest(feed(), etag='"good"')
        with self.assertRaises(ValueError):
            self.ingest(b"<html>error</html>", etag='"bad"')
        self.assertEqual(self.store.get("etag"), '"good"')
        self.assertEqual(len(self.store.snapshot()["items"]), 1)

    def test_empty_valid_feed_is_baseline_then_first_event_notifies(self):
        self.ingest(b"<rss><channel><title>Empty</title></channel></rss>")
        self.ingest(feed())
        self.assertEqual(self.store.pending()[0]["kind"], "new")

    def test_network_failure_then_first_success_still_silent(self):
        with patch("rss_update.fetch_feed", side_effect=URLError("offline")):
            self.assertFalse(check_once(self.config, self.store))
        self.assertFalse(self.store.get("initialized"))
        with patch("rss_update.fetch_feed", return_value=FetchResult(feed())):
            self.assertTrue(check_once(self.config, self.store))
        self.assertFalse(self.store.pending())
        self.assertFalse(self.store.get("last_error"))

    def test_export_failure_does_not_lose_records_and_next_poll_repairs_it(self):
        config = replace(self.config, notifications=False)
        with patch("rss_update.fetch_feed", return_value=FetchResult(feed())), patch("rss_update.export_reports", side_effect=OSError("disk busy")):
            self.assertFalse(check_once(config, self.store))
        self.assertEqual(len(self.store.snapshot()["items"]), 1)
        with patch("rss_update.fetch_feed", return_value=FetchResult(None)):
            self.assertTrue(check_once(config, self.store))
        self.assertTrue((self.directory / "index.md").exists())
        self.assertFalse(self.store.get("export_error"))

    def test_markdown_keeps_source_history_without_executing_remote_html(self):
        malicious = '<p>Status: monitoring</p><script>alert(1)</script><img src=x onerror=alert(2)><p>&lt;script&gt;literal&lt;/script&gt; 中文 &amp; text</p>'
        self.ingest(feed(title="中文 [title] __CARDS__", body=malicious))
        self.ingest(feed("resolved", title="中文 [title] __CARDS__"))
        export_reports(self.store, self.directory)
        files = list((self.directory / "archive").rglob("*.md"))
        self.assertEqual(len(files), 1)
        markdown = files[0].read_text()
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("onerror", markdown)
        self.assertNotIn("alert(1)", markdown)
        self.assertIn("&lt;script&gt;literal&lt;/script&gt;", markdown)
        self.assertFalse(list(self.directory.rglob("*.html")))
        self.assertIn("初始快照", markdown)
        self.assertIn("内容更新", markdown)
        self.assertIn("\\[title\\]", markdown)

    def test_http_uses_cache_headers_and_handles_304(self):
        error = HTTPError(URL, 304, "Not Modified", {}, io.BytesIO())
        with patch("urllib.request.urlopen", side_effect=error) as opener:
            result = fetch_feed(URL, 5, 'W/"etag"', "Tue, 03 Nov 2026 08:00:00 GMT")
        self.assertIsNone(result.data)
        request = opener.call_args[0][0]
        self.assertEqual(request.get_header("If-none-match"), 'W/"etag"')
        self.assertIn("If-modified-since", request.headers)
        with self.assertRaises(ValueError):
            self.store.record_success(result)

    def test_http_success_enforces_size_and_returns_validators(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"ETag": '"version"', "Last-Modified": "date"}
        with patch("urllib.request.urlopen", return_value=Response(feed())):
            result = fetch_feed(URL, 5)
        self.assertEqual(result.etag, '"version"')
        self.assertEqual(result.data, feed())
        with patch("urllib.request.urlopen", return_value=Response(b"x" * (MAX_FEED_BYTES + 1))):
            with self.assertRaises(ValueError):
                fetch_feed(URL, 5)

    def test_feed_binding_prevents_accidental_history_reuse(self):
        with self.assertRaisesRegex(ValueError, "another feed"):
            Store(self.directory, "https://other.example.test/feed.rss")

    def test_lock_prevents_two_writers_and_releases_on_exit(self):
        with instance_lock(self.directory):
            with self.assertRaises(AlreadyRunning):
                with instance_lock(self.directory):
                    pass
        with instance_lock(self.directory):
            pass

    def test_disabled_notifications_do_not_replay_later(self):
        self.ingest(feed())
        self.ingest(feed("resolved"))
        with patch("rss_notifications.send_notification") as sender:
            deliver_pending(self.store, enabled=False, sender=sender)
        sender.assert_not_called()
        self.assertFalse(self.store.pending())

    def test_parse_safe_links_and_reject_conflicting_identity(self):
        xml = feed().replace(b"https://status.example.test/incidents/1", b"javascript:alert(1)")
        self.assertEqual(parse_feed(xml, URL).entries[0].url, "")
        first, second = feed(), feed("resolved")
        duplicated = first.replace(b"</channel>", second[second.index(b"<item>"):second.index(b"</item>") + 7] + b"</channel>")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            parse_feed(duplicated, URL)

    def test_config_paths_are_relative_to_config_not_cwd(self):
        self.config.path.write_text(json.dumps({"feed_url": URL, "data_dir": "archive", "notifications": False}))
        config = load_config(self.config.path)
        self.assertEqual(config.data_dir, self.directory / "archive")
        self.assertFalse(config.notifications)

    def test_windows_notification_uses_protocol_activation(self):
        from types import SimpleNamespace
        calls = []
        module = SimpleNamespace(notify=lambda *args, **kwargs: calls.append((args, kwargs)))
        with patch("rss_notifications.sys.platform", "win32"), patch("rss_notifications.availability_error", return_value=""), patch.dict(sys.modules, {"win11toast": module}):
            native_notify("DeepSeek", "已恢复", "https://status.deepseek.com")
        self.assertEqual(calls[0][1]["on_click"], "https://status.deepseek.com")

    def test_async_naming_does_not_block_updates_and_preserves_original_text(self):
        self.ingest(feed(title="服务异常（Long English translation）"))
        export_reports(self.store, self.directory)
        old = next((self.directory / "archive").rglob("*.md"))
        release = threading.Event()
        calls = []
        def request(config, titles, cancelled):
            calls.append(titles)
            release.wait(3)
            return {title: "服务异常" for title in titles}
        worker = NamingWorker(NamingConfig(enabled=True), request=request)
        try:
            started = time.monotonic()
            worker.tick(self.store)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(worker.busy)
            self.ingest(feed("resolved", title="服务异常（Long English translation）"))
            self.assertEqual(len(self.store.pending()), 1)
            release.set()
            deadline = time.monotonic() + 3
            while worker.busy and time.monotonic() < deadline:
                time.sleep(0.01)
                if worker.tick(self.store):
                    export_reports(self.store, self.directory)
            self.assertFalse(worker.busy)
            new = list((self.directory / "archive").rglob("*.md"))
            self.assertEqual(len(new), 1)
            self.assertFalse(old.exists())
            self.assertIn("_服务异常_", new[0].name)
            self.assertIn("Long English translation", new[0].read_text())
            self.assertIn("resolved", new[0].read_text())
            self.assertIn("初始快照", new[0].read_text())
            worker.next_scan = 0
            worker.tick(self.store)
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
            worker.close()

    def test_naming_failure_keeps_archive_and_schedules_retry(self):
        self.ingest(feed())
        export_reports(self.store, self.directory)
        old = next((self.directory / "archive").rglob("*.md"))
        def failed(*args):
            raise RuntimeError("Codex unavailable")
        worker = NamingWorker(NamingConfig(enabled=True), request=failed)
        try:
            worker.tick(self.store)
            deadline = time.monotonic() + 3
            while worker.busy and time.monotonic() < deadline:
                time.sleep(0.01)
                worker.tick(self.store)
            self.assertTrue(old.exists())
            self.assertEqual(self.store.get("naming_error"), "Codex unavailable")
            self.assertGreater(worker.next_scan, time.time())
        finally:
            worker.close()

    def test_naming_response_rejects_path_injection_and_missing_ids(self):
        titles = ["服务异常"]
        for bad in ("../outside", "bad/name", "a.md", "a" * 33):
            payload = json.dumps({"names": [{"id": title_id(titles[0]), "name": bad}]})
            with self.assertRaises(ValueError):
                validate_names(payload, titles)
        with self.assertRaises(ValueError):
            validate_names('{"names": []}', titles)
        payload = json.dumps({"names": [{"id": title_id(titles[0]), "name": "服务中断"}]})
        self.assertEqual(validate_names(payload, titles), {"服务异常": "服务中断"})

    def test_windows_codex_command_keeps_titles_out_of_shell_arguments(self):
        with patch("rss_naming.sys.platform", "win32"):
            command = codex_command(NamingConfig(enabled=True, wsl_distribution="Ubuntu"))
        self.assertEqual(command[0], "wsl.exe")
        self.assertIn("Ubuntu", command)
        self.assertIn("read-only", command)
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[-1], "-")

    def test_codex_subprocess_contract_with_local_stub(self):
        stub = self.directory / "stub.py"
        stub.write_text("import json,sys\nprompt=sys.stdin.read()\nitems=json.loads(prompt.split('\\n',1)[1])\nprint(json.dumps({'names':[{'id':row['id'],'name':'服务异常'} for row in items]},ensure_ascii=False))\n")
        config = NamingConfig(enabled=True, command=(sys.executable, str(stub)))
        self.assertEqual(request_names(config, ["服务异常（English）"], threading.Event()), {"服务异常（English）": "服务异常"})

    def test_feed_registry_isolates_same_guid_across_sources(self):
        registry = self.directory / "feeds.json"
        registry.write_text(json.dumps([
            {"id": "alpha", "name": "Alpha", "url": URL},
            {"id": "beta", "name": "Beta", "url": "https://beta.example.test/feed.rss"}
        ]))
        config = replace(self.config, feed_url="", feeds_file=registry)
        specs = load_feeds(config)
        for spec in specs:
            store = Store(spec.config.data_dir, spec.config.feed_url)
            try:
                store.record_success(FetchResult(feed(title=spec.name)))
                export_reports(store, spec.config.data_dir)
            finally:
                store.close()
        self.assertIn("Alpha", next((self.directory / "alpha" / "archive").rglob("*.md")).read_text())
        self.assertIn("Beta", next((self.directory / "beta" / "archive").rglob("*.md")).read_text())
        self.assertEqual(len(list(self.directory.rglob("state.sqlite3"))), 3)

    def test_feed_registry_rejects_unsafe_or_duplicate_ids(self):
        registry = self.directory / "feeds.json"
        config = replace(self.config, feed_url="", feeds_file=registry)
        for key in ("../escape", "CON", "con", "logs", "a/b"):
            registry.write_text(json.dumps([{"id": key, "url": URL}]))
            with self.assertRaises(ValueError):
                load_feeds(config)
        registry.write_text(json.dumps([{"id": "a", "url": URL}, {"id": "a", "url": URL}]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_feeds(config)


class ProcessTests(unittest.TestCase):
    def test_running_monitor_reloads_added_and_paused_feeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            registry = directory / "feeds.json"
            registry.write_text(json.dumps([{"id": "alpha", "url": URL}]))
            config = directory / "config.json"
            config.write_text(json.dumps({"feeds_file": "feeds.json", "data_dir": "data", "notifications": False}))
            script = "import sys;sys.path.insert(0,sys.argv[1]);import rss_update;from rss_core import FetchResult;rss_update.fetch_feed=lambda *a: FetchResult(" + repr(feed()) + ");raise SystemExit(rss_update.main(['--config',sys.argv[2],'run','--quiet']))"
            process = subprocess.Popen([sys.executable, "-c", script, str(ROOT), str(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            def wait_for(predicate):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    if process.poll() is not None:
                        self.fail("Monitor exited: " + process.communicate()[1].decode())
                    time.sleep(0.1)
                self.fail("Timed out waiting for registry reload")
            try:
                wait_for(lambda: (directory / "data" / "alpha" / "index.md").exists())
                registry.write_text(json.dumps([{"id": "alpha", "url": URL, "enabled": False},
                                                {"id": "beta", "url": "https://beta.example.test/feed.rss"}]))
                wait_for(lambda: (directory / "data" / "beta" / "index.md").exists())
                self.assertTrue((directory / "data" / "alpha" / "index.md").exists())
                metadata = json.loads((directory / "data" / ".runtime" / "process.json").read_text())
                self.assertEqual(metadata["active_feeds"], ["beta"])
                registry.write_text("[")  # Incomplete editor save must not stop existing subscriptions.
                wait_for(lambda: bool(json.loads((directory / "data" / ".runtime" / "process.json").read_text()).get("registry_error")))
                metadata = json.loads((directory / "data" / ".runtime" / "process.json").read_text())
                self.assertEqual(metadata["active_feeds"], ["beta"])
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    def test_background_start_status_stop_and_single_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            config = directory / "config.json"
            # An intentionally unreachable endpoint makes this test independent of Internet access.
            config.write_text(json.dumps({"feed_url": "http://127.0.0.1:1/feed.rss", "request_timeout_seconds": 1,
                                          "data_dir": "data", "notifications": False}))
            command = [sys.executable, str(ROOT / "rss_update.py"), "--config", str(config)]
            def run(*args):
                return subprocess.run(command + list(args), capture_output=True, text=True, timeout=15)
            try:
                started = run("start")
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                first = json.loads(run("status").stdout)
                self.assertTrue(first["running"])
                self.assertEqual(run("start").returncode, 0)
                second = json.loads(run("status").stdout)
                self.assertEqual(first["process"]["pid"], second["process"]["pid"])
                self.assertEqual(run("once").returncode, 1)
            finally:
                run("stop")
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if not json.loads(run("status").stdout)["running"]:
                        break
                    time.sleep(0.1)
                else:
                    self.fail("Background process did not stop")
            self.assertTrue((directory / "data" / "index.md").exists())


if __name__ == "__main__":
    unittest.main()
