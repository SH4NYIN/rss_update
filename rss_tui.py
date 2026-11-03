"""A small RSS reader and subscription manager; monitoring stays in the background."""

import asyncio
from copy import deepcopy
import sqlite3
import webbrowser

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from rss_core import STATUS_NAMES, safe_url
from rss_reader import ENTRY_LIMIT, read_entry, read_library, read_registry, save_registry
from rss_report import display_time
from rss_update import ROOT, load_config, start_monitor


def literal(value, style=""):
    return Text(str(value), style=style, overflow="ellipsis", no_wrap=True)


class EntryDetails(ModalScreen):
    BINDINGS = [("escape,backspace", "back", "返回"), ("o", "open_original", "打开原文")]
    DEFAULT_CSS = """
    EntryDetails { align: center middle; background: $background 70%; }
    #details-panel { width: 94%; height: 92%; border: round $primary; background: $surface; }
    #details-heading { height: auto; padding: 1 2; text-style: bold; }
    #details-scroll { height: 1fr; padding: 0 2 1 2; }
    #details-text { height: auto; }
    #details-hint { height: auto; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, entry):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        item = self.entry
        status = STATUS_NAMES.get(item["status"], item["status"] or "—")
        body = Text()
        body.append(f"来源：{item['feed_name']}\n", style="bold")
        body.append(f"状态：{status}\n源站时间：{display_time(item['published'])}\n")
        body.append(f"本地观察：{display_time(item['changed_at'])}（北京时间）\n")
        body.append(f"原文：{item['url'] or '未提供链接'}\n\n", style="dim")
        if item.get("kind") == "time_updated":
            body.append("本次仅事件发布时间更新，正文和 RSS 状态未变；实际服务状态请以源站为准。\n\n", style="yellow")
        body.append(item["body"] or "此条目没有提供正文。")
        with Vertical(id="details-panel"):
            yield Static(Text(item["title"]), id="details-heading")
            with VerticalScroll(id="details-scroll"):
                yield Static(body, id="details-text")
            origin = "来源时间更新（程序提示）" if item["status"] == "feed_updated" else "RSS 提供的完整内容"
            yield Static(f"{origin} · 共保存 {item['versions']} 个版本 · ↑↓ / 滚轮阅读 · Esc 返回 · O 打开源站", id="details-hint")
        yield Footer()

    def on_mount(self):
        self.query_one("#details-scroll").focus()

    def action_back(self):
        self.dismiss()

    async def action_open_original(self):
        url = safe_url(self.entry["url"])
        if url:
            await asyncio.to_thread(webbrowser.open, url)
        else:
            self.notify("该条目未提供可用的原文链接。")


class FeedEditor(ModalScreen):
    BINDINGS = [("escape", "cancel", "取消"), ("ctrl+s", "save", "保存")]
    DEFAULT_CSS = """
    FeedEditor { align: center middle; background: $background 70%; }
    #feed-form { width: 74; max-width: 95%; height: 29; max-height: 95%; border: round $primary; background: $surface; padding: 1 2; }
    #form-title { height: 2; text-style: bold; }
    #form-fields { height: 1fr; }
    #form-fields Label { height: 1; }
    #form-fields Input { margin-bottom: 1; }
    #form-error { height: auto; max-height: 3; color: $error; }
    #form-buttons { height: 3; align: right middle; }
    #form-buttons Button { margin-left: 1; }
    """

    def __init__(self, config, rows, version, feed_id=None):
        super().__init__()
        self.config = config
        self.rows = deepcopy(rows)
        self.version = version
        self.feed_id = feed_id
        self.original = next((row for row in rows if row["id"] == feed_id), {})
        self.saving = False

    def compose(self):
        item = self.original
        with Vertical(id="feed-form"):
            yield Static("编辑订阅源" if self.feed_id else "添加订阅源", id="form-title")
            with VerticalScroll(id="form-fields"):
                yield Label("源 ID（小写字母 / 数字 / - / _，也是归档目录名）")
                yield Input(value=item.get("id", ""), placeholder="例如 deepseek", id="feed-id", disabled=bool(self.feed_id))
                yield Label("显示名称")
                yield Input(value=item.get("name", item.get("id", "")), placeholder="例如 DeepSeek 服务状态", id="feed-name")
                yield Label("RSS 地址（新地址请添加新源）" if self.feed_id else "RSS 地址")
                yield Input(value=item.get("url", ""), placeholder="https://example.com/feed.rss", id="feed-url", disabled=bool(self.feed_id))
                yield Label("检查间隔 / 秒（30–86400）")
                yield Input(value=str(item.get("poll_interval_seconds", self.config.interval)), id="feed-interval", type="integer")
            yield Static("", id="form-error")
            with Horizontal(id="form-buttons"):
                yield Button("取消", id="form-cancel")
                yield Button("保存", id="form-save", variant="primary")

    def on_mount(self):
        self.query_one("#feed-name" if self.feed_id else "#feed-id", Input).focus()

    def action_cancel(self):
        if not self.saving:
            self.dismiss(False)

    async def action_save(self):
        if self.saving:
            return
        try:
            item = dict(self.original)
            item.update(id=self.query_one("#feed-id", Input).value.strip(),
                        name=self.query_one("#feed-name", Input).value.strip(),
                        url=self.query_one("#feed-url", Input).value.strip(),
                        enabled=item.get("enabled", True))
            item["poll_interval_seconds"] = int(self.query_one("#feed-interval", Input).value)
            if not item["name"]:
                raise ValueError("请输入显示名称。")
            if self.feed_id:
                rows = [item if row["id"] == self.feed_id else row for row in self.rows]
            else:
                rows = self.rows + [item]
            self.saving = True
            self.query_one("#form-save", Button).disabled = True
            await asyncio.to_thread(save_registry, self.config, rows, self.version)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self.query_one("#form-error", Static).update(Text(str(error)))
            self.saving = False
            self.query_one("#form-save", Button).disabled = False
        else:
            self.dismiss(True)

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "form-save":
            await self.action_save()
        else:
            self.action_cancel()

    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "feed-interval":
            await self.action_save()
        else:
            self.focus_next()


class RemoveFeed(ModalScreen):
    BINDINGS = [("escape", "cancel", "取消")]
    DEFAULT_CSS = """
    RemoveFeed { align: center middle; background: $background 70%; }
    #remove-panel { width: 64; max-width: 95%; height: auto; border: round $warning; background: $surface; padding: 1 2; }
    #remove-panel Static { height: auto; margin-bottom: 1; }
    #remove-buttons { height: 3; align: right middle; }
    #remove-buttons Button { margin-left: 1; }
    #remove-error { color: $error; }
    """

    def __init__(self, config, rows, version, feed_id):
        super().__init__()
        self.config, self.rows, self.version, self.feed_id = config, rows, version, feed_id

    def compose(self):
        row = next(row for row in self.rows if row["id"] == self.feed_id)
        with Vertical(id="remove-panel"):
            yield Static(Text(f"移除订阅「{row.get('name', self.feed_id)}」？", style="bold"))
            yield Static("停止监测此来源。已保存的 Markdown 和历史记录全部保留。")
            yield Static("", id="remove-error")
            with Horizontal(id="remove-buttons"):
                yield Button("取消", id="remove-cancel")
                yield Button("移除订阅", id="remove-confirm", variant="warning")

    def on_mount(self):
        self.query_one("#remove-cancel", Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "remove-cancel":
            self.dismiss(False)
            return
        try:
            rows = [row for row in self.rows if row["id"] != self.feed_id]
            await asyncio.to_thread(save_registry, self.config, rows, self.version)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self.query_one("#remove-error", Static).update(Text(str(error)))
        else:
            self.dismiss(True)


class RSSApp(App):
    TITLE = "RSS | 订阅与阅读"
    BINDINGS = [
        Binding("a", "add_feed", "添加源"), Binding("e", "edit_feed", "编辑源"),
        Binding("p", "toggle_feed", "暂停/启用"), Binding("delete", "remove_feed", "移除源"),
        Binding("o", "open_original", "原文"), Binding("r", "refresh_view", "刷新"),
        Binding("q", "quit_view", "退出"),
    ]
    CSS = """
    Screen { background: $background; }
    #connection { height: 2; padding: 0 1; background: $boost; }
    #layout { height: 1fr; }
    #sidebar { width: 28; min-width: 20; border: round $primary; padding: 0 1; }
    #sidebar-title { height: 1; text-style: bold; }
    #sources { height: 1fr; border: none; padding: 0; }
    #source-help { height: auto; color: $text-muted; }
    #main { width: 1fr; border: round $primary; }
    #list-heading { height: 1; padding: 0 1; text-style: bold; }
    #entries { height: 1fr; }
    #selection { height: 3; padding: 0 1; color: $text-muted; }
    #help { height: 1; padding: 0 1; color: $text-muted; }
    """

    def __init__(self, config, autostart=True):
        super().__init__()
        self.config = config
        self.autostart = autostart
        self.library = {"feeds": [], "entries": [], "running": False, "process": {}}
        self.selected_feed = "__all__"
        self.visible_entries = []
        self._source_signature = None
        self._table_signature = None
        self._refreshing = False
        self._starting = False
        self._ui_mounted = False
        self._view_error = ""

    def compose(self):
        yield Header()
        yield Static("正在读取本地记录…", id="connection")
        with Horizontal(id="layout"):
            with Vertical(id="sidebar"):
                yield Static("订阅源", id="sidebar-title")
                yield OptionList(id="sources")
                yield Static("A 添加 · E 编辑\nP 暂停/启用 · Del 移除", id="source-help")
            with Vertical(id="main"):
                yield Static("标题列表", id="list-heading")
                yield DataTable(id="entries", cursor_type="row", zebra_stripes=True, cell_padding=1)
                yield Static("", id="selection")
        yield Static("Tab 切换区域 · ↑↓ 选择 · Enter 阅读 · Esc 返回 · 关闭界面后后台继续监测", id="help")
        yield Footer()

    async def on_mount(self):
        self.source_list = self.query_one("#sources", OptionList)
        self.table = self.query_one("#entries", DataTable)
        self.connection = self.query_one("#connection", Static)
        self.heading = self.query_one("#list-heading", Static)
        self.selection = self.query_one("#selection", Static)
        self._ui_mounted = True
        await self.refresh_library()
        self.table.focus()
        self.set_interval(2, self.refresh_library)
        if self.autostart:
            self.run_worker(self.ensure_background(), name="start-background", exclusive=True)

    async def ensure_background(self):
        if self._starting:
            return
        self._starting = True
        try:
            await asyncio.to_thread(start_monitor, self.config, False)
        except (OSError, ValueError, RuntimeError) as error:
            self.notify(f"后台启动失败：{error}", severity="error", timeout=10)
        finally:
            self._starting = False
            await self.refresh_library()

    async def refresh_library(self):
        if self._refreshing:
            return
        self._refreshing = True
        try:
            updated = await asyncio.to_thread(read_library, self.config)
            # A momentary SQLite lock should not clear a user's selection or history.
            for source in updated["feeds"]:
                if source["error"]:
                    updated["entries"].extend(row for row in self.library["entries"] if row["feed_id"] == source["id"])
                    previous = next((row for row in self.library["feeds"] if row["id"] == source["id"]), {})
                    source["count"] = previous.get("count", 0)
            updated["entries"].sort(key=lambda row: (row["changed_at"], row["published"], row["row_id"]), reverse=True)
            self.library = updated
            self._view_error = ""
            if self.selected_feed != "__all__" and not any(row["id"] == self.selected_feed for row in updated["feeds"]):
                self.selected_feed = "__all__"
            self.render_sources()
            self.render_entries()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self._view_error = f"读取暂不可用，保留当前列表：{error}"
        finally:
            self._refreshing = False
            self.render_status()

    def render_sources(self):
        sources = self.library["feeds"]
        signature = [(row["id"], row["name"], row["enabled"], row["count"], bool(row["error"] or row["metadata"].get("last_error"))) for row in sources]
        if signature == self._source_signature:
            return
        self._source_signature = signature
        options = [Option(literal(f"全部来源  {sum(row['count'] for row in sources)}"), id="__all__")]
        for row in sources:
            state = "暂停" if not row["enabled"] else "异常" if row["error"] or row["metadata"].get("last_error") else "启用"
            options.append(Option(Text(f"{row['name']}\n{state} · {row['count']} 条", overflow="fold"), id=row["id"]))
        with self.source_list.prevent(OptionList.OptionHighlighted, OptionList.OptionSelected):
            self.source_list.clear_options()
            self.source_list.add_options(options)
            self.source_list.highlighted = next((i for i, option in enumerate(options) if option.id == self.selected_feed), 0)

    def current_entry(self):
        index = self.table.cursor_row
        return self.visible_entries[index] if 0 <= index < len(self.visible_entries) else None

    def render_entries(self, force=False):
        rows = [row for row in self.library["entries"] if self.selected_feed == "__all__" or row["feed_id"] == self.selected_feed]
        signature = (self.selected_feed, tuple(tuple(row.items()) for row in rows), self.table.size.width)
        if not force and signature == self._table_signature:
            return
        previous = self.current_entry()
        selected = previous["row_id"] if previous else None
        previous_row = self.table.cursor_row
        old_scroll = self.table.scroll_offset
        self._table_signature = signature
        self.visible_entries = rows
        width = self.table.size.width
        show_source = self.selected_feed == "__all__" and width >= 76
        show_time = width >= 44
        time_width = 16 if width >= 66 else 11
        with self.table.prevent(DataTable.RowHighlighted, DataTable.RowSelected):
            self.table.clear(columns=True)
            if show_time:
                self.table.add_column("源站时间", width=time_width)
            if show_source:
                self.table.add_column("来源", width=16)
            self.table.add_column("状态", width=8)
            used = (time_width + 2 if show_time else 0) + (18 if show_source else 0) + 12
            self.table.add_column("标题", width=max(14, width - used))
            for row in rows:
                status = STATUS_NAMES.get(row["status"], row["status"] or "—")
                color = "green" if row["status"] in ("resolved", "completed") else "yellow" if row["status"] else ""
                timestamp = display_time(row["published"])[:16]
                cells = [literal(timestamp if time_width == 16 else timestamp[5:])] if show_time else []
                if show_source:
                    cells.append(literal(row["feed_name"]))
                cells.extend([literal(status, color), literal(row["title"])])
                self.table.add_row(*cells, key=row["row_id"])
            matched = next((i for i, row in enumerate(rows) if row["row_id"] == selected), None)
            target = matched if matched is not None else 0
            if rows:
                self.table.move_cursor(row=target, column=0, scroll=False)
            scroll_y = max(0, old_scroll.y + target - previous_row) if matched is not None else 0
            self.table.scroll_to(x=old_scroll.x if matched is not None else 0, y=scroll_y, animate=False, force=True)
        label = next((row["name"] for row in self.library["feeds"] if row["id"] == self.selected_feed), "全部来源")
        self.heading.update(literal(f"{label} · {len(rows)} 条（每源最多显示 {ENTRY_LIMIT} 条）"))
        self.update_selection()

    def update_selection(self):
        row = self.current_entry()
        self.selection.update(Text(f"{row['feed_name']} · {row['title']}" if row else "暂无记录。首次抓取完成后会自动显示；也可按 A 添加订阅源。"))

    def render_status(self):
        running = self.library["running"]
        text = Text("● 后台监测中" if running else "○ 后台未运行 · 按 R 启动", style="green" if running else "yellow")
        text.append(f"  ·  {len(self.library['feeds'])} 个订阅源", style="")
        selected = next((row for row in self.library["feeds"] if row["id"] == self.selected_feed), None)
        errors = [self._view_error, self.library["process"].get("registry_error", "")]
        if selected:
            meta = selected["metadata"]
            text.append(f"  ·  最近成功 {display_time(meta.get('last_success', ''))}", style="")
            errors.extend([selected["error"], meta.get("last_error", ""), meta.get("notification_error", ""), meta.get("naming_error", "")])
            errors.append(self.library["process"].get("feed_errors", {}).get(selected["id"], ""))
        elif any(row["error"] or row["metadata"].get("last_error") for row in self.library["feeds"]):
            errors.append("部分源暂不可用；选择对应来源查看详情")
        error = next((error for error in errors if error), "")
        if error:
            text.append("\n" + " ".join(str(error).split()), style="yellow")
        self.connection.update(text)
        self.connection.tooltip = text.plain

    def on_resize(self):
        if self._ui_mounted:
            self.call_after_refresh(self.render_entries, True)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        if event.option_list is self.source_list:
            self.selected_feed = event.option.id
            self.render_entries()
            self.render_status()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option_list is self.source_list:
            self.table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted):
        self.update_selection()

    async def on_data_table_row_selected(self, event: DataTable.RowSelected):
        row = self.current_entry()
        if row:
            try:
                entry = await asyncio.to_thread(read_entry, self.config, row["feed_id"], row["key"])
                self.push_screen(EntryDetails(entry))
            except (OSError, ValueError, sqlite3.Error) as error:
                self.notify(str(error), severity="error")

    def on_main(self):
        return len(self.screen_stack) == 1

    def editable_source(self):
        if self.selected_feed == "__all__":
            self.notify("请先在左侧选择一个订阅源。")
            return None
        return self.selected_feed

    async def registry_for_edit(self):
        rows, version = await asyncio.to_thread(read_registry, self.config)
        if rows is None:
            raise ValueError("订阅管理需要 feeds.json 配置。")
        return rows, version

    async def after_registry_change(self, saved):
        if saved:
            self.notify("订阅已保存，后台会自动加载。")
            await self.refresh_library()

    async def action_add_feed(self):
        if not self.on_main():
            return
        try:
            rows, version = await self.registry_for_edit()
            self.push_screen(FeedEditor(self.config, rows, version), self.after_registry_change)
        except (OSError, ValueError) as error:
            self.notify(str(error), severity="error")

    async def action_edit_feed(self):
        if not self.on_main() or not (key := self.editable_source()):
            return
        try:
            rows, version = await self.registry_for_edit()
            if not any(row["id"] == key for row in rows):
                raise ValueError("该订阅已被移除，请刷新列表。")
            self.push_screen(FeedEditor(self.config, rows, version, key), self.after_registry_change)
        except (OSError, ValueError) as error:
            self.notify(str(error), severity="error")

    async def action_toggle_feed(self):
        if not self.on_main() or not (key := self.editable_source()):
            return
        try:
            rows, version = await self.registry_for_edit()
            row = next((row for row in rows if row["id"] == key), None)
            if row is None:
                raise ValueError("该订阅已被移除，请刷新列表。")
            row["enabled"] = not row.get("enabled", True)
            await asyncio.to_thread(save_registry, self.config, rows, version)
            await self.after_registry_change(True)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            self.notify(str(error), severity="error")

    async def action_remove_feed(self):
        if not self.on_main() or not (key := self.editable_source()):
            return
        try:
            rows, version = await self.registry_for_edit()
            if not any(row["id"] == key for row in rows):
                raise ValueError("该订阅已被移除，请刷新列表。")
            self.push_screen(RemoveFeed(self.config, rows, version, key), self.after_registry_change)
        except (OSError, ValueError) as error:
            self.notify(str(error), severity="error")

    async def action_open_original(self):
        if not self.on_main():
            return
        row = self.current_entry()
        if row:
            try:
                item = await asyncio.to_thread(read_entry, self.config, row["feed_id"], row["key"])
                url = safe_url(item["url"])
                if url:
                    await asyncio.to_thread(webbrowser.open, url)
            except (OSError, ValueError, sqlite3.Error) as error:
                self.notify(str(error), severity="error")

    async def action_refresh_view(self):
        if not self.on_main():
            return
        await self.refresh_library()
        if not self.library["running"]:
            self.run_worker(self.ensure_background(), name="start-background", exclusive=True)

    def action_quit_view(self):
        if self.on_main():
            self.exit()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="RSS reader and subscription manager")
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    args = parser.parse_args()
    RSSApp(load_config(args.config)).run()
