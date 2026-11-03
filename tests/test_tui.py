import asyncio
from hashlib import sha256
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Button, Input, Static

from rss_core import FetchResult, Store
from rss_feeds import load_feeds
from rss_reader import read_entry, read_library, read_registry, save_registry
from rss_report import export_reports
from rss_tui import EntryDetails, FeedEditor, RSSApp, RemoveFeed
from rss_update import load_config


class Fixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = self.root / "feeds.json"
        self.registry.write_text(json.dumps([
            {"id": "alpha", "name": "甲来源", "url": "https://alpha.example/feed.rss"},
            {"id": "beta", "name": "乙来源", "url": "https://beta.example/feed.rss", "enabled": False}
        ], ensure_ascii=False))
        (self.root / "config.json").write_text(json.dumps({"feeds_file": "feeds.json", "data_dir": "data", "notifications": False}))
        self.config = load_config(self.root / "config.json")
        self.seed("alpha", [self.item("1", "甲标题 [bold]原样保留[/bold]", "\n".join(f"正文第 {i} 行" for i in range(120)))])
        self.seed("beta", [self.item("1", "乙标题", "乙来源的完整正文。")])

    def tearDown(self):
        self.temp.cleanup()

    def item(self, guid, title, body="完整正文", date="Tue, 03 Nov 2026 16:58:16 +0800"):
        return f"<item><guid>{guid}</guid><title>{escape(title)}</title><pubDate>{date}</pubDate><link>https://example.com/{guid}</link><description>{escape(body)}</description></item>"

    def seed(self, feed_id, items, now="2026-11-03T09:00:00+00:00", channel_time=""):
        spec = next(spec for spec in load_feeds(self.config) if spec.id == feed_id)
        store = Store(spec.config.data_dir, spec.config.feed_url)
        try:
            data = ("<rss><channel><title>示例来源</title><lastBuildDate>" + channel_time + "</lastBuildDate>" + "".join(items) + "</channel></rss>").encode()
            store.record_success(FetchResult(data), now=now)
            export_reports(store, spec.config.data_dir)
        finally:
            store.close()


class ReaderTests(Fixture, unittest.TestCase):
    def test_reads_full_body_without_changing_database_or_notification_state(self):
        paths = [self.root / "data" / key / "state.sqlite3" for key in ("alpha", "beta")]
        before = [sha256(path.read_bytes()).hexdigest() for path in paths]
        library = read_library(self.config)
        self.assertEqual(len(library["entries"]), 2)
        self.assertFalse(library["feeds"][1]["enabled"])
        entry = next(row for row in library["entries"] if row["feed_id"] == "alpha")
        detail = read_entry(self.config, "alpha", entry["key"])
        self.assertIn("正文第 119 行", detail["body"])
        self.assertEqual(before, [sha256(path.read_bytes()).hexdigest() for path in paths])

    def test_stale_editor_does_not_overwrite_newer_registry(self):
        rows, version = read_registry(self.config)
        changed = json.loads(self.registry.read_text())
        changed[0]["name"] = "外部已修改"
        self.registry.write_text(json.dumps(changed))
        rows[0]["name"] = "过期窗口"
        with self.assertRaisesRegex(ValueError, "其它窗口"):
            save_registry(self.config, rows, version)
        self.assertEqual(json.loads(self.registry.read_text())[0]["name"], "外部已修改")

    def test_remove_preserves_archive_and_rebinding_url_is_rejected(self):
        rows, version = read_registry(self.config)
        original = self.root / "data" / "alpha" / "state.sqlite3"
        rows[0]["url"] = "https://another.example/rss"
        with self.assertRaisesRegex(ValueError, "新的源 ID"):
            save_registry(self.config, rows, version)
        rows, version = read_registry(self.config)
        save_registry(self.config, rows[1:], version)
        self.assertTrue(original.exists())
        self.assertTrue(list((self.root / "data/alpha/archive").rglob("*.md")))

    def test_unfetched_source_does_not_create_empty_database(self):
        rows, version = read_registry(self.config)
        rows.append({"id": "empty", "name": "新来源", "url": "https://empty.example/rss"})
        save_registry(self.config, rows, version)
        library = read_library(self.config)
        self.assertEqual(library["feeds"][-1]["count"], 0)
        self.assertFalse((self.root / "data/empty/state.sqlite3").exists())


class TUITests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Some restricted Linux runners cannot wake the loop through its socket
        # from executor threads. A timer also keeps executor shutdown observable.
        loop = asyncio.get_running_loop()
        def heartbeat():
            loop.call_later(0.05, heartbeat)
        loop.call_later(0.05, heartbeat)

    async def test_channel_time_update_appears_as_program_notice_in_list_and_detail(self):
        items = [self.item("1", "原始事件标题", "原文没有变化")]
        self.seed("alpha", items, channel_time="Tue, 03 Nov 2026 18:00:00 +0800")
        self.seed("alpha", items, now="2026-11-03T10:25:00+00:00", channel_time="Tue, 03 Nov 2026 18:21:00 +0800")
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            self.assertEqual(app.table.row_count, 3)
            self.assertEqual(app.current_entry()["status"], "feed_updated")
            app.table.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, EntryDetails)
            self.assertIn("程序提示", app.screen.entry["body"])
            self.assertIn("程序提示", str(app.screen.query_one("#details-hint", Static).render()))
            self.assertEqual(app.screen.entry["kind"], "feed_time_updated")

    async def test_select_source_enter_full_body_scroll_and_back(self):
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            self.assertEqual(app.table.row_count, 2)
            self.assertEqual(app.source_list.option_count, 3)
            app.source_list.focus()
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertEqual(app.selected_feed, "alpha")
            self.assertEqual(app.table.row_count, 1)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, EntryDetails)
            self.assertIn("正文第 119 行", app.screen.entry["body"])
            self.assertIn("[bold]原样保留[/bold]", app.screen.entry["title"])
            await pilot.press("pagedown")
            await pilot.pause()
            self.assertGreater(app.screen.query_one("#details-scroll").scroll_y, 0)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.current_entry()["feed_id"], "alpha")
            app.save_screenshot("/tmp/rss-tui-preview.svg")

    async def test_new_updates_keep_selection_and_open_detail_snapshot(self):
        self.seed("alpha", [self.item(str(i), f"标题 {i}") for i in range(60)])
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            app.table.move_cursor(row=25)
            await pilot.pause()
            selected = app.current_entry()["row_id"]
            await pilot.press("enter")
            await pilot.pause()
            detail = app.screen.entry.copy()
            self.seed("alpha", [self.item("new", "刚到达的新标题")], now="2026-11-04T09:00:00+00:00")
            await app.refresh_library()
            await pilot.pause()
            self.assertEqual(app.current_entry()["row_id"], selected)
            self.assertEqual(app.screen.entry, detail)
            await pilot.press("escape")
            self.assertEqual(app.current_entry()["row_id"], selected)

    async def test_add_and_edit_source(self):
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, FeedEditor)
            app.screen.query_one("#feed-id", Input).value = "gamma"
            app.screen.query_one("#feed-name", Input).value = "新增来源"
            app.screen.query_one("#feed-url", Input).value = "https://gamma.example/feed.rss"
            await pilot.click("#form-save")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual([row["id"] for row in json.loads(self.registry.read_text())], ["alpha", "beta", "gamma"])
            app.source_list.highlighted = 3
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertTrue(app.screen.query_one("#feed-id", Input).disabled)
            self.assertTrue(app.screen.query_one("#feed-url", Input).disabled)
            app.screen.query_one("#feed-name", Input).value = "修改后的名称"
            app.screen.query_one("#feed-interval", Input).value = "300"
            await pilot.press("ctrl+s")
            await pilot.pause()
            saved = json.loads(self.registry.read_text())[-1]
            self.assertEqual(saved["name"], "修改后的名称")
            self.assertEqual(saved["poll_interval_seconds"], 300)

    async def test_pause_remove_cancel_and_preserve_records(self):
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(100, 32)) as pilot:
            app.source_list.highlighted = 1
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertFalse(json.loads(self.registry.read_text())[0]["enabled"])
            self.assertEqual(app.table.row_count, 1)
            await pilot.press("delete")
            await pilot.pause()
            self.assertIsInstance(app.screen, RemoveFeed)
            await pilot.press("escape")
            self.assertEqual(len(json.loads(self.registry.read_text())), 2)
            await pilot.press("delete")
            await pilot.pause()
            await pilot.click("#remove-confirm")
            await pilot.pause()
            self.assertEqual([row["id"] for row in json.loads(self.registry.read_text())], ["beta"])
            self.assertTrue((self.root / "data/alpha/state.sqlite3").exists())
            self.assertEqual(app.selected_feed, "__all__")

    async def test_invalid_external_registry_keeps_current_view_and_recovers(self):
        app = RSSApp(self.config, autostart=False)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            saved = self.registry.read_text()
            self.registry.write_text("[")
            await app.refresh_library()
            self.assertEqual(app.table.row_count, 2)
            self.assertIn("保留当前列表", app._view_error)
            self.registry.write_text(saved)
            await app.refresh_library()
            self.assertFalse(app._view_error)
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, FeedEditor)
            self.assertGreater(app.screen.query_one("#form-save", Button).region.height, 0)
            await pilot.press("escape")
            app.save_screenshot("/tmp/rss-tui-small.svg")

    async def test_startup_reuses_background_and_quit_does_not_stop_it(self):
        app = RSSApp(self.config)
        with patch("rss_tui.start_monitor", return_value=0) as start, patch("rss_update.stop_monitor") as stop:
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                start.assert_called_once_with(self.config, False)
                await pilot.press("q")
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
