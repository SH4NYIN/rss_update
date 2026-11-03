"""RSS parsing, conditional HTTP requests and durable change tracking."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


MAX_FEED_BYTES = 2 * 1024 * 1024
STATUS_NAMES = {
    "investigating": "调查中", "identified": "原因已定位",
    "monitoring": "监控中", "resolved": "已恢复",
    "scheduled": "维护计划", "in_progress": "维护中",
    "verifying": "验证中", "completed": "已完成",
    "feed_updated": "时间更新",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def source_timestamp(value):
    """Comparable UTC timestamp; ignore missing, malformed and timezone-less dates."""
    try:
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            timestamp = parsedate_to_datetime(value)
        if timestamp.tzinfo is not None:
            return timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError, OverflowError):
        pass
    return ""


def safe_url(value, base=""):
    value = urllib.parse.urljoin(base, value.strip())
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return value
    return ""


class PlainText(HTMLParser):
    """Retain paragraph boundaries; never copy remote HTML into local reports."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if not self.hidden and tag in ("p", "div", "br", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in ("p", "div", "li", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    parser = PlainText()
    parser.feed(value)
    parser.close()
    lines = [re.sub(r"[\t \xa0]+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line)


@dataclass(frozen=True)
class Entry:
    key: str
    title: str
    url: str
    body: str
    published: str
    status: str

    @property
    def fingerprint(self):
        # Track source timestamps separately so time-only updates are labeled clearly.
        content = json.dumps([self.title, self.url, self.body], ensure_ascii=False)
        return sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Feed:
    title: str
    entries: list[Entry]
    updated: str = ""
    url: str = ""


def parse_feed(data: bytes, feed_url: str) -> Feed:
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("RSS exceeds the 2 MiB size limit")
    root = ET.fromstring(data)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Expected an RSS 2.0 channel (HTML and Atom are not supported)")
    channel = root.find("channel")
    title = plain_text(channel.findtext("title", "")) or "RSS Update"
    entries = {}
    for item in channel.findall("item"):
        body_html = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded")
        if not body_html:
            body_html = item.findtext("description", "")
        body = plain_text(body_html)
        item_title = plain_text(item.findtext("title", ""))
        if not item_title and not body:
            raise ValueError("RSS item has neither a title nor a description")
        item_title = item_title or body.splitlines()[0][:120]
        url = safe_url(item.findtext("link", ""), feed_url) if item.findtext("link") else ""
        published = item.findtext("pubDate", "").strip()
        try:
            timestamp = parsedate_to_datetime(published)
            # Do not silently assign a timezone to a malformed source timestamp.
            if timestamp.tzinfo is not None:
                published = timestamp.isoformat(timespec="seconds")
        except (ValueError, TypeError, OverflowError):
            pass
        guid = item.findtext("guid", "").strip()
        identity = "guid:" + guid if guid else "url:" + url if url else "fallback:" + item_title + "|" + published
        key = sha256(identity.encode("utf-8")).hexdigest()
        status_match = re.search(r"^Status:\s*([a-z_]+)", body, flags=re.I | re.M)
        status = status_match.group(1).lower() if status_match else ""
        entry = Entry(key, item_title, url, body, published, status)
        if key in entries and entries[key].fingerprint != entry.fingerprint:
            raise ValueError("RSS contains conflicting entries with the same identity")
        entries[key] = entry
    updated = max(source_timestamp(channel.findtext(name, "")) for name in ("lastBuildDate", "pubDate"))
    return Feed(title, list(entries.values()), updated, safe_url(channel.findtext("link", ""), feed_url))


@dataclass(frozen=True)
class FetchResult:
    data: bytes | None
    etag: str = ""
    last_modified: str = ""


def fetch_feed(url, timeout, etag="", last_modified=""):
    headers = {"User-Agent": "RSS-Update/0.1", "Accept": "application/rss+xml, application/xml, text/xml"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError(f"Unexpected HTTP status: {response.status}")
            data = response.read(MAX_FEED_BYTES + 1)
            if len(data) > MAX_FEED_BYTES:
                raise ValueError("RSS exceeds the 2 MiB size limit")
            return FetchResult(data, response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
    except urllib.error.HTTPError as error:
        if error.code == 304:
            error.close()
            return FetchResult(None)
        raise


class Store:
    def __init__(self, data_dir: Path, feed_url: str):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(data_dir / "state.sqlite3", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (
                key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, title TEXT NOT NULL,
                url TEXT NOT NULL, body TEXT NOT NULL, published TEXT NOT NULL,
                status TEXT NOT NULL, first_seen TEXT NOT NULL, changed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
                id INTEGER PRIMARY KEY, item_key TEXT NOT NULL, title TEXT NOT NULL,
                url TEXT NOT NULL, body TEXT NOT NULL, published TEXT NOT NULL,
                status TEXT NOT NULL, observed_at TEXT NOT NULL, kind TEXT NOT NULL,
                notification TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS revision_item ON revisions(item_key, id);
            CREATE TABLE IF NOT EXISTS names (
                title TEXT PRIMARY KEY, short_title TEXT NOT NULL DEFAULT '',
                retry_at REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
        """)
        existing_url = self.get("feed_url")
        if existing_url and existing_url != feed_url:
            self.db.close()
            raise ValueError("This data_dir belongs to another feed; choose a different data_dir")
        with self.db:
            self.set("feed_url", feed_url)

    def close(self):
        self.db.close()

    def get(self, key, default=""):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def record_success(self, result: FetchResult, now=None):
        now = now or utc_now()
        if result.data is None:
            if not self.get("initialized"):
                raise ValueError("Received HTTP 304 before a valid initial feed")
            with self.db:
                self.set("last_checked", now)
                self.set("last_success", now)
                self.set("last_error", "")
            return 0
        # Fully parse before changing state or saving HTTP cache validators.
        feed = parse_feed(result.data, self.get("feed_url"))
        baseline = not self.get("initialized")
        changed = 0
        with self.db:
            for entry in feed.entries:
                old = self.db.execute("SELECT fingerprint,published FROM items WHERE key=?", (entry.key,)).fetchone()
                if old and old["fingerprint"] == entry.fingerprint:
                    timestamp = source_timestamp(entry.published)
                    if not timestamp or timestamp <= source_timestamp(old["published"]):
                        continue
                    # Also ignore timestamps already seen before an older cached/content revision.
                    latest = max((source_timestamp(row[0]) for row in self.db.execute(
                        "SELECT published FROM revisions WHERE item_key=?", (entry.key,))), default="")
                    if timestamp <= latest:
                        continue
                    kind = "time_updated"
                else:
                    kind = "baseline" if baseline else "updated" if old else "new"
                self.save_revision(entry, kind, now)
                changed += 1
            previous_time = self.get("feed_timestamp")
            if feed.updated and feed.updated > previous_time:
                if previous_time and not baseline and not changed:
                    # A channel date cannot be attributed to an individual incident.
                    label = self.get("label") or feed.title
                    def local_time(value):
                        return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
                    notice = Entry(
                        sha256(("feed-timestamp:" + self.get("feed_url")).encode()).hexdigest(),
                        f"{label} · 来源时间更新", feed.url or self.get("feed_url"),
                        "程序提示：RSS 整体更新时间已变化，本次未发现新的事件内容。\n"
                        f"上次来源时间：{local_time(previous_time)}\n本次来源时间：{local_time(feed.updated)}\n"
                        "时间为北京时间 UTC+08:00。RSS 未说明实际服务状态是否变化，请打开源站确认。",
                        feed.updated, "feed_updated",
                    )
                    self.save_revision(notice, "feed_time_updated", now)
                    changed += 1
                self.set("feed_timestamp", feed.updated)
            for key, value in {"title": feed.title, "initialized": "1", "last_checked": now,
                               "last_success": now, "last_error": "", "etag": result.etag,
                               "last_modified": result.last_modified, "timestamp_tracking": "1"}.items():
                self.set(key, value)
        return changed

    def save_revision(self, entry, kind, now):
        """Caller owns the transaction, including timestamp baselines and notification queue."""
        self.db.execute("""
            INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,
                title=excluded.title, url=excluded.url, body=excluded.body,
                published=excluded.published, status=excluded.status, changed_at=excluded.changed_at
        """, (entry.key, entry.fingerprint, entry.title, entry.url, entry.body, entry.published, entry.status, now, now))
        self.db.execute("""
            INSERT INTO revisions (item_key,title,url,body,published,status,observed_at,kind,notification)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (entry.key, entry.title, entry.url, entry.body, entry.published, entry.status, now, kind,
              "baseline" if kind == "baseline" else "pending"))

    def record_error(self, error):
        with self.db:
            self.set("last_checked", utc_now())
            self.set("last_error", str(error)[:600])

    def pending(self):
        return self.db.execute("SELECT * FROM revisions WHERE notification='pending' ORDER BY id").fetchall()

    def mark_notifications(self, rows, state):
        with self.db:
            self.db.executemany("UPDATE revisions SET notification=? WHERE id=?", [(state, row["id"]) for row in rows])
            self.set("notification_error", "")

    def snapshot(self):
        return {
            "metadata": dict(self.db.execute("SELECT key,value FROM metadata")),
            "items": [dict(row) for row in self.db.execute("SELECT items.*,names.short_title FROM items LEFT JOIN names ON items.title=names.title ORDER BY changed_at DESC,published DESC,key")],
            "revisions": [dict(row) for row in self.db.execute("SELECT * FROM revisions ORDER BY id DESC")],
        }
