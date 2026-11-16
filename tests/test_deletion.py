"""Deletion controls persist changes without removing collection data."""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'releases/v22'))
from etsy_runtime.queue import Store, QueueError


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name);self.database=self.data/'shops.sqlite3'
        with closing(sqlite3.connect(self.database)) as db,db:
            db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
            for shop in (1,2):
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(?,?,?,?,?)",(shop,f'Shop{shop}',f'shop{shop}',f'https://www.etsy.com/shop/Shop{shop}','2026-11-01'))
            db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops,success_count) VALUES(1,'2026-11-16','completed',1,1)")
            db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales) VALUES(1,1,'2026-11-16T08:00:00+00:00','success',100)")
            db.execute("INSERT INTO collection_results(run_id,shop_id,collected_at,status,sales) VALUES(1,1,'2026-11-16T08:00:00+00:00','success',100)")
            db.execute("INSERT INTO reviews(shop_id,review_id,stars,first_seen_at,last_seen_at) VALUES(1,'real',5,'2026-11-16','2026-11-16')")
        self.clock=1000
        self.store=Store(self.database,clock=lambda:self.clock);self.store.initialize()
        spec=importlib.util.spec_from_file_location('deletion_web',ROOT/'releases/v22/app.py')
        self.web=importlib.util.module_from_spec(spec);sys.modules[spec.name]=self.web
        self.addCleanup(lambda:sys.modules.pop(spec.name,None))
        with mock.patch.dict(os.environ,{'ETSY_TOOLS_DATA_DIR':str(self.data),'ETSY_COLLECTION_ENABLED':'1'}):spec.loader.exec_module(self.web)
        self.web.app.config.update(TESTING=True,SERVER_NAME='127.0.0.1:8765')
        self.client=self.web.app.test_client()
        with self.client.session_transaction() as session:session['csrf_token']='token'
        self.headers={'X-CSRF-Token':'token'}

    def finish_shop(self,shop=1,status='succeeded'):
        job=self.store.submit('shop',[shop])['job'];task=self.store.claim('test-worker')
        self.store.finish(task['id'],status,metrics_status='success',reviews_status='completed')
        return job,task

    def test_finished_record_is_deleted_from_database_not_just_hidden(self):
        job,task=self.finish_shop()
        self.store.delete_job(job['id'])
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM collection_jobs WHERE id=?',(job['id'],)).fetchone())
            self.assertIsNone(db.execute('SELECT job_id FROM collection_job_tasks WHERE job_id=?',(job['id'],)).fetchone())
            self.assertIsNotNone(db.execute('SELECT id FROM collection_tasks WHERE id=?',(task['id'],)).fetchone())
            for table in ['shop_snapshots','collection_results','reviews','collection_runs']:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],1)
        self.assertEqual(Store(self.database).state()['jobs'],[])
        with self.assertRaises(QueueError) as error:self.store.job(job['id'])
        self.assertEqual(error.exception.status,404)

    def test_delete_record_does_not_bypass_cooldown(self):
        job,_=self.finish_shop();self.store.delete_job(job['id'])
        with self.assertRaises(QueueError) as error:self.store.submit('shop',[1])
        self.assertEqual(error.exception.code,'cooldown')

    def test_active_records_are_not_deletable(self):
        job=self.store.submit('all')['job']
        for phase in ['queued','running']:
            if phase=='running':self.store.claim('worker')
            with self.assertRaises(QueueError) as error:self.store.delete_job(job['id'])
            self.assertEqual(error.exception.code,'job_active')
            self.assertIsNotNone(self.store.job(job['id']))

    def test_deleting_one_request_preserves_shared_tasks(self):
        all_job=self.store.submit('all')['job']
        single=self.store.submit('shop',[1])['job']
        task=self.store.claim('worker');self.store.finish(task['id'],'succeeded')
        self.store.delete_job(single['id'])
        remaining=self.store.job(all_job['id'])
        self.assertEqual(len(remaining['tasks']),2)
        self.assertEqual(remaining['tasks'][0]['status'],'succeeded')
        self.assertEqual(remaining['tasks'][0]['id'],task['id'])
        self.assertEqual(remaining['status'],'running')

    def test_diagnostic_log_files_remain_explicitly_separate(self):
        job,task=self.finish_shop()
        logs=self.data/'logs';logs.mkdir();path=logs/f"collection-task-{task['id']}.log"
        path.write_text('diagnostic evidence',encoding='utf-8')
        self.store.delete_job(job['id'])
        self.assertEqual(path.read_text(encoding='utf-8'),'diagnostic evidence')

    def test_record_deletion_does_not_resume_restricted_queue(self):
        job=self.store.submit('all')['job'];task=self.store.claim('worker')
        self.store.finish(task['id'],'blocked',error='restricted')
        self.store.delete_job(job['id'])
        self.assertTrue(self.store.state()['worker']['paused'])

    def test_retired_history_api_does_not_delete_records(self):
        job,_=self.finish_shop()
        route='/api/collection/jobs/'+job['id']
        self.assertEqual(self.client.delete(route).status_code,404)
        self.assertEqual(self.client.delete(route,headers=self.headers).status_code,404)
        self.assertEqual(self.store.job(job['id'])['status'],'completed')

    def test_shop_deletion_cascades_all_owned_database_records(self):
        job=self.store.submit('shop',[1])['job']
        response=self.client.post('/shops/1/delete',data={'csrf_token':'token','confirm_permanent':'1'})
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['Location'],'/')
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
            for table in ['shop_snapshots','collection_results','reviews','review_sync_runs','finance_demo','collection_tasks','collection_jobs']:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE shop_id=1').fetchone()[0],0,table)
            self.assertEqual(db.execute('SELECT count(*) FROM collection_runs WHERE id=1').fetchone()[0],0)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        with self.assertRaises(QueueError):self.store.job(job['id'])
        self.assertEqual(self.store.state()['active_shop_count'],1)
        self.assertEqual(self.client.get('/shops/1/data').status_code,404)
        html=self.client.get('/').get_data(as_text=True)
        self.assertNotIn('/shops/1/data',html)
        self.assertNotIn('/shops/1/data',self.client.get('/dashboard').get_data(as_text=True))
        with self.assertRaises(QueueError) as error:self.store.submit('shop',[1])
        self.assertEqual(error.exception.status,404)

    def test_shop_delete_requires_csrf(self):
        self.assertEqual(self.client.post('/shops/1/delete').status_code,403)
        with self.store.connect() as db:self.assertIsNone(db.execute('SELECT deleted_at FROM shops WHERE id=1').fetchone()[0])

    def test_running_collection_blocks_permanent_deletion(self):
        job=self.store.submit('all')['job'];task=self.store.claim('worker')
        with self.assertRaises(QueueError) as error:self.store.delete_shop(1)
        self.assertEqual(error.exception.code,'shop_delete_blocked')
        self.assertEqual(self.store.job(job['id'])['tasks'][0]['status'],'running')
        with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
        self.store.finish(task['id'],'succeeded')
        self.assertEqual(self.store.claim('worker')['shop_id'],2)

    def test_permanently_deleted_shop_is_new_when_readded(self):
        self.client.post('/shops/1/delete',data={'csrf_token':'token','confirm_permanent':'1'})
        response=self.client.post('/shops/add',data={'csrf_token':'token','shop_url':'https://www.etsy.com/shop/Shop1','shop_id':'','operator':''})
        self.assertEqual(response.status_code,303)
        with self.store.connect() as db:
            new=db.execute("SELECT id FROM shops WHERE slug_key='shop1'").fetchone()[0]
            self.assertGreater(new,1)
            self.assertEqual(db.execute('SELECT count(*) FROM shop_snapshots WHERE shop_id=?',(new,)).fetchone()[0],0)
        self.assertEqual(self.client.get('/shops/1/data').status_code,404)
        self.assertEqual(self.client.get(f'/shops/{new}/data').status_code,200)

    def test_permanent_delete_remains_on_directory_and_shop_details(self):
        for route in ('/', '/shops/1/data'):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('data-delete-shop-form', html)
                self.assertIn('shop-actions.js', html)
                self.assertIn('name="csrf_token" value="token"', html)
                self.assertIn('name="confirm_permanent" value="0"', html)

    def test_all_dashboard_metrics_offer_shop_details_instead_of_delete(self):
        for metric in ('sales', 'daily_sales', 'admirers', 'item_count', 'rating', 'review_count', 'bad_review_count'):
            with self.subTest(metric=metric):
                response = self.client.get(f'/dashboard?metric={metric}&days=7')
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('class="shop-data-link" href="/shops/1/data?days=7"', html)
                self.assertIn('>单店详情 <span', html)
                self.assertNotIn('data-delete-shop-form', html)
                self.assertNotIn('shop-actions.js', html)

    def test_legacy_shop_delete_works_without_queue_tables(self):
        path=self.data/'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db,db:
            db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'Shop','shop','https://www.etsy.com/shop/Shop','now')")
        self.assertTrue(Store(path).delete_shop(1))
        self.assertFalse(Store(path).delete_shop(1))


    def test_shared_batches_and_request_groups_keep_other_shops_only(self):
        all_job=self.store.submit('all')['job']
        own_job=self.store.submit('shop',[1])['job']
        first=self.store.claim('w');self.store.finish(first['id'],'failed',metrics_status='failed')
        second=self.store.claim('w');self.store.finish(second['id'],'succeeded',metrics_status='success',reviews_status='completed')
        with self.store.connect(write=True) as db:
            db.execute("INSERT INTO operators(id,name,name_key,created_at) VALUES(1,'Shared','shared','now')")
            db.execute('UPDATE shops SET operator_id=1')
            db.execute("INSERT INTO collection_runs(id,started_at,status,total_shops,success_count,failed_count) VALUES(2,'2026-11-15','completed',2,1,1)")
            db.execute('UPDATE collection_tasks SET run_id=2')
            for shop,status in [(1,'failed'),(2,'success')]:
                db.execute("INSERT INTO collection_results(run_id,shop_id,collected_at,status,sales) VALUES(2,?,'2026-11-15T08:00:00+00:00',?,?)",(shop,status,shop*100))
                db.execute("INSERT INTO shop_snapshots(run_id,shop_id,collected_at,status,sales) VALUES(2,?,'2026-11-15T08:00:00+00:00',?,?)",(shop,status,shop*100))
                db.execute("INSERT INTO review_sync_runs(collection_run_id,shop_id,started_at,status) VALUES(2,?,'2026-11-15','completed')",(shop,))
                db.execute("INSERT INTO finance_demo VALUES(?,'2026-11-15',100,50,10,0)",(shop,))
            db.execute("INSERT INTO reviews(shop_id,review_id,stars,first_seen_at,last_seen_at) VALUES(2,'other-real',5,'2026-11-15','2026-11-15')")
            before={table:[tuple(r) for r in db.execute('SELECT * FROM '+table+' WHERE shop_id=2 ORDER BY rowid')] for table in ['shop_snapshots','collection_results','reviews','review_sync_runs','finance_demo','collection_tasks']}
        self.assertTrue(self.store.delete_shop(1))
        with self.store.connect() as db:
            for table,rows in before.items():
                self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table+' WHERE shop_id=2 ORDER BY rowid')],rows,table)
                self.assertEqual(db.execute('SELECT count(*) FROM '+table+' WHERE shop_id=1').fetchone()[0],0,table)
            self.assertIsNone(db.execute('SELECT id FROM collection_runs WHERE id=1').fetchone())
            self.assertEqual(tuple(db.execute('SELECT total_shops,success_count,partial_count,failed_count FROM collection_runs WHERE id=2').fetchone()),(1,1,0,0))
            self.assertEqual(db.execute('SELECT count(*) FROM operators').fetchone()[0],1)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        with self.assertRaises(QueueError):self.store.job(own_job['id'])
        surviving=self.store.job(all_job['id'])
        self.assertEqual(surviving['status'],'completed')
        self.assertEqual([t['shop_id'] for t in surviving['tasks']],[2])

    def test_empty_selection_job_is_removed_with_its_last_shop(self):
        job=self.store.submit('selection',[1])['job']
        self.store.delete_shop(1)
        with self.assertRaises(QueueError):self.store.job(job['id'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM collection_job_tasks').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM collection_tasks').fetchone()[0],0)

    def test_new_unknown_shop_table_cancels_deletion_without_partial_changes(self):
        with self.store.connect(write=True) as db:
            db.execute('CREATE TABLE future_shop_data(shop_id INTEGER,payload TEXT)')
            db.execute("INSERT INTO future_shop_data VALUES(1,'keep until supported')")
        with self.assertRaises(QueueError) as error:self.store.delete_shop(1)
        self.assertIn('尚未支持',str(error.exception))
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM shop_snapshots WHERE shop_id=1').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reviews WHERE shop_id=1').fetchone()[0],1)

    def test_unhandled_foreign_key_rolls_back_every_child_delete(self):
        with self.store.connect(write=True) as db:
            db.execute('CREATE TABLE unrelated_name(parent_id INTEGER REFERENCES shops(id))')
            db.execute('INSERT INTO unrelated_name VALUES(1)')
        with self.assertRaises(QueueError):self.store.delete_shop(1)
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM shop_snapshots WHERE shop_id=1').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM collection_results WHERE shop_id=1').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM collection_runs WHERE id=1').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM reviews WHERE shop_id=1').fetchone()[0],1)

    def test_legacy_collector_resource_lock_blocks_deletion(self):
        from etsy_runtime.locks import FileLock
        with FileLock(self.data/'collector.lock'),self.assertRaises(QueueError):self.store.delete_shop(1)
        with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())

    def test_running_other_shop_also_blocks_for_safe_resource_handoff(self):
        self.store.submit('shop',[2]);task=self.store.claim('w')
        with self.assertRaises(QueueError):self.store.delete_shop(1)
        with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
        self.store.finish(task['id'],'succeeded')

    def test_old_or_unconfirmed_forms_never_trigger_permanent_deletion(self):
        for payload in [{'csrf_token':'token'},{'csrf_token':'token','confirm_permanent':'0'}]:
            response=self.client.post('/shops/1/delete',data=payload,follow_redirects=True)
            self.assertEqual(response.status_code,200)
            self.assertIn('完成永久删除确认',response.get_data(as_text=True))
            with self.store.connect() as db:self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())

    def test_delete_confirmation_warns_about_permanent_scope(self):
        source=(ROOT/'releases/v22/static/shop-actions.js').read_text(encoding='utf-8')
        self.assertIn('无法撤销',source)
        self.assertIn('重新添加同一链接不会恢复历史',source)
        self.assertIn("confirmation.value = '1'",source)

    def test_restricted_queue_does_not_resume_when_its_shop_is_deleted(self):
        self.store.submit('shop',[1]);task=self.store.claim('w')
        self.store.finish(task['id'],'blocked',error='restricted')
        self.store.delete_shop(1)
        self.assertTrue(self.store.state()['worker']['paused'])

    def test_database_deletion_does_not_remove_shared_files_or_backups(self):
        image_dir=self.data/'review_images';image_dir.mkdir();image=image_dir/'cached.jpg';image.write_bytes(b'cache')
        backups=self.data/'backups';backups.mkdir();backup=backups/'prior.sqlite3';backup.write_bytes(b'backup')
        logs=self.data/'logs';logs.mkdir();log=logs/'collection-task-1.log';log.write_text('diagnostic',encoding='utf-8')
        self.store.delete_shop(1)
        self.assertTrue(image.exists() and backup.exists() and log.exists())


    def test_delete_and_worker_claim_cannot_leave_an_orphan_task(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.store.submit('all')
        barrier=Barrier(2)
        def remove():
            barrier.wait()
            try:return self.store.delete_shop(1)
            except QueueError as error:
                self.assertEqual(error.code,'shop_delete_blocked')
                return False
        def claim():
            barrier.wait()
            return Store(self.database).claim('racing-worker')
        with ThreadPoolExecutor(max_workers=2) as pool:
            removed=pool.submit(remove);claimed=pool.submit(claim)
            was_removed=removed.result(timeout=20);task=claimed.result(timeout=20)
        with self.store.connect() as db:
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=2').fetchone())
            if was_removed:
                self.assertIsNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
                self.assertEqual(task['shop_id'],2)
                self.assertEqual(db.execute('SELECT count(*) FROM collection_tasks WHERE shop_id=1').fetchone()[0],0)
            else:
                self.assertIsNotNone(db.execute('SELECT id FROM shops WHERE id=1').fetchone())
                self.assertEqual(task['shop_id'],1)


if __name__=='__main__':unittest.main()
