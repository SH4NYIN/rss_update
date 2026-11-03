from html import escape
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rss_core import FetchResult, Store, parse_feed
from rss_notifications import deliver_pending
from rss_reader import read_entry, read_library
from rss_report import export_reports
from rss_update import Config, check_once


URL = "https://status.example.test/feed.rss"
T0 = "Tue, 03 Nov 2026 18:00:00 +0800"
T1 = "Tue, 03 Nov 2026 18:21:58 +0800"
T2 = "Tue, 03 Nov 2026 18:25:37 +0800"


def snapshot(updated=T0, published=T0, status="monitoring", channel_date="", guid="incident-1"):
    description = f"<p>Status: {status}</p><p>已实施修复，正在监控结果。</p>"
    return f"""<rss version="2.0"><channel>
      <title>DeepSeek 示例</title><link>https://status.example.test</link>
      <lastBuildDate>{updated}</lastBuildDate><pubDate>{channel_date}</pubDate>
      <item><guid>{guid}</guid><title>服务部分中断</title>
      <link>https://status.example.test/incidents/1</link><pubDate>{published}</pubDate>
      <description>{escape(description)}</description></item>
    </channel></rss>""".encode()


class TimestampTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.store = Store(self.directory, URL)
        self.addCleanup(lambda: self.store.close())
        self.config = Config(self.directory / "config.json", URL, 120, 5, self.directory, True)

    def ingest(self, data):
        return self.store.record_success(FetchResult(data))

    def test_channel_only_change_alerts_without_claiming_an_incident_status(self):
        self.ingest(snapshot())
        self.assertFalse(self.store.pending())
        self.assertEqual(self.ingest(snapshot(T1)), 1)
        row = self.store.pending()[0]
        self.assertEqual(row["kind"], "feed_time_updated")
        self.assertEqual(row["status"], "feed_updated")
        self.assertEqual(row["url"], "https://status.example.test")
        self.assertIn("程序提示", row["body"])
        self.assertIn("18:21:58", row["body"])
        sent = []
        self.assertEqual(deliver_pending(self.store, sender=lambda *args: sent.append(args)), 1)
        self.assertIn("正文未变", sent[0][1])
        self.assertIn("请打开源站确认", sent[0][1])
        original = next(item for item in self.store.snapshot()["items"] if item["status"] != "feed_updated")
        self.assertEqual(original["status"], "monitoring")
        self.assertEqual(original["published"], "2026-11-03T18:00:00+08:00")

    def test_channel_timestamp_deduplicates_across_restart_304_and_stale_cache(self):
        self.ingest(snapshot())
        self.ingest(snapshot(T1))
        deliver_pending(self.store, sender=lambda *args: None)
        self.store.close()
        self.store = Store(self.directory, URL)
        self.assertEqual(self.store.record_success(FetchResult(None)), 0)
        for raw in (snapshot(T1), snapshot(T0), snapshot(T1), snapshot("Tue, 03 Nov 2026 10:21:58 GMT")):
            self.assertEqual(self.ingest(raw), 0)
        self.assertEqual(self.ingest(snapshot(T2)), 1)
        notices = [row for row in self.store.snapshot()["items"] if row["status"] == "feed_updated"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.store.pending()[0]["item_key"], notices[0]["key"])

    def test_item_date_only_update_preserves_text_and_gets_explicit_notification(self):
        self.ingest(snapshot())
        old = self.store.snapshot()["items"][0]
        self.assertEqual(self.ingest(snapshot(T1, published=T1)), 1)
        self.assertEqual(len(self.store.snapshot()["items"]), 1)
        row = self.store.pending()[0]
        self.assertEqual(row["kind"], "time_updated")
        self.assertEqual(row["body"], old["body"])
        self.assertEqual(row["status"], "monitoring")
        sent = []
        deliver_pending(self.store, sender=lambda *args: sent.append(args))
        self.assertIn("时间更新（正文未变）", sent[0][1])
        self.assertNotIn("监控中", sent[0][1])
        self.assertEqual(self.ingest(snapshot(T1, published="Tue, 03 Nov 2026 10:21:58 GMT")), 0)
        self.assertEqual(self.ingest(snapshot(T1, published=T0)), 0)
        self.assertEqual(self.ingest(snapshot(T1, published=T1)), 0)

    def test_content_change_and_channel_time_share_one_normal_update(self):
        self.ingest(snapshot())
        self.assertEqual(self.ingest(snapshot(T1, status="resolved")), 1)
        self.assertEqual(self.store.pending()[0]["kind"], "updated")
        deliver_pending(self.store, sender=lambda *args: None)
        self.assertEqual(self.ingest(snapshot(T1, status="resolved")), 0)
        self.assertEqual(self.ingest(snapshot(T2, status="resolved")), 1)
        self.assertEqual(self.store.pending()[0]["kind"], "feed_time_updated")

    def test_item_timestamp_previously_seen_before_content_reversion_does_not_realert(self):
        self.ingest(snapshot())
        self.ingest(snapshot(published=T1))
        self.ingest(snapshot(published=T0, status="resolved"))
        deliver_pending(self.store, sender=lambda *args: None)
        self.assertEqual(self.ingest(snapshot(published=T1, status="resolved")), 0)
        self.assertFalse(self.store.pending())

    def test_upgrade_fetches_full_body_then_silently_establishes_channel_baseline(self):
        self.ingest(snapshot())
        with self.store.db:
            self.store.db.execute("DELETE FROM metadata WHERE key IN ('feed_timestamp','timestamp_tracking')")
            self.store.set("etag", '"old"')
            self.store.set("last_modified", T0)
        with patch("rss_update.fetch_feed", return_value=FetchResult(snapshot(T1), '"new"', T1)) as fetch:
            self.assertTrue(check_once(self.config, self.store))
            fetch.assert_called_once_with(URL, 5, "", "")
        self.assertFalse(self.store.pending())
        with patch("rss_update.fetch_feed", return_value=FetchResult(None)) as fetch:
            self.assertTrue(check_once(self.config, self.store))
            fetch.assert_called_once_with(URL, 5, '"new"', T1)
        self.assertEqual(self.ingest(snapshot(T2)), 1)

    def test_invalid_missing_or_timezone_less_dates_do_not_trigger(self):
        self.ingest(snapshot())
        for value in ("", "invalid", "Tue, 03 Nov 2026 19:00:00"):
            self.assertEqual(self.ingest(snapshot(value, published=value)), 0)
        self.assertEqual(self.ingest(snapshot(T1)), 1)

    def test_channel_pubdate_also_works_and_first_date_is_silent(self):
        self.ingest(snapshot(""))
        self.assertEqual(self.ingest(snapshot("", channel_date=T0)), 0)
        self.assertEqual(self.ingest(snapshot("", channel_date=T1)), 1)
        self.assertEqual(parse_feed(snapshot(T0, channel_date=T2), URL).updated, "2026-11-03T10:25:37+00:00")

    def test_time_notice_delivery_failure_is_durable_and_disabled_notifications_stay_disabled(self):
        self.ingest(snapshot())
        self.ingest(snapshot(T1))
        def fail(*args):
            raise RuntimeError("desktop unavailable")
        self.assertEqual(deliver_pending(self.store, sender=fail), 0)
        self.store.close()
        self.store = Store(self.directory, URL)
        self.store.record_success(FetchResult(None))
        self.assertEqual(deliver_pending(self.store, sender=lambda *args: None), 1)
        self.ingest(snapshot(T2))
        deliver_pending(self.store, enabled=False, sender=fail)
        self.assertFalse(self.store.pending())
        self.assertEqual(self.ingest(snapshot(T2)), 0)

    def test_time_notice_is_readable_in_library_and_markdown_as_program_note(self):
        self.ingest(snapshot())
        self.ingest(snapshot(T1))
        export_reports(self.store, self.directory)
        library = read_library(self.config)
        notice = next(row for row in library["entries"] if row["status"] == "feed_updated")
        detail = read_entry(self.config, "default", notice["key"])
        self.assertEqual(detail["kind"], "feed_time_updated")
        self.assertIn("程序提示", detail["body"])
        files = list((self.directory / "archive").rglob("*.md"))
        self.assertEqual(len(files), 2)
        document = next(path.read_text(encoding="utf-8") for path in files if notice["key"][:12] in path.name)
        self.assertIn("来源时间更新（程序提示）", document)
        self.assertIn("提示标题", document)
        self.assertNotIn("原始标题", document)


if __name__ == "__main__":
    unittest.main()
