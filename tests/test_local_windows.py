import hashlib
import importlib.util
import io
import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "releases/v11"))
import collector
import review_sync

spec = importlib.util.spec_from_file_location("local_dev", ROOT / "scripts/local.py")
local_dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_dev)


class LocalDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_blank_database_supports_all_seven_tables(self):
        path = local_dev.initialize_database(self.root / "data")
        local_dev.validate_database(path)
        with local_dev.connect_readonly(path) as db:
            for table in local_dev.TABLES:
                self.assertEqual(db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        secret = (path.parent / "session_secret").read_text()
        local_dev.initialize_database(path.parent)
        self.assertEqual(secret, (path.parent / "session_secret").read_text())

    def test_snapshot_is_copied_without_mutating_source_or_overwriting(self):
        source = local_dev.initialize_database(self.root / "snapshot")
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        dest = local_dev.initialize_database(self.root / "dev", source)
        self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())
        with closing(sqlite3.connect(dest)) as db, db:
            db.execute("INSERT INTO operators(name,name_key,created_at) VALUES('test','test','now')")
        with local_dev.connect_readonly(source) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operators").fetchone()[0], 0)
        with self.assertRaises(FileExistsError):
            local_dev.initialize_database(self.root / "dev", source)

    def test_sql_connection_rejects_writes(self):
        path = local_dev.initialize_database(self.root / "data")
        with local_dev.connect_readonly(path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM shops")

    def test_collect_requires_explicit_scope(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            local_dev.main(["collect"])
        self.assertEqual(error.exception.code, 2)


class BrowserTests(unittest.TestCase):
    def test_windows_browser_uses_own_profile_and_no_linux_flags(self):
        args = collector.browser_arguments("chrome.exe", windows=True)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--lang=en-US", args)
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in args))
        self.assertNotIn("--no-sandbox", args)
        self.assertNotIn("--disable-dev-shm-usage", args)

    def test_linux_browser_preserves_existing_launch_flags(self):
        self.assertIn("--no-sandbox", collector.browser_arguments("chromium", windows=False))

    def test_invalid_explicit_browser_is_not_silently_ignored(self):
        with mock.patch.dict(os.environ, {"ETSY_BROWSER_PATH": str(ROOT / "missing-browser.exe")}):
            with self.assertRaises(FileNotFoundError):
                collector.browser_executable()

    def test_occupied_debug_port_is_rejected(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            with mock.patch.object(collector, "DEBUG_PORT", server.getsockname()[1]):
                with self.assertRaisesRegex(RuntimeError, "已占用"):
                    collector.launch_browser()

    def test_current_english_counter_parser(self):
        page = {"title": "ExampleShop - Etsy", "text": "ExampleShop\n4.9 (1.2k)\n12,345 Sales\n678 Admirers\nAll 90\nOn sale 30\nReviews\nSort by: Suggested\n4.9 (1,234)"}
        values, status = collector.parse(page, "ExampleShop")
        self.assertEqual(status, "success")
        self.assertEqual(values["sales"], 12345)
        self.assertEqual(values["review_count"], 1234)

    def test_selected_shop_only_and_no_review_sync_after_page_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            path = local_dev.initialize_database(data)
            with closing(sqlite3.connect(path)) as db, db:
                for i in (1, 2):
                    db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(?,?,?,?,?)", (i, f"Shop{i}", f"shop{i}", f"https://www.etsy.com/shop/Shop{i}", "now"))
            with mock.patch.multiple(collector, DATA_DIR=data, DB_PATH=path), \
                 mock.patch.object(collector, "launch_browser", return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
                 mock.patch.object(collector, "close_browser"), \
                 mock.patch.object(collector, "collect", side_effect=ValueError("page failed")), \
                 mock.patch.object(collector, "sync_shop_reviews") as sync, \
                 redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                collector.main(["--shop-id", "1"])
            sync.assert_not_called()
            with local_dev.connect_readonly(path) as db:
                self.assertEqual([row[0] for row in db.execute("SELECT shop_id FROM shop_snapshots")], [1])


if __name__ == "__main__":
    unittest.main()
