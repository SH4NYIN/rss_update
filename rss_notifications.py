"""Native notifications, isolated in a short-lived process with a deadline."""

from html import escape
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

from rss_core import STATUS_NAMES, safe_url


def availability_error():
    if sys.platform == "win32":
        if importlib.util.find_spec("win11toast") is None:
            return "Missing win11toast; run: py -m pip install -r requirements.txt"
    elif sys.platform.startswith("linux"):
        if shutil.which("notify-send") is None:
            return "Missing notify-send; use Windows Python for Windows alerts, or install libnotify-bin on a Linux desktop"
    else:
        return f"Desktop notifications are not implemented on {sys.platform}"
    return ""


def native_notify(title, body, url):
    error = availability_error()
    if error:
        raise RuntimeError(error)
    if sys.platform == "win32":
        from win11toast import notify
        # Protocol activation is handled by Windows even after this worker exits.
        notify(title, body, on_click=safe_url(url) or "https://status.deepseek.com")
    else:
        result = subprocess.run(
            [shutil.which("notify-send"), "--app-name=RSS Update", "--urgency=normal", "--",
             title, escape(body, quote=False)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=5,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip()[:400])


def send_notification(title, body, url):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.stem.lower() == "pythonw":
        executable = executable.with_name("python.exe")
    result = subprocess.run(
        [str(executable), str(Path(__file__).resolve()), "--worker"],
        input=json.dumps({"title": title, "body": body, "url": url}, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, **options,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip()[-600:] or "Notification worker failed")


def deliver_pending(store, enabled=True, sender=send_notification):
    rows = store.pending()
    if not rows:
        return 0
    if not enabled:
        store.mark_notifications(rows, "disabled")
        return 0
    latest = {}
    for row in rows:
        latest[row["item_key"]] = row
    def order(row):
        try:
            timestamp = datetime.fromisoformat(row["published"])
            published = timestamp.timestamp() if timestamp.tzinfo else 0
        except ValueError:
            published = 0
        return row["observed_at"], published, row["id"]
    events = sorted(latest.values(), key=order, reverse=True)
    source = store.get("label") or store.get("title", "RSS Update")
    def update_label(row):
        if row["kind"] in ("time_updated", "feed_time_updated"):
            return "时间更新（正文未变）"
        return STATUS_NAMES.get(row["status"], row["status"] or "有更新")
    if len(events) == 1:
        event = events[0]
        status = update_label(event)
        title = event["title"]
        lines = [line for line in event["body"].splitlines() if not line.lower().startswith(("status:", "affected components:"))]
        if event["kind"] == "time_updated":
            body = f"{source} · {status}\n事件发布时间已更新，RSS 未提供新的正文或状态，请打开源站确认。"
        else:
            body = f"{source} · {status}\n" + " ".join(lines)
    else:
        title = events[0]["title"]
        body = f"{source} · {len(events)} 条更新\n"
        body += "\n".join(f"{update_label(row)} · {row['title']}" for row in events[:3])
        if len(events) > 3:
            body += f"\n另有 {len(events) - 3} 条更新，详见本地存档。"
    try:
        sender(title[:100], body[:350], events[0]["url"] or store.get("feed_url"))
    except Exception as error:
        with store.db:
            store.set("notification_error", str(error)[:600])
        # Keep durable pending rows for a subsequent attempt, including on HTTP 304.
        return 0
    store.mark_notifications(rows, "sent")
    return len(rows)


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--worker"]:
            raise ValueError("Use rss_update.py test-notification")
        payload = json.loads(sys.stdin.buffer.read())
        native_notify(payload["title"], payload["body"], payload["url"])
    except Exception as error:
        if sys.stderr is not None:
            sys.stderr.buffer.write((str(error) + "\n").encode("utf-8"))
        raise SystemExit(1)
