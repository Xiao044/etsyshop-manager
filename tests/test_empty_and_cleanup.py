"""Empty-state and cleanup regression tests; only temporary databases are used."""

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "releases/v22"))
sys.path.insert(0, str(ROOT / "releases/v11"))
from etsy_runtime import maintenance
from etsy_runtime.maintenance import clean_simulated, simulated_counts
from etsy_runtime.locks import FileLock, LockBusy
from etsy_runtime.queue import Store
from finance_metrics import daily_finance
from shop_metrics import build_shop_data
import collector
import review_sync


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.path = self.data / "shops.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'Example','example','https://www.etsy.com/shop/Example','2026-11-01')")
            for run_id, simulated, day in [(1,1,'2026-11-14'),(2,0,'2026-11-15')]:
                db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops) VALUES(?,?,'completed',1)",(run_id,day))
                db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales,is_simulated) VALUES(?,1,?,'success',100,?)",(run_id,day+'T08:00:00+00:00',simulated))
                db.execute("INSERT INTO collection_results(run_id,shop_id,collected_at,status,sales,is_simulated) VALUES(?,1,?,'success',100,?)",(run_id,day+'T08:00:00+00:00',simulated))
                db.execute("INSERT INTO finance_demo VALUES(1,?,0,0,0,?)",(day,simulated))
            db.execute("INSERT INTO finance_demo VALUES(1,'2026-11-16',NULL,NULL,NULL,NULL)")
            db.execute("INSERT INTO reviews(shop_id,review_id,stars,first_seen_at,last_seen_at) VALUES(1,'real-review',5,'2026-11-15','2026-11-15')")
        self.store = Store(self.path)
        self.store.initialize()

    def counts(self):
        with closing(sqlite3.connect(self.path)) as db:
            return simulated_counts(db)

    def test_dry_run_does_not_modify_database_or_create_backup(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        result=clean_simulated(self.path)
        self.assertEqual(result['total'],3)
        self.assertTrue(result['dry_run'])
        self.assertEqual(before,hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertFalse((self.data/'backups').exists())

    def test_cleanup_has_verified_backup_and_preserves_real_data_and_zeroes(self):
        report=clean_simulated(self.path,apply=True)
        self.assertEqual(report['total'],3)
        self.assertTrue(report['real_records_unchanged'])
        self.assertTrue(Path(report['backup']).is_file())
        self.assertEqual(sum(self.counts().values()),0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT sales,is_simulated FROM shop_snapshots').fetchone(),(100,0))
            self.assertEqual(db.execute('SELECT count(*) FROM collection_runs').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM reviews').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM finance_demo').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT sales_usd FROM finance_demo WHERE day='2026-11-15'").fetchone()[0],0)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        with closing(sqlite3.connect(report['backup'])) as db:
            self.assertEqual(sum(simulated_counts(db).values()),3)
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')

    def test_repeated_cleanup_does_not_delete_more_records(self):
        clean_simulated(self.path,apply=True)
        self.assertEqual(clean_simulated(self.path,apply=True)['total'],0)

    def test_running_worker_lock_blocks_cleanup(self):
        with FileLock(self.data/'collection-worker.lock'), self.assertRaises(LockBusy):
            clean_simulated(self.path,apply=True)
        self.assertEqual(sum(self.counts().values()),3)

    def test_queued_and_running_tasks_block_cleanup(self):
        self.store.submit('shop',[1])
        with self.assertRaisesRegex(ValueError,'排队'):
            clean_simulated(self.path,apply=True)
        self.store.claim('test-worker')
        with self.assertRaises(ValueError):clean_simulated(self.path,apply=True)
        self.assertEqual(sum(self.counts().values()),3)

    def test_failed_preservation_check_rolls_back_deletion(self):
        fingerprint=maintenance._fingerprint
        calls={}
        def tampered(db,table,filtered=False):
            value=fingerprint(db,table,filtered)
            calls[table]=calls.get(table,0)+1
            if table=='shops' and calls[table]>1:value={**value,'sha256':'mismatch'}
            return value
        with mock.patch.object(maintenance,'_fingerprint',side_effect=tampered), self.assertRaisesRegex(ValueError,'回滚'):
            clean_simulated(self.path,apply=True)
        self.assertEqual(sum(self.counts().values()),3)

    def test_missing_database_is_not_created(self):
        missing=self.data/'missing.sqlite3'
        with self.assertRaises(FileNotFoundError):clean_simulated(missing,apply=True)
        self.assertFalse(missing.exists())

    def test_missing_source_flags_are_not_guessed(self):
        path=self.data/'unmarked.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:db.execute('CREATE TABLE finance_demo(day TEXT)')
        with self.assertRaisesRegex(ValueError,'拒绝'):clean_simulated(path,apply=True)


class EmptyMetricTests(unittest.TestCase):
    def build(self,snapshots=(),finances=(),available=False):
        return build_shop_data(snapshots,finances,[],'2026-11-10','2026-11-16',review_available=available)

    def test_no_data_keeps_seven_metrics_and_blank_cells(self):
        data=self.build()
        self.assertFalse(data['has_data'])
        self.assertEqual(len(data['metrics']),7)
        self.assertEqual(len(data['table_rows']),7)
        self.assertTrue(all(m['current'] is None and m['stat_value'] is None for m in data['metrics']))
        self.assertTrue(all(cell['value'] is None for row in data['table_rows'] for cell in row['cells']))

    def test_all_null_snapshot_is_not_interpreted_as_zero(self):
        row={'id':1,'day':'2026-11-16','collected_at':'2026-11-16T08:00:00+00:00','is_simulated':0,
             **dict.fromkeys(['sales','admirers','item_count','on_sale_count','rating','review_count'])}
        data=self.build([row])
        self.assertFalse(data['has_data'])
        self.assertTrue(all(m['total_value'] is None for m in data['metrics']))

    def test_first_snapshot_keeps_totals_but_no_fake_daily_delta(self):
        row={'id':1,'day':'2026-11-16','collected_at':'2026-11-16T08:00:00+00:00','is_simulated':0,
             'sales':100,'admirers':0,'item_count':10,'on_sale_count':0,'rating':4.9,'review_count':0}
        data=self.build([row])
        sales=next(m for m in data['metrics'] if m['key']=='sales')
        self.assertEqual(sales['total_value'],100)
        self.assertIsNone(sales['today_value'])
        admirers=next(m for m in data['metrics'] if m['key']=='admirers')
        self.assertEqual(admirers['total_value'],0)

    def test_unknown_reviews_are_blank_but_confirmed_empty_is_zero(self):
        missing=next(m for m in self.build()['metrics'] if m['key']=='bad_review_count')
        known=next(m for m in self.build(available=True)['metrics'] if m['key']=='bad_review_count')
        self.assertIsNone(missing['total_value'])
        self.assertEqual(known['total_value'],0)
        self.assertEqual(known['today_value'],0)

    def test_all_null_finances_and_unknown_dates_are_safe(self):
        rows=[{'day':day,'sales_usd':None,'net_profit_usd':None,'marketing_usd':None} for day in (None,'2026-11-15','2026-11-16')]
        self.assertEqual(daily_finance(rows),[])
        self.assertFalse(self.build(finances=rows)['has_finance_data'])

    def test_missing_finance_fields_never_fill_in_zero(self):
        rows=[{'day':'2026-11-01','sales_usd':None,'net_profit_usd':None,'marketing_usd':10},
              {'day':'2026-11-02','sales_usd':100,'net_profit_usd':None,'marketing_usd':15}]
        result=daily_finance(rows)
        self.assertIsNone(result[0]['sales_usd'])
        self.assertIsNone(result[1]['sales_usd'])
        self.assertIsNone(result[1]['net_profit_usd'])
        self.assertEqual(result[1]['marketing_usd'],5)

    def test_no_page_metrics_is_failure_not_success(self):
        with self.assertRaisesRegex(ValueError,'未找到指标'):
            collector.parse({'title':'Example - Etsy','text':'Example\nNo metrics loaded'},'Example')

    def test_zero_reviews_completes_without_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'shops.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'Example','example','https://www.etsy.com/shop/Example','now')")
                run=db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES('now','running',1)").lastrowid
            with mock.patch.object(review_sync,'choose_most_recent',side_effect=AssertionError('must not access zero-review page')):
                result=review_sync.sync_shop_reviews(mock.Mock(),path,Path(directory),mock.Mock(),1,run,0)
            self.assertEqual((result['status'],result['pages'],result['added']),('completed',0,0))


    def test_unknown_review_count_with_empty_cards_is_failure_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'shops.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'Example','example','https://www.etsy.com/shop/Example','now')")
                run=db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES('now','running',1)").lastrowid
            state={'sort':'Most recent','items':[],'next_page':None}
            with mock.patch.object(review_sync,'choose_most_recent',return_value=state), mock.patch.object(review_sync,'next_page') as next_page:
                result=review_sync.sync_shop_reviews(mock.Mock(),path,Path(directory),mock.Mock(),1,run,None)
            self.assertEqual(result['status'],'failed')
            self.assertIn('没有评论卡片',result['error'])
            next_page.assert_not_called()


class EmptyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name);self.path=self.data/'shops.sqlite3'
        with closing(sqlite3.connect(self.path)) as db:db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
        spec=importlib.util.spec_from_file_location('empty_state_web',ROOT/'releases/v22/app.py')
        self.web=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.web
        self.addCleanup(lambda:sys.modules.pop(spec.name,None))
        with mock.patch.dict(os.environ,{'ETSY_TOOLS_DATA_DIR':str(self.data),'ETSY_COLLECTION_ENABLED':'1'}):spec.loader.exec_module(self.web)
        self.web.app.config.update(TESTING=True,SERVER_NAME='127.0.0.1:8765')
        self.client=self.web.app.test_client()
        with self.client.session_transaction() as session:session['csrf_token']='test'

    def add_shop(self):
        with self.web.connect_db() as db:db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'Example','example','https://www.etsy.com/shop/Example','2026-11-01')")

    def test_blank_directory_does_not_start_collection_or_create_jobs(self):
        with mock.patch('subprocess.Popen',side_effect=AssertionError('no automatic collection')):
            for route in ['/','/dashboard','/shop-data']:
                result=self.client.get(route)
                self.assertEqual(result.status_code,200)
                self.assertIn('添加',result.get_data(as_text=True))
                self.assertNotIn('data-refresh',result.get_data(as_text=True))
        response=self.client.post('/api/collection/jobs',json={'scope':'all'},headers={'X-CSRF-Token':'test'})
        self.assertEqual(response.status_code,404)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_jobs'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM collection_runs').fetchone()[0],0)

    def test_shop_without_data_renders_every_metric_and_no_demo_claim(self):
        self.add_shop()
        for key in ['sales','admirers','item_count','rating','review_count','bad_review_count','daily_sales']:
            response=self.client.get('/dashboard?metric='+key)
            self.assertEqual(response.status_code,200,key)
            self.assertNotIn('历史中包含 30 天模拟数据',response.get_data(as_text=True))
        html=self.client.get('/shops/1/data').get_data(as_text=True)
        self.assertIn('暂无采集数据',html)
        self.assertIn('财务数据尚未接入',html)
        self.assertNotIn('模拟 · USD',html)

    def test_bad_review_dashboard_does_not_chart_unknown_as_zero(self):
        self.add_shop()
        with mock.patch.object(self.web,'render_template',return_value='ok') as render:
            self.client.get('/dashboard?metric=bad_review_count')
        card=render.call_args.kwargs['cards'][0]
        self.assertIsNone(card['current'])
        self.assertEqual(card['chart_days'],[])
        with self.web.connect_db() as db:db.execute("INSERT INTO review_sync_runs(shop_id,started_at,status) VALUES(1,'2026-11-16','completed')")
        with mock.patch.object(self.web,'render_template',return_value='ok') as render:
            self.client.get('/dashboard?metric=bad_review_count')
        self.assertEqual(render.call_args.kwargs['cards'][0]['current'],0)

    def test_nullable_finance_rows_do_not_error_in_either_view(self):
        self.add_shop()
        with self.web.connect_db() as db:
            db.execute("INSERT INTO finance_demo VALUES(1,'2026-11-15',NULL,NULL,NULL,0)")
            db.execute("INSERT INTO finance_demo VALUES(1,NULL,NULL,NULL,NULL,0)")
        for route in ['/dashboard?metric=daily_sales','/shops/1/data']:
            result=self.client.get(route)
            self.assertEqual(result.status_code,200)
            self.assertIn('尚未接入',result.get_data(as_text=True))


if __name__=='__main__':unittest.main()
