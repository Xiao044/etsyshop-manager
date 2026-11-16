"""All / On sale collection, additive migrations, analytics and merged cards."""

import hashlib
import importlib.util
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "releases/v11"))
sys.path.insert(0, str(ROOT / "releases/v22"))
import collector
from etsy_runtime.schema import ensure_item_counts_schema
from shop_metrics import build_item_counts, build_shop_data

SCHEMA = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")


def page(sections="All 45\nOn sale 30"):
    return {"title": "EnCeramic - Etsy", "text": "EnCeramic\n4.9 (314)\n2,900 Sales\n678 Admirers\n"
            + sections + "\nReviews\nSort by: Suggested\n4.9 (314)"}


def snapshot(day, all_count=45, sale_count=30, simulated=0):
    return {"id": 1, "day": day, "collected_at": day + "T08:00:00+00:00", "is_simulated": simulated,
            "sales": None, "admirers": None, "item_count": all_count, "on_sale_count": sale_count,
            "rating": None, "review_count": None}


def paired(rows):
    return build_item_counts(rows, "2026-11-10", "2026-11-16")


class ProductParserTests(unittest.TestCase):
    def test_both_counts_are_independently_parsed(self):
        values, status = collector.parse(page(), "EnCeramic")
        self.assertEqual(status, "success")
        self.assertEqual((values["item_count"], values["on_sale_count"]), (45, 30))

    def test_newline_comma_and_case_variations(self):
        values, status = collector.parse(page("ALL\n1,245\non SALE\n1,030"), "EnCeramic")
        self.assertEqual(status, "success")
        self.assertEqual((values["item_count"], values["on_sale_count"]), (1245, 1030))

    def test_zero_is_real_data_not_missing(self):
        values, status = collector.parse(page("All 45\nOn sale 0"), "EnCeramic")
        self.assertEqual(status, "success")
        self.assertEqual(values["on_sale_count"], 0)

    def test_hidden_sale_count_is_unknown_and_partial(self):
        values, status = collector.parse(page("All 45"), "EnCeramic")
        self.assertEqual(status, "partial")
        self.assertIsNone(values["on_sale_count"])
        self.assertEqual(values["item_count"], 45)

    def test_missing_all_does_not_use_sale_as_its_total(self):
        values, status = collector.parse(page("On sale 30"), "EnCeramic")
        self.assertEqual(status, "partial")
        self.assertIsNone(values["item_count"])
        self.assertEqual(values["on_sale_count"], 30)

    def test_counts_can_follow_a_long_shop_header(self):
        values, _ = collector.parse(page("Announcement " + "details " * 150 + "\nAll\n45\nOn sale\n30"), "EnCeramic")
        self.assertEqual((values["item_count"], values["on_sale_count"]), (45, 30))

    def test_titles_and_discount_percentages_are_not_sidebar_counts(self):
        values, status = collector.parse(page("All 45\nOn sale 50% off\nCustom mug On sale 80\nAll 90 products"), "EnCeramic")
        self.assertEqual(status, "partial")
        self.assertEqual(values["item_count"], 45)
        self.assertIsNone(values["on_sale_count"])


class SnapshotMigrationTests(unittest.TestCase):
    def test_old_rows_remain_intact_with_unknown_sale_counts(self):
        with closing(sqlite3.connect(":memory:")) as db, db:
            db.executescript(SCHEMA.replace("            on_sale_count INTEGER,\n", ""))
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'EnCeramic','enceramic','https://www.etsy.com/shop/EnCeramic','now')")
            db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(1,'now','completed',1)")
            db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,item_count) VALUES(1,1,'2026-11-15T08:00:00+00:00','success',45)")
            ensure_item_counts_schema(db)
            ensure_item_counts_schema(db)
            self.assertEqual(db.execute("SELECT item_count,on_sale_count FROM shop_snapshots").fetchone(), (45, None))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_no_snapshot_table_is_a_safe_noop(self):
        with closing(sqlite3.connect(":memory:")) as db:
            ensure_item_counts_schema(db)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='shop_snapshots'").fetchone())

    def test_collector_migrates_and_persists_both_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            path = data / "shops.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(SCHEMA.replace("            on_sale_count INTEGER,\n", ""))
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'EnCeramic','enceramic','https://www.etsy.com/shop/EnCeramic','now')")
            with mock.patch.multiple(collector, DATA_DIR=data, DB_PATH=path), \
                 mock.patch.object(collector, "launch_browser", return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
                 mock.patch.object(collector, "close_browser"), \
                 mock.patch.object(collector, "collect", return_value=collector.parse(page(), "EnCeramic")), \
                 redirect_stdout(io.StringIO()):
                collector.main(["--shop-id", "1", "--skip-reviews"])
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute("SELECT item_count,on_sale_count,status FROM shop_snapshots").fetchone(), (45, 30, "success"))
                self.assertEqual(db.execute("SELECT item_count,on_sale_count FROM collection_results").fetchone(), (45, 30))


class PairedMetricTests(unittest.TestCase):
    def test_independent_latest_totals_and_daily_changes(self):
        counts = paired([snapshot("2026-11-15", 40, 32), snapshot("2026-11-16", 45, 30)])
        all_items, sale = counts["series"]
        self.assertEqual((all_items["total_value"], sale["total_value"]), (45, 30))
        self.assertEqual((all_items["today_value"], sale["today_value"]), (5, -2))
        self.assertEqual((all_items["stat_value"], sale["stat_value"]), (5, -2))
        self.assertIn("All：5 · On sale：-2", counts["chart_days"][0]["formatted"])

    def test_sale_zero_and_negative_delta(self):
        counts = paired([snapshot("2026-11-15", 40, 5), snapshot("2026-11-16", 40, 0)])
        self.assertEqual(counts["series"][0]["today_value"], 0)
        self.assertEqual(counts["series"][1]["today_value"], -5)
        self.assertEqual(counts["series"][1]["total_value"], 0)

    def test_historical_missing_sale_count_is_not_zero_or_all(self):
        counts = paired([snapshot("2026-11-15", 40, None), snapshot("2026-11-16", 45, 30)])
        self.assertEqual(counts["series"][0]["today_value"], 5)
        self.assertIsNone(counts["series"][1]["today_value"])
        self.assertEqual(counts["series"][1]["total_value"], 30)
        self.assertEqual(counts["chart_days"][0]["parts"][1]["formatted"], "—")
        legacy = snapshot("2026-11-15")
        del legacy["on_sale_count"]
        self.assertIsNone(paired([legacy])["series"][1]["total_value"])

    def test_gap_breaks_each_line_and_does_not_use_other_series_baseline(self):
        counts = paired([snapshot("2026-11-10", 40, 10), snapshot("2026-11-11", 42, 12),
                         snapshot("2026-11-12", None, 13), snapshot("2026-11-13", 46, None),
                         snapshot("2026-11-14", 47, 15), snapshot("2026-11-15", 48, 17)])
        all_items, sale = counts["series"]
        self.assertEqual(all_items["points"].count("M"), 2)
        self.assertEqual(sale["points"].count("M"), 2)
        self.assertEqual([p["day"] for p in all_items["chart_days"]], ["2026-11-11", "2026-11-14", "2026-11-15"])
        self.assertEqual([p["day"] for p in sale["chart_days"]], ["2026-11-11", "2026-11-12", "2026-11-15"])
        self.assertIsNone(all_items["today_value"])
        self.assertIsNone(sale["today_value"])
        self.assertEqual(all_items["total_day"], "2026-11-15")

    def test_both_lines_use_one_scale(self):
        counts = paired([snapshot("2026-11-15", 45, 5), snapshot("2026-11-16", 47, 30)])
        self.assertEqual(counts["series"][0]["axis_ticks"], counts["series"][1]["axis_ticks"])
        self.assertGreater(counts["series"][0]["chart_days"][0]["y"], counts["series"][1]["chart_days"][0]["y"])

    def test_simulated_baselines_and_today_totals_are_distinguished(self):
        counts = paired([snapshot("2026-11-15", 40, 10, 1), snapshot("2026-11-16", 45, 30)])
        for item in counts["series"]:
            self.assertTrue(item["today_simulated"])
            self.assertFalse(item["total_simulated"])
        self.assertEqual(counts["chart_days"][0]["kind"].count("模拟数据"), 2)

    def test_single_shop_keeps_seven_cards_and_puts_both_values_in_one_cell(self):
        data = build_shop_data([snapshot("2026-11-15", 40, 32), snapshot("2026-11-16", 45, 30)],
                               [], [], "2026-11-10", "2026-11-16")
        self.assertEqual(len(data["metrics"]), 7)
        item = next(card for card in data["metrics"] if card["key"] == "item_count")
        self.assertEqual(len(item["item_counts"]["series"]), 2)
        cell = next(cell for cell in data["table_rows"][0]["cells"] if cell["key"] == "item_count")
        self.assertEqual([part["value"] for part in cell["parts"]], [5, -2])

    def test_sale_only_data_counts_as_collected_data(self):
        data = build_shop_data([snapshot("2026-11-15", None, 32), snapshot("2026-11-16", None, 30)],
                               [], [], "2026-11-10", "2026-11-16")
        self.assertTrue(data["has_data"])
        self.assertTrue(data["table_rows"][0]["has_collected"])
        self.assertFalse(data["table_rows"][0]["has_simulated"])

    def test_real_snapshot_wins_and_future_counts_are_not_used(self):
        rows = [snapshot("2026-11-15", 40, 32), snapshot("2026-11-16", 45, 30),
                snapshot("2026-11-16", 100, 80, 1), snapshot("2026-11-17", 120, 90)]
        data = build_shop_data(rows, [], [], "2026-11-10", "2026-11-16")
        item = next(card for card in data["metrics"] if card["key"] == "item_count")
        self.assertEqual([series["total_value"] for series in item["item_counts"]["series"]], [45, 30])
        self.assertEqual([series["today_value"] for series in item["item_counts"]["series"]], [5, -2])


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        stamp = cls(2026, 11, 16, 8, tzinfo=timezone.utc)
        return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)


class ProductCardRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name)
        cls.database = cls.data / "shops.sqlite3"
        # Web startup must migrate before either read-only analytics route runs.
        with closing(sqlite3.connect(cls.database)) as db, db:
            db.executescript(SCHEMA.replace("            on_sale_count INTEGER,\n", ""))
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'EnCeramic','enceramic','https://www.etsy.com/shop/EnCeramic','now')")
            for run, day, all_count in ((1, "2026-11-15", 40), (2, "2026-11-16", 45)):
                db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(?,?,'completed',1)", (run, day))
                db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,item_count) VALUES(?,1,?,'success',?)", (run, day + "T08:00:00+00:00", all_count))
        spec = importlib.util.spec_from_file_location("paired_products_web", ROOT / "releases/v22/app.py")
        cls.web = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.web
        with mock.patch.dict(os.environ, {"ETSY_TOOLS_DATA_DIR": str(cls.data)}):
            spec.loader.exec_module(cls.web)
        cls.web.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("paired_products_web", None)
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.web.app.test_client()
        clock = mock.patch.object(self.web, "datetime", FixedDatetime)
        clock.start()
        self.addCleanup(clock.stop)
        with self.web.connect_db() as db:
            db.execute("UPDATE shop_snapshots SET on_sale_count=CASE run_id WHEN 1 THEN 32 WHEN 2 THEN 30 END")

    def get(self, path):
        response = self.client.get(path, base_url="http://127.0.0.1:8765")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_single_shop_merged_overview_and_chart(self):
        html = self.get("/shops/1/data?days=7")
        self.assertEqual(html.count('class="metric-overview-card '), 7)
        self.assertEqual(html.count('class="chart-card metric-chart '), 7)
        overview = html.split('class="metric-overview-card metric-item_count"')[1].split('</a>')[0]
        self.assertIn('data-counter="item_count"', overview)
        self.assertIn('data-counter="on_sale_count"', overview)
        self.assertIn('<strong>45</strong>', overview)
        self.assertIn('<strong>30</strong>', overview)
        self.assertIn('>+5</strong>', overview)
        self.assertIn('>-2</strong>', overview)
        self.assertEqual(html.count('data-series="item_count"'), 1)
        self.assertEqual(html.count('data-series="on_sale_count"'), 1)
        self.assertIn('All 与 On sale', html)
        self.assertNotIn('daily-data-table', html)
        self.assertIn('All：5 · On sale：-2', html)

    def test_dashboard_uses_one_card_per_shop_for_both_counts(self):
        html = self.get("/dashboard?metric=item_count&days=7")
        self.assertEqual(html.count('class="chart-card product-card"'), 1)
        self.assertEqual(html.count('data-counter="item_count"'), 1)
        self.assertEqual(html.count('data-counter="on_sale_count"'), 1)
        self.assertEqual(html.count('data-series="on_sale_count"'), 1)
        self.assertIn('促销商品', html)
        self.assertIn('<span>截至</span> <time datetime="2026-11-16">2026-11-16</time>', html)

    def test_legacy_missing_sale_is_explicit_and_routes_are_read_only(self):
        with self.web.connect_db() as db:
            db.execute("UPDATE shop_snapshots SET on_sale_count=NULL")
        before = hashlib.sha256(self.database.read_bytes()).digest()
        for path in ("/shops/1/data?days=7", "/dashboard?metric=item_count&days=7"):
            html = self.get(path)
            sale = html.split('data-counter="on_sale_count"')[1].split('</div>\n  </div>')[0]
            self.assertIn('尚未采集', sale)
            self.assertIn('product-missing">—</strong>', sale)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())


if __name__ == "__main__":
    unittest.main()
