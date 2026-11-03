"""UI-facing read-only library queries and conflict-checked subscription edits."""

from contextlib import contextmanager
from hashlib import sha256
import json
import sqlite3

from rss_feeds import load_feeds, validate_feeds
from rss_report import atomic_write
from rss_update import instance_lock, is_running, read_json


ENTRY_LIMIT = 1000


@contextmanager
def open_reader(path):
    # Never create a database, initialize Store, or update delivery state in a viewer.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.3)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def read_registry(config):
    if config.feeds_file is None:
        return None, None
    data = config.feeds_file.read_bytes()
    rows = json.loads(data.decode("utf-8-sig"))
    validate_feeds(config, rows)
    return rows, sha256(data).hexdigest()


def save_registry(config, rows, expected_version):
    if config.feeds_file is None:
        raise ValueError("当前为单源配置，请先在 config.json 中设置 feeds_file。")
    specs = validate_feeds(config, rows)
    with instance_lock(config.data_dir, "feeds.lock"):
        _, current_version = read_registry(config)
        if current_version != expected_version:
            raise ValueError("订阅列表已被其它窗口或编辑器修改，请关闭表单、刷新后重试。")
        for spec in specs:
            database = spec.config.data_dir / "state.sqlite3"
            if database.exists():
                with open_reader(database) as connection:
                    previous = connection.execute("SELECT value FROM metadata WHERE key='feed_url'").fetchone()
                if previous and previous[0] != spec.config.feed_url:
                    raise ValueError(f"{spec.id} 已有另一个地址的历史记录，请为新地址使用新的源 ID。")
        if sha256(config.feeds_file.read_bytes()).hexdigest() != expected_version:
            raise ValueError("订阅列表刚被其它窗口或编辑器修改，请刷新后重试。")
        atomic_write(config.feeds_file, json.dumps(rows, ensure_ascii=False, indent=2) + "\n")


def read_library(config):
    specs = load_feeds(config)
    feeds, entries = [], []
    for spec in specs:
        result = {"id": spec.id, "name": spec.name, "enabled": spec.enabled, "url": spec.config.feed_url,
                  "count": 0, "metadata": {}, "error": ""}
        database = spec.config.data_dir / "state.sqlite3"
        if database.exists():
            try:
                with open_reader(database) as connection:
                    result["metadata"] = dict(connection.execute("SELECT key,value FROM metadata"))
                    result["count"] = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                    rows = connection.execute("""SELECT key,title,published,status,changed_at
                        FROM items ORDER BY changed_at DESC,published DESC,key LIMIT ?""", (ENTRY_LIMIT,)).fetchall()
                for row in rows:
                    entries.append(dict(row, feed_id=spec.id, feed_name=spec.name, row_id=f"{spec.id}:{row['key']}"))
            except (OSError, sqlite3.Error) as error:
                result["error"] = str(error)
        feeds.append(result)
    entries.sort(key=lambda row: (row["changed_at"], row["published"], row["row_id"]), reverse=True)
    return {"feeds": feeds, "entries": entries, "running": is_running(config),
            "process": read_json(config.data_dir / ".runtime" / "process.json")}


def read_entry(config, feed_id, entry_key):
    spec = next((spec for spec in load_feeds(config) if spec.id == feed_id), None)
    if spec is None:
        raise ValueError("该订阅已被移除，请刷新列表。")
    database = spec.config.data_dir / "state.sqlite3"
    with open_reader(database) as connection:
        row = connection.execute("SELECT * FROM items WHERE key=?", (entry_key,)).fetchone()
        if row is None:
            raise ValueError("该事件已不在当前记录中。")
        versions = connection.execute("SELECT COUNT(*) FROM revisions WHERE item_key=?", (entry_key,)).fetchone()[0]
        latest = connection.execute("SELECT kind FROM revisions WHERE item_key=? ORDER BY id DESC LIMIT 1", (entry_key,)).fetchone()
    return dict(row, feed_name=spec.name, versions=versions, kind=latest[0] if latest else "")
