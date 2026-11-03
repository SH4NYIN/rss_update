"""Editable feed registry and per-source archive roots, independent of any UI."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
from urllib.parse import quote

from rss_core import safe_url
from rss_report import atomic_write, markdown_text


@dataclass(frozen=True)
class FeedSpec:
    id: str
    name: str
    enabled: bool
    config: object


def load_feeds(config):
    if config.feeds_file is None:
        return [FeedSpec("default", "RSS", True, config)]
    values = json.loads(config.feeds_file.read_text(encoding="utf-8-sig"))
    return validate_feeds(config, values)


def validate_feeds(config, values):
    """Validate a proposed registry without writing it or opening any database."""
    if not isinstance(values, list):
        raise ValueError("feeds.json must be a JSON array")
    result, ids = [], set()
    reserved = {"logs", "archive", "con", "prn", "aux", "nul"} | {f"{prefix}{i}" for prefix in ("com", "lpt") for i in range(1, 10)}
    for value in values:
        if not isinstance(value, dict) or value.keys() - {"id", "name", "url", "enabled", "poll_interval_seconds"}:
            raise ValueError("Each feed must contain only id, name, url, enabled and optional poll_interval_seconds")
        key = value.get("id", "")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", key) or key in reserved:
            raise ValueError("Feed id must be a safe lowercase directory name (a-z, 0-9, _ or -)")
        if key in ids:
            raise ValueError(f"Duplicate feed id: {key}")
        ids.add(key)
        url = value.get("url", "")
        if not isinstance(url, str) or not safe_url(url):
            raise ValueError(f"Invalid feed URL: {key}")
        name = value.get("name", key)
        enabled = value.get("enabled", True)
        interval = value.get("poll_interval_seconds", config.interval)
        if not isinstance(name, str) or not name.strip() or type(enabled) is not bool:
            raise ValueError(f"Invalid feed name/enabled: {key}")
        if type(interval) is not int or not 30 <= interval <= 86400:
            raise ValueError(f"Invalid polling interval: {key}")
        source_config = replace(config, feed_url=safe_url(url), data_dir=config.data_dir / key, interval=interval, feeds_file=None)
        result.append(FeedSpec(key, name.strip(), enabled, source_config))
    return result


def export_feed_index(config, feeds, error=""):
    if config.feeds_file is None:
        return
    lines = ["# RSS 订阅归档", "", "订阅列表由 feeds.json 管理。每个来源独立保存，文件正文保留源站内容。", ""]
    if error:
        lines.extend([f"> 订阅配置暂未加载：{markdown_text(error)}", ""])
    for spec in feeds:
        label = markdown_text(spec.name)
        status = "启用" if spec.enabled else "暂停"
        destination = spec.config.data_dir / "index.md"
        if destination.exists():
            lines.append(f"- **{status}** · [{label}]({quote(spec.id)}/index.md)")
        else:
            lines.append(f"- **{status}** · {label}（尚无记录）")
    atomic_write(config.data_dir / "index.md", "\n".join(lines) + "\n")
