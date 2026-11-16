"""Single-shop calculations and Flask routes, using only temporary SQLite data."""

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "releases/v22"))
from shop_metrics import METRICS, build_shop_data, format_value


def snapshot(day, sales=100, simulated=0, **overrides):
    return {"id": 1, "day": day, "collected_at": day + "T10:00:00+00:00", "is_simulated": simulated,
            "sales": sales, "admirers": 20, "item_count": 10, "rating": 4.9, "review_count": 30, **overrides}


def finance(day, sales, profit=20, marketing=10, **overrides):
    return {"day": day, "sales_usd": sales, "net_profit_usd": profit, "marketing_usd": marketing,
            "is_simulated": 1, **overrides}


def build(snapshots=(), finances=(), bad=(), first="2026-11-10", last="2026-11-16", review_available=False):
    return build_shop_data(snapshots, finances, bad, first, last, review_available)


def card(data, key):
    return next(item for item in data["metrics"] if item["key"] == key)


def cell(data, day, key):
    row = next(row for row in data["table_rows"] if row["day"] == day)
    return next(value for value in row["cells"] if value["key"] == key)


class ShopMetricTests(unittest.TestCase):
    def test_seven_metrics_and_complete_descending_calendar(self):
        data = build()
        self.assertEqual([item["key"] for item in data["metrics"]], [item["key"] for item in METRICS])
        self.assertEqual(len(data["metrics"]), 7)
        self.assertEqual(len(data["table_rows"]), 7)
        self.assertEqual(data["table_rows"][0]["day"], "2026-11-16")
        self.assertEqual(data["table_rows"][-1]["day"], "2026-11-10")
        self.assertFalse(data["has_data"])
        self.assertTrue(all(value["value"] is None for row in data["table_rows"] for value in row["cells"]))

    def test_real_snapshot_wins_even_when_simulated_one_is_later(self):
        data = build([snapshot("2026-11-09", 95), snapshot("2026-11-10", 100),
                      snapshot("2026-11-10", 900, 1, collected_at="2026-11-10T23:00:00+00:00")])
        self.assertEqual(card(data, "sales")["current"], 100)
        self.assertEqual(cell(data, "2026-11-10", "sales")["value"], 5)
        self.assertFalse(cell(data, "2026-11-10", "sales")["simulated"])

    def test_latest_real_snapshot_within_same_day_is_used(self):
        data = build([snapshot("2026-11-09", 90), snapshot("2026-11-10", 100),
                      snapshot("2026-11-10", 105, collected_at="2026-11-10T11:00:00+00:00")])
        self.assertEqual(cell(data, "2026-11-10", "sales")["value"], 15)

    def test_previous_day_outside_window_is_used_as_baseline(self):
        data = build([snapshot("2026-11-09", 99), snapshot("2026-11-10", 105)])
        self.assertEqual(cell(data, "2026-11-10", "sales")["value"], 6)
        self.assertIsNone(cell(data, "2026-11-11", "sales")["value"])

    def test_missing_dates_and_null_metrics_are_not_zero_or_bridged(self):
        data = build([snapshot("2026-11-09", 90), snapshot("2026-11-10", 100),
                      snapshot("2026-11-11", None), snapshot("2026-11-12", 120), snapshot("2026-11-13", 125)])
        points = card(data, "sales")["chart_days"]
        self.assertEqual([(point["day"], point["value"]) for point in points], [("2026-11-10", 10), ("2026-11-13", 5)])
        self.assertEqual(card(data, "sales")["points"].count("M"), 2)
        self.assertNotIn("L", card(data, "sales")["points"])
        self.assertIsNone(cell(data, "2026-11-12", "sales")["value"])
        self.assertEqual(points[0]["x"], 80)
        self.assertEqual(points[1]["x"], 398)

    def test_simulated_baseline_marks_real_day_delta_as_simulated(self):
        data = build([snapshot("2026-11-09", 95, 1), snapshot("2026-11-10", 100)])
        self.assertTrue(cell(data, "2026-11-10", "sales")["simulated"])
        self.assertFalse(cell(data, "2026-11-10", "rating")["simulated"])
        self.assertFalse(card(data, "sales")["current_simulated"])
        row = data["table_rows"][-1]
        self.assertTrue(row["has_collected"] and row["has_simulated"])

    def test_negative_deltas_and_zero_are_preserved(self):
        data = build([snapshot("2026-11-09", 100), snapshot("2026-11-10", 95), snapshot("2026-11-11", 95)])
        self.assertEqual(cell(data, "2026-11-10", "sales")["value"], -5)
        self.assertEqual(cell(data, "2026-11-11", "sales")["value"], 0)

    def test_rating_is_not_a_delta(self):
        data = build([snapshot("2026-11-10", rating=4.8), snapshot("2026-11-11", rating=4.9)])
        self.assertEqual(cell(data, "2026-11-10", "rating")["formatted"], "4.80")
        self.assertAlmostEqual(card(data, "rating")["stat_value"], 4.85)

    def test_future_values_do_not_change_latest_totals(self):
        data = build([snapshot("2026-11-16", 100), snapshot("2026-11-17", 999)],
                     [finance("2026-11-01", 30), finance("2026-11-17", 999)], ["2026-11-17"])
        self.assertEqual(card(data, "sales")["current"], 100)
        self.assertEqual(card(data, "daily_sales")["current"], 30)
        self.assertIsNone(card(data, "bad_review_count")["current"])

    def test_finance_uses_daily_changes_month_reset_and_monthly_ratio(self):
        data = build(finances=[finance("2026-10-31", 3000, 900, 300), finance("2026-11-01", 100, 40, 10),
                               finance("2026-11-02", 160, 50, 24)], first="2026-11-01", last="2026-11-07")
        self.assertEqual(cell(data, "2026-11-01", "daily_sales")["value"], 100)
        self.assertEqual(cell(data, "2026-11-02", "daily_sales")["value"], 60)
        chart = card(data, "daily_sales")
        self.assertEqual(chart["finance_rows"][1]["net_profit_usd"], 10)
        self.assertEqual(chart["finance_rows"][1]["marketing_ratio"], 15)
        self.assertEqual(chart["stat_value"], 160)
        self.assertTrue(chart["current_simulated"])

    def test_finance_missing_baseline_is_a_gap_but_ratio_is_available(self):
        data = build(finances=[finance("2026-11-10", 100), finance("2026-11-12", 200)])
        self.assertIsNone(cell(data, "2026-11-10", "daily_sales")["value"])
        self.assertIsNone(cell(data, "2026-11-12", "daily_sales")["value"])
        self.assertTrue(card(data, "daily_sales")["has_finance"])
        self.assertIsNone(card(data, "daily_sales")["finance_rows"][1]["marketing_ratio"])

    def test_partial_finance_rows_cannot_become_fake_zeroes(self):
        data = build(finances=[finance("2026-11-09", 100), finance("2026-11-10", None), finance("2026-11-11", 200)])
        self.assertIsNone(cell(data, "2026-11-11", "daily_sales")["value"])

    def test_bad_reviews_are_daily_counts_of_archived_review_dates(self):
        data = build(bad=["2026-11-09", "2026-11-10", "2026-11-10", "2026-11-11", "2026-11-17"], review_available=True)
        self.assertEqual(card(data, "bad_review_count")["current"], 4)
        self.assertEqual(cell(data, "2026-11-10", "bad_review_count")["value"], 2)
        self.assertEqual(cell(data, "2026-11-12", "bad_review_count")["value"], 0)
        self.assertEqual(card(data, "bad_review_count")["stat_value"], 3)

    def test_no_review_sync_is_unknown_not_a_zero(self):
        self.assertIsNone(card(build(), "bad_review_count")["current"])
        self.assertEqual(card(build(review_available=True), "bad_review_count")["current"], 0)

    def test_overview_uses_today_deltas_as_headlines_and_totals_as_details(self):
        data = build([snapshot("2026-11-15", 100, admirers=20, item_count=10, review_count=30),
                      snapshot("2026-11-16", 108, admirers=22, item_count=9, review_count=33)])
        for key, daily, total in (("sales", 8, 108), ("admirers", 2, 22),
                                  ("item_count", -1, 9), ("review_count", 3, 33)):
            metric = card(data, key)
            self.assertEqual(metric["today_value"], daily)
            self.assertEqual(metric["total_value"], total)
            self.assertEqual(metric["total_day"], "2026-11-16")
            self.assertEqual(metric["today_value"], cell(data, "2026-11-16", key)["value"])
        self.assertEqual(card(data, "rating")["today_value"], 4.9)
        self.assertEqual(card(data, "rating")["total_label"], "最新评分")

    def test_overview_does_not_promote_stale_data_to_today(self):
        data = build([snapshot("2026-11-14", 100), snapshot("2026-11-15", 110)],
                     [finance("2026-11-14", 100), finance("2026-11-15", 160)])
        for key in ("sales", "admirers", "item_count", "review_count", "rating", "daily_sales"):
            metric = card(data, key)
            self.assertIsNone(metric["today_value"])
            self.assertIsNotNone(metric["total_value"])
            self.assertEqual(metric["total_day"], "2026-11-15")
        self.assertEqual(card(data, "daily_sales")["current"], 60)
        self.assertEqual(card(data, "daily_sales")["total_value"], 160)

    def test_today_snapshot_without_yesterday_has_total_but_no_delta(self):
        data = build([snapshot("2026-11-16", 108)])
        self.assertIsNone(card(data, "sales")["today_value"])
        self.assertEqual(card(data, "sales")["total_value"], 108)
        self.assertEqual(card(data, "rating")["today_value"], 4.9)

    def test_today_and_total_track_simulated_sources_independently(self):
        data = build([snapshot("2026-11-15", 100, 1), snapshot("2026-11-16", 108)])
        self.assertTrue(card(data, "sales")["today_simulated"])
        self.assertFalse(card(data, "sales")["total_simulated"])
        self.assertFalse(card(data, "rating")["today_simulated"])

    def test_finance_today_is_daily_amount_but_detail_is_month_total(self):
        data = build(finances=[finance("2026-11-15", 300), finance("2026-11-16", 350)])
        metric = card(data, "daily_sales")
        self.assertEqual(metric["today_value"], 50)
        self.assertEqual(metric["total_value"], 350)
        self.assertEqual(metric["total_label"], "月累计销售额")
        self.assertTrue(metric["today_simulated"] and metric["total_simulated"])

    def test_finance_current_month_total_does_not_sum_across_resets(self):
        data = build(finances=[finance("2026-10-31", 3000), finance("2026-11-01", 100)],
                     first="2026-10-26", last="2026-11-01")
        self.assertEqual(card(data, "daily_sales")["today_value"], 100)
        self.assertEqual(card(data, "daily_sales")["total_value"], 100)

    def test_finance_without_baseline_still_has_month_total_detail(self):
        data = build(finances=[finance("2026-11-16", 350)])
        self.assertIsNone(card(data, "daily_sales")["today_value"])
        self.assertEqual(card(data, "daily_sales")["total_value"], 350)
        self.assertTrue(card(data, "daily_sales")["total_simulated"])

    def test_bad_review_today_count_is_separate_from_archived_total(self):
        data = build(bad=["2026-11-09", "2026-11-16", "2026-11-16"], review_available=True)
        self.assertEqual(card(data, "bad_review_count")["today_value"], 2)
        self.assertEqual(card(data, "bad_review_count")["total_value"], 3)
        self.assertEqual(card(build(review_available=True), "bad_review_count")["today_value"], 0)
        self.assertIsNone(card(build(), "bad_review_count")["today_value"])

    def test_today_overview_does_not_change_with_chart_period(self):
        snapshots = [snapshot("2026-11-15", 100), snapshot("2026-11-16", 108)]
        short, long = build(snapshots), build(snapshots, first="2026-10-18")
        for metric, other in zip(short["metrics"], long["metrics"]):
            for key in ("today_value", "today_simulated", "total_value", "total_day", "total_simulated"):
                self.assertEqual(metric[key], other[key])

    def test_number_formats_preserve_zero_negative_and_missing_values(self):
        self.assertEqual(format_value(None), "—")
        self.assertEqual(format_value(0), "0")
        self.assertEqual(format_value(-1200), "-1,200")
        self.assertEqual(format_value(1200.5, "money"), "$1,200.50")


class FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 16, 8, 0, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class ShopDataRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temp.name)
        cls.database = cls.data / "shops.sqlite3"
        with closing(sqlite3.connect(cls.database)) as db:
            db.executescript((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
        spec = importlib.util.spec_from_file_location("single_shop_web", ROOT / "releases/v22/app.py")
        cls.web = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.web
        with mock.patch.dict(os.environ, {"ETSY_TOOLS_DATA_DIR": str(cls.data)}):
            spec.loader.exec_module(cls.web)
        cls.web.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("single_shop_web", None)
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.web.app.test_client()
        self.clock = mock.patch.object(self.web, "datetime", FixedDatetime)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with self.web.connect_db() as db:
            for table in ("reviews", "review_sync_runs", "shop_snapshots", "finance_demo", "collection_runs", "shops", "operators"):
                db.execute("DELETE FROM " + table)
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'AlphaShop','alpha','https://www.etsy.com/shop/AlphaShop','2026-11-01')")
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(2,'BetaShop','beta','https://www.etsy.com/shop/BetaShop','2026-11-01')")
            for run, timestamp, sales in ((1, "2026-11-09T10:00:00+00:00", 100), (2, "2026-11-10T10:00:00+00:00", 115)):
                db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(?,?,'completed',1)", (run, timestamp))
                db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales,admirers,item_count,rating,review_count,is_simulated) VALUES(?,1,?,'success',?,20,10,4.9,30,0)", (run, timestamp, sales))
            db.execute("INSERT INTO finance_demo VALUES(1,'2026-11-09',100,30,10,1)")
            db.execute("INSERT INTO finance_demo VALUES(1,'2026-11-10',150,45,20,1)")
            for identifier, stars, review_date, first_seen in (("r1", 4, "2026-11-10", "2026-11-11T10:00:00+00:00"),
                                                             ("r2", 5, "2026-11-10", "2026-11-11T10:00:00+00:00"),
                                                             ("r3", 2, None, "2026-11-09T18:00:00+00:00")):
                db.execute("INSERT INTO reviews(shop_id,review_id,stars,review_date,first_seen_at,last_seen_at) VALUES(1,?,?,?,?,?)", (identifier, stars, review_date, first_seen, first_seen))

    def get(self, path, **kwargs):
        return self.client.get(path, base_url="http://127.0.0.1:8765", **kwargs)

    def test_page_keeps_seven_cards_and_charts_without_daily_detail_table(self):
        response = self.get("/shops/1/data?days=7")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('class="metric-overview-card '), 7)
        self.assertEqual(html.count('class="chart-card metric-chart '), 7)
        self.assertNotIn('每日数据明细', html)
        self.assertNotIn('daily-data-table', html)
        self.assertNotIn('shop-daily-panel', html)
        self.assertNotIn('<table', html)
        self.assertIn('<span class="data-badge simulated">模拟 · USD</span>', html)
        self.assertIn('data-url="/shops/1/reviews?bad=1"', html)
        self.assertIn('aria-current="page">单店数据', html)

    def test_overview_missing_today_shows_dated_totals_below_empty_headlines(self):
        html = self.get("/shops/1/data?days=7").get_data(as_text=True)
        overview = html.split('<section class="metric-overview"')[1].split('</section>')[0]
        sales = overview.split('class="metric-overview-card metric-sales"')[1].split('</a>')[0]
        self.assertIn('overview-value-missing">—</strong>', sales)
        self.assertIn('<span>累计销量</span><strong>115</strong>', sales)
        self.assertIn('截至 <time datetime="2026-11-10">2026-11-10</time>', sales)
        self.assertIn('<h3 id="overview-title-sales">Sales</h3>', sales)
        finance = overview.split('class="metric-overview-card metric-daily_sales"')[1].split('</a>')[0]
        self.assertIn('<span>月累计销售额</span><strong>$150.00</strong>', finance)
        self.assertIn('模拟总值', finance)
        self.assertIn('<time datetime="2026-11-16">2026-11-16</time>', html)

    def test_overview_today_delta_is_prominent_with_total_in_separate_row(self):
        with self.web.connect_db() as db:
            for run, timestamp, sales in ((3, "2026-11-15T10:00:00+00:00", 200),
                                          (4, "2026-11-16T10:00:00+00:00", 208)):
                db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(?,?,'completed',1)", (run, timestamp))
                db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales) VALUES(?,1,?,'success',?)", (run, timestamp, sales))
        html = self.get("/shops/1/data?days=30").get_data(as_text=True)
        overview = html.split('<section class="metric-overview"')[1].split('</section>')[0]
        sales = overview.split('class="metric-overview-card metric-sales"')[1].split('</a>')[0]
        self.assertIn('<strong class="overview-value">+8</strong>', sales)
        self.assertIn('<span>累计销量</span><strong>208</strong>', sales)
        self.assertNotIn('overview-total-meta', sales)
        self.assertNotIn('当日暂无数据', sales)

    def test_period_switch_is_in_the_trends_header_and_returns_to_it(self):
        html = self.get('/shops/1/data?days=7').get_data(as_text=True)
        before_header, after = html.split('id="shop-trends"', 1)
        header = after.split('class="chart-grid shop-charts"', 1)[0]
        self.assertNotIn('aria-label="查看时间范围"', before_header)
        self.assertIn('<h2>指标走势</h2>', header)
        self.assertIn('aria-label="查看时间范围"', header)
        self.assertIn('/shops/1/data?days=7#shop-trends" aria-current="true"', header)
        self.assertIn('/shops/1/data?days=30#shop-trends">最近 30 天', header)

    def test_default_page_and_shop_switch_preserve_period(self):
        response = self.get("/shop-data?shop_id=2&days=7")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/shops/2/data?days=7'))
        self.assertTrue(self.get('/shop-data').location.endswith('/shops/1/data?days=30'))

    def test_default_and_invalid_period_use_thirty_days(self):
        for query in ('', '?days=not-a-number', '?days=999'):
            response = self.get('/shops/1/data' + query)
            html = response.get_data(as_text=True)
            self.assertIn('2026-10-18 — 2026-11-16', html)
            self.assertEqual(html.count('class="chart-card metric-chart '), 7)
            self.assertNotIn('daily-data-table', html)

    def test_shop_isolation_and_no_data_state(self):
        html = self.get('/shops/2/data?days=7').get_data(as_text=True)
        self.assertIn('暂无采集数据', html)
        self.assertNotIn('$50.00', html)
        self.assertNotIn('真实采集</span>', html)
        self.assertNotIn('data-rows=', html)

    def test_nonexistent_and_deleted_shop_ids_return_404(self):
        with self.web.connect_db() as db:
            db.execute("UPDATE shops SET deleted_at='2026-11-16' WHERE id=2")
        for path in ('/shops/2/data', '/shops/99/data', '/shop-data?shop_id=99', '/shop-data?shop_id=bad'):
            self.assertEqual(self.get(path).status_code, 404)

    def test_empty_directory_has_actionable_state(self):
        with self.web.connect_db() as db:
            db.execute("UPDATE shops SET deleted_at='2026-11-16'")
        html = self.get('/shop-data').get_data(as_text=True)
        self.assertIn('请先添加店铺', html)
        self.assertIn('前往店铺管理', html)

    def test_reviews_use_beijing_fallback_date_and_exclude_five_stars(self):
        html = self.get('/shops/1/data?days=7').get_data(as_text=True)
        bad_chart = html.split('id="metric-bad_review_count"')[1].split('</article>')[0]
        self.assertIn('data-day="2026-11-10" data-value="2"', bad_chart)
        self.assertIn('data-url="/shops/1/reviews?bad=0"', html)
        self.assertEqual(self.get('/shops/1/reviews?bad=1').status_code, 200)

    def test_routes_are_read_only_and_work_with_existing_views(self):
        before = hashlib.sha256(self.database.read_bytes()).digest()
        for path in ('/shops/1/data?days=7', '/shops/1/data?days=30', '/', '/dashboard', '/dashboard?metric=daily_sales'):
            response = self.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn('/shops/1/data', response.get_data(as_text=True))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())

    def test_local_host_guard_still_applies(self):
        self.assertEqual(self.client.get('/shops/1/data', base_url='http://untrusted.example').status_code, 400)

    def test_pages_keep_navigation_without_duplicate_intro_and_footer_copy(self):
        paths = ("/", "/shops/1/data", "/finance", *(f'/dashboard?metric={metric["key"]}' for metric in METRICS))
        for path in paths:
            with self.subTest(path=path):
                response = self.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertEqual(html.count('<h1>'), 1)
                self.assertIn('aria-label="工具菜单"', html)
                for removed_class in ('topbar', 'sidebar-note', 'eyebrow', 'subtitle', 'footer-note',
                                      'simulation-banner', 'metric-method-note', 'review-scope-note', 'product-method-note'):
                    self.assertNotIn(f'class="{removed_class}"', html)

    def test_required_source_and_chart_labels_remain_visible(self):
        with self.web.connect_db() as db:
            db.execute("UPDATE shop_snapshots SET is_simulated=1 WHERE shop_id=1")
        html = self.get('/dashboard?metric=sales&days=7').get_data(as_text=True)
        self.assertIn('<span class="data-badge simulated">模拟</span>', html)
        self.assertIn('data-kind="模拟数据"', html)
        self.assertIn('含模拟', html)
        html = self.get('/dashboard?metric=daily_sales').get_data(as_text=True)
        self.assertIn('<span class="data-badge simulated">模拟 · USD</span>', html)
        self.assertIn('Marketing/Sales（月累计 · 右轴）', html)
        self.assertIn('class="finance-chart"', html)
        html = self.get('/shops/1/data?days=7').get_data(as_text=True)
        self.assertNotIn('table-key', html)
        self.assertNotIn('每日数据明细', html)
        self.assertIn('已收录 · 非五星', html)

    def test_metadata_is_escaped(self):
        with self.web.connect_db() as db:
            db.execute("UPDATE shops SET internal_shop_id=? WHERE id=1", ('<script>alert(1)</script>',))
        html = self.get('/shops/1/data').get_data(as_text=True)
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)


if __name__ == '__main__':
    unittest.main()
