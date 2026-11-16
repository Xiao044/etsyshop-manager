"""Daily snapshot upserts, historical migration, and collector batch accounting."""

import io
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'releases/v11'))
import collector
from access_guard import AccessRestrictedError
from etsy_runtime.snapshots import (backup_before_daily_migration, collection_result,
                                    ensure_daily_snapshots_schema, save_snapshot)

VALUES = {'sales': 100, 'admirers': 20, 'item_count': 10, 'on_sale_count': 3,
          'rating': 4.9, 'review_count': 30, 'review_count_display': '30'}


class DailySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.path = self.data / 'shops.sqlite3'
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(self.db.close)
        self.db.executescript((ROOT / 'sql/schema.sql').read_text(encoding='utf-8'))
        for shop_id in (1, 2):
            self.db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(?,?,?,?,?)",
                            (shop_id, f'Shop{shop_id}', f'shop{shop_id}', f'https://www.etsy.com/shop/Shop{shop_id}', '2026-11-16'))
        for run_id in range(1, 21):
            self.db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(?,'2026-11-16T00:00:00+00:00','completed',1)", (run_id,))
        self.db.commit()

    def save(self, run=1, stamp='2026-11-16T01:00:00+00:00', status='success', shop=1, **kwargs):
        with self.db:
            save_snapshot(self.db, run, shop, stamp, status, kwargs.pop('values', dict(VALUES)), **kwargs)

    def snapshots(self):
        return self.db.execute('SELECT * FROM shop_snapshots ORDER BY shop_id,collected_at').fetchall()

    def legacy(self):
        self.db.execute('DROP INDEX idx_snapshots_shop_day')
        self.db.execute('DROP TABLE collection_results')
        self.db.commit()

    def test_same_shop_day_updates_the_existing_id_and_values(self):
        self.save()
        original_id = self.snapshots()[0]['id']
        self.save(2, '2026-11-16T02:00:00+00:00', values={**VALUES, 'sales': 108, 'on_sale_count': 4})
        rows = self.snapshots()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], original_id)
        self.assertEqual((rows[0]['sales'], rows[0]['run_id'], rows[0]['on_sale_count']), (108, 2, 4))
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 2)
        self.assertEqual(collection_result(self.db, 1, 1)['sales'], 100)

    def test_new_beijing_date_appends_instead_of_overwriting(self):
        self.save(stamp='2026-11-16T15:59:59+00:00')
        self.save(2, '2026-11-16T16:00:00+00:00', values={**VALUES, 'sales': 108})
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual([r[0] for r in self.db.execute("SELECT date(datetime(collected_at,'+8 hours')) FROM shop_snapshots ORDER BY collected_at")],
                         ['2026-11-16', '2026-11-17'])

    def test_different_utc_dates_in_one_beijing_day_update(self):
        self.save(stamp='2026-11-15T23:00:00+00:00')
        self.save(2, '2026-11-16T01:00:00+00:00', values={**VALUES, 'sales': 108})
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0]['sales'], 108)

    def test_explicit_beijing_offset_is_normalized_not_shifted_twice(self):
        self.save(stamp='2026-11-16T08:00:00+08:00')
        self.save(2, '2026-11-16T01:00:00Z', values={**VALUES, 'sales': 108})
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0]['collected_at'], '2026-11-16T01:00:00+00:00')

    def test_other_shops_have_independent_daily_records(self):
        self.save(shop=1)
        self.save(2, shop=2)
        self.assertEqual(len(self.snapshots()), 2)

    def test_failed_refresh_preserves_valid_daily_values_and_its_batch_error(self):
        self.save()
        original = tuple(self.snapshots()[0])
        self.save(2, '2026-11-16T02:00:00+00:00', 'failed', values={}, error='page failed')
        self.assertEqual(tuple(self.snapshots()[0]), original)
        result = collection_result(self.db, 2, 1)
        self.assertEqual((result['status'], result['error']), ('failed', 'page failed'))
        self.assertIsNone(result['sales'])

    def test_failed_first_attempt_can_be_updated_by_a_successful_retry(self):
        self.save(status='failed', values={}, error='page failed')
        original_id = self.snapshots()[0]['id']
        self.save(2, '2026-11-16T02:00:00+00:00')
        row = self.snapshots()[0]
        self.assertEqual(row['id'], original_id)
        self.assertEqual((row['sales'], row['status']), (100, 'success'))
        self.assertIsNone(row['error'])

    def test_partial_refresh_keeps_known_fields_without_converting_zero_to_missing(self):
        self.save()
        self.save(2, '2026-11-16T02:00:00+00:00', 'partial',
                  values={'sales': 108, 'admirers': None, 'on_sale_count': 0}, error='partial')
        row = self.snapshots()[0]
        self.assertEqual((row['sales'], row['admirers'], row['on_sale_count']), (108, 20, 0))
        self.assertEqual(row['status'], 'partial')
        self.assertIsNone(collection_result(self.db, 2, 1)['admirers'])

    def test_real_partial_refresh_never_relabels_simulated_fields_as_real(self):
        self.save(is_simulated=True)
        self.save(2, '2026-11-16T02:00:00+00:00', 'partial', values={'sales': 108})
        row = self.snapshots()[0]
        self.assertEqual((row['sales'], row['is_simulated']), (108, 0))
        self.assertIsNone(row['admirers'])
        self.assertIsNone(row['on_sale_count'])

    def test_real_result_wins_over_a_later_simulated_snapshot(self):
        self.save()
        self.save(2, '2026-11-16T02:00:00+00:00', values={**VALUES, 'sales': 900}, is_simulated=True)
        self.assertEqual(self.snapshots()[0]['sales'], 100)

    def test_older_result_cannot_roll_back_same_day_values(self):
        self.save(stamp='2026-11-16T02:00:00+00:00', values={**VALUES, 'sales': 108})
        self.save(2, '2026-11-16T01:00:00+00:00')
        self.assertEqual(self.snapshots()[0]['sales'], 108)
        self.assertEqual(collection_result(self.db, 2, 1)['sales'], 100)

    def test_same_run_replay_is_idempotent(self):
        self.save()
        self.save(stamp='2026-11-16T02:00:00+00:00', values={**VALUES, 'sales': 108})
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 1)

    def test_unique_index_enforces_daily_rule_even_for_direct_inserts(self):
        self.save()
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status) VALUES(2,1,'2026-11-16T02:00:00+00:00','failed')")
        self.assertEqual(len(self.snapshots()), 1)

    def test_invalid_timestamp_does_not_write_any_record(self):
        with self.assertRaises(ValueError):
            self.save(stamp='not-a-date')
        self.assertEqual(len(self.snapshots()), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 0)

    def test_invalid_status_is_rejected_without_writes(self):
        with self.assertRaises(ValueError):
            self.save(status='invented')
        self.assertEqual(len(self.snapshots()), 0)

    def test_migration_backfills_original_columns_before_on_sale_existed(self):
        self.legacy()
        # Old databases have no On sale column. Unknown counts must stay NULL.
        self.db.execute('ALTER TABLE shop_snapshots DROP COLUMN on_sale_count')
        with self.db:
            self.db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales) VALUES(1,1,'2026-11-16T01:00:00+00:00','success',100)")
            ensure_daily_snapshots_schema(self.db)
        self.assertIsNone(self.snapshots()[0]['on_sale_count'])
        self.assertEqual(collection_result(self.db, 1, 1)['sales'], 100)
        self.assertIsNone(collection_result(self.db, 1, 1)['on_sale_count'])

    def test_audit_and_daily_changes_roll_back_together(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.save(run=999)
        self.assertEqual(len(self.snapshots()), 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 0)
        self.save()
        self.assertEqual(len(self.snapshots()), 1)

    def test_concurrent_writers_keep_one_row_and_all_run_results(self):
        def write(run):
            with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
                db.execute('PRAGMA foreign_keys=ON')
                save_snapshot(db, run, 1, f'2026-11-16T01:{run:02d}:00+00:00', 'success', {**VALUES, 'sales': 100 + run})
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(write, range(1, 11)))
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0]['sales'], 110)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 10)

    def test_migration_archives_all_runs_but_keeps_latest_real_valid_day(self):
        self.legacy()
        with self.db:
            for run, stamp, status, amount, simulated in (
                (1, '2026-11-16T01:00:00+00:00', 'success', 100, 0),
                (2, '2026-11-16T02:00:00+00:00', 'success', 108, 0),
                (3, '2026-11-16T03:00:00+00:00', 'success', 999, 1),
                (4, '2026-11-16T04:00:00+00:00', 'failed', None, 0),
                (5, '2026-11-17T01:00:00+00:00', 'success', 120, 0)):
                self.db.execute('INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales,is_simulated) VALUES(?,1,?,?,?,?)',
                                (run, stamp, status, amount, simulated))
            self.db.execute("INSERT INTO reviews(shop_id,review_id,stars,review_text,first_seen_at,last_seen_at) VALUES(1,'review-1',4,'Keep me','2026-11-16','2026-11-16')")
        backup = backup_before_daily_migration(self.path)
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as saved:
            self.assertEqual(saved.execute('SELECT count(*) FROM shop_snapshots').fetchone()[0], 5)
        with self.db:
            ensure_daily_snapshots_schema(self.db)
        self.assertEqual([r['sales'] for r in self.snapshots()], [108, 120])
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 5)
        self.assertEqual(collection_result(self.db, 4, 1)['status'], 'failed')
        self.assertEqual(self.db.execute('SELECT review_text FROM reviews').fetchone()[0], 'Keep me')
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_runs').fetchone()[0], 20)
        self.assertIsNone(self.db.execute('PRAGMA foreign_key_check').fetchone())
        self.assertEqual(self.db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        with self.db:
            ensure_daily_snapshots_schema(self.db)
        self.assertEqual(self.db.execute('SELECT count(*) FROM collection_results').fetchone()[0], 5)
        self.assertIsNone(backup_before_daily_migration(self.path))

    def test_migration_prefers_valid_demo_over_an_empty_failed_attempt(self):
        self.legacy()
        with self.db:
            self.db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales,is_simulated) VALUES(1,1,'2026-11-16T01:00:00+00:00','success',100,1)")
            self.db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales) VALUES(2,1,'2026-11-16T02:00:00+00:00','failed',NULL)")
            ensure_daily_snapshots_schema(self.db)
        self.assertEqual((self.snapshots()[0]['sales'], self.snapshots()[0]['is_simulated']), (100, 1))

    def test_invalid_legacy_date_cancels_migration_without_deleting_rows(self):
        self.legacy()
        with self.db:
            self.db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status) VALUES(1,1,'bad-date','failed')")
        with self.assertRaises(ValueError), self.db:
            ensure_daily_snapshots_schema(self.db)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='idx_snapshots_shop_day'").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_results'").fetchone())

    def test_result_reader_supports_legacy_databases(self):
        self.save()
        self.db.execute('DROP TABLE collection_results')
        self.assertEqual(collection_result(self.db, 1, 1)['sales'], 100)
        self.assertIsNone(collection_result(self.db, 2, 1))

    def test_collector_runs_same_day_update_one_row_and_keep_separate_counts(self):
        with mock.patch.multiple(collector, DATA_DIR=self.data, DB_PATH=self.path), \
             mock.patch.object(collector, 'now', return_value='2026-11-16T01:00:00+00:00'), \
             mock.patch.object(collector, 'launch_browser', return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
             mock.patch.object(collector, 'close_browser'), \
             mock.patch.object(collector, 'collect', side_effect=[(dict(VALUES), 'success'), ({**VALUES, 'sales': 108}, 'success')]), \
             redirect_stdout(io.StringIO()):
            collector.main(['--shop-id', '1', '--skip-reviews'])
            collector.main(['--shop-id', '1', '--skip-reviews'])
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(self.snapshots()[0]['sales'], 108)
        runs = self.db.execute('SELECT status,success_count,failed_count FROM collection_runs WHERE id>20 ORDER BY id').fetchall()
        self.assertEqual([tuple(row) for row in runs], [('completed', 1, 0), ('completed', 1, 0)])

    def test_blocked_batch_is_counted_from_attempts_not_previous_successes(self):
        self.save()
        self.save(2, shop=2)
        with mock.patch.multiple(collector, DATA_DIR=self.data, DB_PATH=self.path), \
             mock.patch.object(collector, 'now', return_value='2026-11-16T02:00:00+00:00'), \
             mock.patch.object(collector, 'launch_browser', return_value=(mock.Mock(), mock.Mock(), mock.Mock())), \
             mock.patch.object(collector, 'close_browser'), \
             mock.patch.object(collector, 'collect', side_effect=AccessRestrictedError('blocked')), \
             redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            collector.main(['--skip-reviews'])
        self.assertEqual([row['sales'] for row in self.snapshots()], [100, 100])
        run = self.db.execute('SELECT status,success_count,failed_count FROM collection_runs ORDER BY id DESC LIMIT 1').fetchone()
        self.assertEqual(tuple(run), ('blocked', 0, 2))
        attempt = self.db.execute('SELECT * FROM collection_results WHERE run_id=(SELECT max(id) FROM collection_runs) AND shop_id=2').fetchone()
        self.assertEqual(attempt['status'], 'failed')
        self.assertIn('未访问', attempt['error'])


if __name__ == '__main__':
    unittest.main()
