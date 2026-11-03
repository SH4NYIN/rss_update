"""Plain Markdown event archives; model-generated names never replace source text."""

from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote

from rss_core import STATUS_NAMES


KINDS = {"baseline": "初始快照", "new": "新事件", "updated": "内容更新",
         "time_updated": "时间更新（正文未变）", "feed_time_updated": "来源时间更新（程序提示）"}
DISPLAY_TZ = timezone(timedelta(hours=8))


def display_time(value):
    if not value:
        return "尚无记录"
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None:
            return value
        return timestamp.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except FileNotFoundError:
        pass
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def markdown_text(value):
    value = escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def filename_title(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .-_")[:40].rstrip(" .")
    return value or "RSS事件"


def archive_path(item, history):
    initial = history[-1]
    date = display_time(initial["published"] or item["first_seen"])[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = display_time(item["first_seen"])[:10]
    name = filename_title(item.get("short_title") or item["title"])
    return Path("archive") / date[:4] / date[5:7] / f"{date}_{name}_{item['key'][:12]}.md"


def export_reports(store, data_dir: Path):
    snapshot = store.snapshot()
    meta, items = snapshot["metadata"], snapshot["items"]
    revisions = {}
    for row in snapshot["revisions"]:
        revisions.setdefault(row["item_key"], []).append(row)
    index = [f"# {markdown_text(meta.get('title', 'RSS Update'))} — 更新记录", "",
        f"订阅源：<{meta['feed_url']}>", "", f"最近检查：{display_time(meta.get('last_checked', ''))}",
        f"最近成功：{display_time(meta.get('last_success', ''))}", "",
        "时间均为北京时间 UTC+08:00。正文保留源站文字；初始快照之前及轮询间未观察到的变化无法还原。", ""]
    for key, label in (("last_error", "抓取失败"), ("notification_error", "通知暂不可用"), ("naming_error", "文件命名暂不可用")):
        if meta.get(key):
            index.extend([f"> {label}：{markdown_text(meta[key])}", ""])
    manifest_path = data_dir / ".runtime" / "archives.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        previous = {}
    current = {}
    for item in items:
        history = revisions[item["key"]]
        path = archive_path(item, history)
        current[item["key"]] = path.as_posix()
        status = STATUS_NAMES.get(item["status"], item["status"] or "更新")
        text = [f"# {markdown_text(item['title'])}", "", f"来源：{markdown_text(meta.get('title', 'RSS'))}", "",
            f"原文：<{quote(item['url'], safe='/:?&=%#@+,-._~')}>" if item["url"] else "原文链接：未提供", "",
            f"当前状态：{status}", "", "以下按本地观察时间倒序排列；来源时间更新为程序提示，其余正文保留源站文字。", ""]
        for row in history:
            text.extend([f"## {display_time(row['observed_at'])} · {KINDS[row['kind']]}", "",
                f"{'提示标题' if row['kind'] == 'feed_time_updated' else '原始标题'}：{markdown_text(row['title'])}", "", f"源站时间：{display_time(row['published'])}", "",
                markdown_text(row["body"]), ""])
        atomic_write(data_dir / path, "\n".join(text))
        label = markdown_text(item.get("short_title") or item["title"])
        index.append(f"- {display_time(item['published'])} · **{status}** · [{label}]({quote(path.as_posix())}) · {len(history)} 个版本")
    atomic_write(data_dir / "index.md", "\n".join(index) + "\n")
    root = (data_dir / "archive").resolve()
    for key, old_path in previous.items():
        if key in current and old_path != current[key]:
            old = (data_dir / old_path).resolve()
            if old.is_relative_to(root) and old.name.endswith(f"_{key[:12]}.md"):
                old.unlink(missing_ok=True)
    atomic_write(manifest_path, json.dumps(current, ensure_ascii=False, indent=2))
