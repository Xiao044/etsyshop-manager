import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "releases/v11"))
import collector
import review_sync
from access_guard import AccessRestrictedError, ensure_page_accessible

spec = importlib.util.spec_from_file_location("local_for_restriction_tests", ROOT / "scripts/local.py")
local_dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_dev)

RESTRICTED_TEXT = "Etsy\nAccess is temporarily restricted\nWe detected unusual activity from your device or network."
VALUES = {"sales": 100, "admirers": 20, "item_count": 5, "rating": 4.9,
          "review_count": 3, "review_count_display": "3"}


class RestrictionDetectionTests(unittest.TestCase):
    def test_restriction_has_specific_error_not_generic_title_error(self):
        with self.assertRaises(AccessRestrictedError) as error:
            collector.parse({"title": "etsy.com", "text": RESTRICTED_TEXT}, "Shop1")
        self.assertEqual(error.exception.code, "access_restricted")
        self.assertIn("停止本批", str(error.exception))

    def test_case_and_whitespace_variations_are_supported(self):
        with self.assertRaises(AccessRestrictedError):
            ensure_page_accessible("ACCESS IS   TEMPORARILY\nRESTRICTED")

    def test_normal_page_is_not_misclassified(self):
        ensure_page_accessible("Shop1\n4.9 (3)\nA buyer mentioned unusual activity yesterday.")
        with self.assertRaises(ValueError):
            collector.parse({"title": "Still loading", "text": ""}, "Shop1")

    def test_collect_stops_on_first_restricted_observation(self):
        send = mock.Mock()
        with mock.patch.object(collector, "command", send), \
             mock.patch.object(collector, "page_text", return_value={"title": "etsy.com", "text": RESTRICTED_TEXT}) as read, \
             mock.patch.object(collector.time, "sleep"), \
             self.assertRaises(AccessRestrictedError):
            collector.collect(mock.Mock(), {"name": "Shop1", "url": "https://www.etsy.com/shop/Shop1"}, 100)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(send.call_count, 1)  # initial navigation only; no retry

    def test_review_page_restriction_is_detected_before_clicking(self):
        payload = json.dumps({"page_text": RESTRICTED_TEXT, "sort": "", "items": [], "next_page": None})
        with mock.patch.object(review_sync, "evaluate", return_value=payload) as evaluate, \
             self.assertRaises(AccessRestrictedError):
            review_sync.choose_most_recent(mock.Mock(), mock.Mock())
        self.assertEqual(evaluate.call_count, 1)


class RestrictedBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.database = local_dev.initialize_database(self.data)
        with closing(sqlite3.connect(self.database)) as db, db:
            for shop_id in (1, 2):
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(?,?,?,?,?)",
                           (shop_id, f"Shop{shop_id}", f"shop{shop_id}", f"https://www.etsy.com/shop/Shop{shop_id}", "now"))

    def test_metric_restriction_stops_other_shops_and_all_reviews(self):
        with mock.patch.multiple(collector, DATA_DIR=self.data, DB_PATH=self.database), \
             mock.patch.object(collector, "launch_browser", return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
             mock.patch.object(collector, "close_browser"), \
             mock.patch.object(collector, "collect", side_effect=AccessRestrictedError("访问受限")) as collect, \
             mock.patch.object(collector, "sync_shop_reviews") as sync, \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
            collector.main([])
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(collect.call_count, 1)
        sync.assert_not_called()
        with local_dev.connect_readonly(self.database) as db:
            run = db.execute("SELECT * FROM collection_runs").fetchone()
            self.assertEqual(run["status"], "blocked")
            self.assertEqual(run["failed_count"], 2)
            pending = db.execute("SELECT * FROM shop_snapshots WHERE shop_id=2").fetchone()
            self.assertIn("未访问", pending["error"])
            self.assertIsNone(pending["sales"])

    def test_review_sync_records_a_blocked_result_and_closes_database(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            run_id = db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES('now','running',1)").lastrowid
        with mock.patch.object(review_sync, "choose_most_recent", side_effect=AccessRestrictedError("访问受限")):
            result = review_sync.sync_shop_reviews(mock.Mock(), self.database, self.data, mock.Mock(), 1, run_id, 3)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "access_restricted")
        self.assertEqual(result["added"], 0)
        with local_dev.connect_readonly(self.database) as db:
            self.assertEqual(db.execute("SELECT status FROM review_sync_runs").fetchone()[0], "blocked")

    def test_review_restriction_preserves_metrics_but_stops_other_shops(self):
        blocked = {"status": "blocked", "error_code": "access_restricted", "error": "访问受限"}
        with mock.patch.multiple(collector, DATA_DIR=self.data, DB_PATH=self.database), \
             mock.patch.object(collector, "launch_browser", return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
             mock.patch.object(collector, "close_browser"), \
             mock.patch.object(collector, "collect", return_value=(dict(VALUES), "success")) as collect, \
             mock.patch.object(collector, "sync_shop_reviews", return_value=blocked) as sync, \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            collector.main([])
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(sync.call_count, 1)
        with local_dev.connect_readonly(self.database) as db:
            self.assertEqual(db.execute("SELECT status FROM collection_runs").fetchone()[0], "blocked")
            first = db.execute("SELECT status,sales FROM shop_snapshots WHERE shop_id=1").fetchone()
            self.assertEqual(tuple(first), ("success", 100))
            self.assertIn("未访问", db.execute("SELECT error FROM shop_snapshots WHERE shop_id=2").fetchone()[0])

    def test_normal_success_still_completes_the_batch(self):
        with mock.patch.multiple(collector, DATA_DIR=self.data, DB_PATH=self.database), \
             mock.patch.object(collector, "launch_browser", return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
             mock.patch.object(collector, "close_browser"), \
             mock.patch.object(collector, "collect", return_value=(dict(VALUES), "success")) as collect, \
             mock.patch.object(collector, "sync_shop_reviews", return_value={"status": "completed"}), \
             redirect_stdout(io.StringIO()):
            collector.main([])
        self.assertEqual(collect.call_count, 2)
        with local_dev.connect_readonly(self.database) as db:
            run = db.execute("SELECT status,success_count,failed_count FROM collection_runs").fetchone()
            self.assertEqual(tuple(run), ("completed", 2, 0))


if __name__ == "__main__":
    unittest.main()
