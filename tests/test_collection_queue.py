import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "releases/v22"))
from etsy_runtime.queue import Store, QueueError
from etsy_runtime.locks import FileLock, LockBusy
from etsy_runtime.worker import execute_one, run_worker


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "shops.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
            for shop_id in (1,2,3):
                db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(?,?,?,?,?)", (shop_id,f"Shop{shop_id}",f"shop{shop_id}",f"https://www.etsy.com/shop/Shop{shop_id}","now"))
        self.now = 1000.0
        self.store = Store(self.database, clock=lambda:self.now)
        self.store.initialize()

    def test_all_and_single_share_tasks(self):
        all_job = self.store.submit("all")["job"]
        same = self.store.submit("all")
        single = self.store.submit("shop", [2])
        self.assertEqual(same["job"]["id"], all_job["id"])
        self.assertTrue(same["merged"])
        self.assertEqual(single["job"]["tasks"][0]["id"], all_job["tasks"][1]["id"])
        self.assertTrue(single["merged"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_tasks").fetchone()[0],3)

    def test_single_then_all_keeps_fifo_and_no_duplicates(self):
        first = self.store.submit("shop",[2])["job"]
        task = self.store.claim("worker")
        self.assertEqual(task["shop_id"],2)
        all_job = self.store.submit("all")["job"]
        self.assertEqual(all_job["tasks"][0]["id"],first["tasks"][0]["id"])
        self.assertIsNone(self.store.claim("another-worker"))
        self.store.finish(task["id"],"succeeded",metrics_status="success",reviews_status="completed")
        self.assertEqual(self.store.claim("worker")["shop_id"],1)

    def test_concurrent_clicks_atomically_deduplicate(self):
        def submit(_):
            return Store(self.database,clock=lambda:self.now).submit("shop",[1])["job"]["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit,range(16)))
        self.assertEqual(len(set(results)),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM collection_tasks").fetchone()[0],1)

    def test_cooldown_and_all_reuses_recent_result(self):
        first = self.store.submit("shop",[1])["job"]
        task = self.store.claim("w")
        self.store.finish(task["id"],"succeeded")
        with self.assertRaises(QueueError) as error:
            self.store.submit("shop",[1])
        self.assertEqual(error.exception.status,429)
        self.assertEqual(error.exception.retry_after,60)
        all_job = self.store.submit("all")["job"]
        self.assertEqual(all_job["tasks"][0]["reused"],"cooldown")
        self.assertEqual(all_job["tasks"][0]["id"],first["tasks"][0]["id"])
        self.now += 60
        fresh = self.store.submit("shop",[1])["job"]
        self.assertNotEqual(fresh["tasks"][0]["id"],task["id"])

    def test_restricted_access_pauses_pending_tasks_and_resume_never_retries(self):
        job = self.store.submit("all")["job"]
        task = self.store.claim("w")
        self.store.finish(task["id"],"blocked",error_code="access_restricted",error="restricted")
        result = self.store.job(job["id"])
        self.assertEqual(result["status"],"blocked")
        self.assertEqual(result["progress"]["blocked"],3)
        self.assertIn("未访问",result["tasks"][1]["error"])
        with self.assertRaises(QueueError) as error:
            self.store.submit("shop",[2])
        self.assertEqual(error.exception.code,"queue_paused")
        self.store.resume()
        self.assertIsNone(self.store.claim("w"))
        self.assertEqual(self.store.job(job["id"])["status"],"blocked")

    def test_ordinary_failure_continues_and_batch_is_partial(self):
        job = self.store.submit("all")["job"]
        results = iter([{"status":"failed"},{"status":"succeeded"},{"status":"partial","metrics_status":"success","reviews_status":"failed"}])
        with redirect_stdout(io.StringIO()):
            for _ in range(3):
                self.assertTrue(execute_one(self.store,"w",lambda task,heartbeat:next(results)))
        result = self.store.job(job["id"])
        self.assertEqual(result["status"],"partial")
        self.assertEqual(result["progress"]["done"],3)

    def test_restart_marks_active_task_interrupted_without_retry(self):
        job = self.store.submit("shop",[1])["job"]
        self.store.claim("old")
        reopened = Store(self.database,clock=lambda:self.now)
        reopened.recover_interrupted()
        self.assertEqual(reopened.job(job["id"])["status"],"interrupted")
        self.assertIsNone(reopened.claim("new"))

    def test_delete_queued_shop_skips_access(self):
        job = self.store.submit("shop",[1])["job"]
        with self.store.connect(write=True) as db:
            db.execute("UPDATE shops SET deleted_at='now' WHERE id=1")
        self.assertIsNone(self.store.claim("w"))
        self.assertEqual(self.store.job(job["id"])["tasks"][0]["status"],"skipped")

    def test_worker_heartbeat_expires(self):
        self.store.register_worker("w",123)
        self.assertTrue(self.store.state()["worker"]["online"])
        self.now += 16
        self.assertFalse(self.store.state()["worker"]["online"])

    def test_worker_once_uses_injected_executor_not_browser(self):
        job = self.store.submit("shop",[1])["job"]
        with redirect_stdout(io.StringIO()):
            run_worker(self.database,once=True,executor=lambda task,hb:{"status":"succeeded"})
        self.assertEqual(self.store.job(job["id"])["status"],"completed")
        self.assertFalse(self.store.state()["worker"]["online"])

    def test_os_lock_blocks_another_process_and_releases_after_exit(self):
        lock = self.database.parent / "test.lock"
        code = "from etsy_runtime.locks import FileLock,LockBusy; import sys;\ntry:\n with FileLock(sys.argv[1]): pass\nexcept LockBusy: sys.exit(23)"
        with FileLock(lock):
            result = subprocess.run([sys.executable,"-c",code,str(lock)],cwd=ROOT,capture_output=True)
            self.assertEqual(result.returncode,23,result.stderr)
        result = subprocess.run([sys.executable,"-c",code,str(lock)],cwd=ROOT,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_multiple_processes_cannot_create_duplicate_all_jobs(self):
        code = "from etsy_runtime.queue import Store; import sys; print(Store(sys.argv[1]).submit('all')['job']['id'])"
        children = [subprocess.Popen([sys.executable,"-c",code,str(self.database)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(4)]
        ids=[]
        for child in children:
            out,err=child.communicate(timeout=20)
            self.assertEqual(child.returncode,0,err)
            ids.append(out.strip())
        self.assertEqual(len(set(ids)),1)


    def test_browser_port_busy_keeps_tasks_queued_without_launching(self):
        job=self.store.submit("shop",[1])["job"]
        with mock.patch("etsy_runtime.worker.browser_port_busy",return_value=True), mock.patch("etsy_runtime.worker.CollectorExecutor") as executor:
            run_worker(self.database,once=True)
        executor.assert_not_called()
        self.assertEqual(self.store.job(job["id"])["status"],"queued")

    def test_legacy_collector_lock_keeps_queue_waiting(self):
        job=self.store.submit("shop",[1])["job"]
        with FileLock(self.database.parent/"collector.lock"):
            run_worker(self.database,once=True,executor=mock.Mock())
        self.assertEqual(self.store.job(job["id"])["status"],"queued")

    def test_interruption_also_finalizes_legacy_running_records(self):
        self.store.submit("shop",[1]);task=self.store.claim("w")
        with self.store.connect(write=True) as db:
            run=db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES('now','running',1)").lastrowid
            db.execute("UPDATE collection_tasks SET run_id=? WHERE id=?",(run,task["id"]))
            db.execute("INSERT INTO review_sync_runs(collection_run_id,shop_id,started_at,status) VALUES(?,1,'now','running')",(run,))
        self.store.finish(task["id"],"interrupted",error_code="timeout")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT status FROM collection_runs WHERE id=?",(run,)).fetchone()[0],"interrupted")
            self.assertEqual(db.execute("SELECT status FROM review_sync_runs WHERE collection_run_id=?",(run,)).fetchone()[0],"interrupted")

    def test_invalid_large_ids_are_validation_errors_not_sqlite_overflows(self):
        with self.assertRaises(QueueError):self.store.submit("shop",[10**100])


    def test_orphan_collector_restriction_is_not_lost_on_restart(self):
        job=self.store.submit("all")["job"]
        task=self.store.claim("old")
        with self.store.connect(write=True) as db:
            run=db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES('now','blocked',1)").lastrowid
            db.execute("UPDATE collection_tasks SET run_id=? WHERE id=?",(run,task["id"]))
        self.store.recover_interrupted()
        self.assertTrue(self.store.state()["worker"]["paused"])
        self.assertIsNone(self.store.claim("new"))
        recovered=self.store.job(job["id"])
        self.assertEqual(recovered["status"],"blocked")
        self.assertEqual(recovered["progress"]["blocked"],3)

    def test_repeated_finish_cannot_pause_an_already_completed_queue(self):
        self.store.submit("shop",[1]);task=self.store.claim("w")
        self.store.finish(task["id"],"succeeded")
        self.store.finish(task["id"],"blocked",error="stale callback")
        self.assertFalse(self.store.state()["worker"]["paused"])


class CollectionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data=Path(self.temp.name)
        self.database=data/'shops.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript((ROOT/'sql/schema.sql').read_text(encoding='utf-8'))
            db.execute("INSERT INTO shops(id,name,slug_key,url,created_at) VALUES(1,'AlphaShop','alpha','https://www.etsy.com/shop/AlphaShop','2026-11-16T00:00:00+00:00')")
        spec=importlib.util.spec_from_file_location('collection_web_test',ROOT/'releases/v22/app.py')
        self.web=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=self.web
        self.addCleanup(lambda:sys.modules.pop(spec.name,None))
        with mock.patch.dict(os.environ,{'ETSY_TOOLS_DATA_DIR':str(data),'ETSY_COLLECTION_ENABLED':'1'}):
            spec.loader.exec_module(self.web)
        self.web.app.config.update(TESTING=True, SERVER_NAME='127.0.0.1:8765')
        self.client=self.web.app.test_client()
        with self.client.session_transaction() as session:
            session['csrf_token']='test-token'
        self.headers={'X-CSRF-Token':'test-token'}

    def test_old_refresh_endpoints_are_removed_even_with_stale_flag(self):
        endpoints=[('POST','/api/collection/jobs'),('GET','/api/collection/state'),('GET','/api/collection/jobs/old'),('DELETE','/api/collection/jobs/old'),('POST','/api/collection/resume')]
        with mock.patch('subprocess.Popen',side_effect=AssertionError('No collection from HTTP')):
            for method,route in endpoints:
                result=self.client.open(route,method=method,json={'scope':'all'},headers=self.headers)
                self.assertEqual(result.status_code,404,(method,route))
        self.assertFalse(any('/api/collection/' in str(r) for r in self.web.app.url_map.iter_rules()))

    def test_all_pages_have_no_manual_refresh(self):
        for route in ['/','/dashboard','/dashboard?metric=item_count','/dashboard?metric=daily_sales','/shops/1/data']:
            result=self.client.get(route)
            self.assertEqual(result.status_code,200)
            html=result.get_data(as_text=True)
            for retired in ['data-refresh','collection.js','collection-api','collection-panel','collection-inline-status','data-collection-jobs','data-collection-refresh-region']:
                self.assertNotIn(retired,html,(route,retired))

    def test_loading_pages_does_not_start_worker_or_create_queue(self):
        with mock.patch('subprocess.Popen',side_effect=AssertionError('No Worker from web')):
            for route in ['/','/dashboard','/shops/1/data']:self.assertEqual(self.client.get(route).status_code,200)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='collection_jobs'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM collection_runs').fetchone()[0],0)

    def test_unused_manual_refresh_asset_is_not_served(self):
        self.assertEqual(self.client.get('/static/collection.js').status_code,404)
        for name in ['collection_controls.html','collection_inline.html']:
            self.assertFalse((ROOT/'releases/v22/templates'/name).exists())

    def test_existing_queue_history_is_preserved(self):
        store=Store(self.database);store.initialize()
        job=store.submit('shop',[1])['job'];task=store.claim('test')
        store.finish(task['id'],'succeeded')
        self.assertEqual(self.client.get('/').status_code,200)
        self.assertEqual(self.client.delete('/api/collection/jobs/'+job['id'],headers=self.headers).status_code,404)
        self.assertEqual(store.job(job['id'])['status'],'completed')

    def test_local_startup_runs_website_only(self):
        source=(ROOT/'scripts/start-windows.ps1').read_text(encoding='utf-8')
        starts=[line for line in source.splitlines() if 'Start-Process -FilePath' in line]
        self.assertEqual(len(starts),1)
        self.assertIn("'web'",starts[0])
        self.assertNotIn("'worker'",starts[0])
        self.assertNotIn('ETSY_COLLECTION_ENABLED',(ROOT/'scripts/local.py').read_text(encoding='utf-8'))

    def test_scheduled_service_is_unchanged_and_targets_all_shops(self):
        source=(ROOT/'releases/v11/etsy-collector.service').read_text(encoding='utf-8')
        original=subprocess.run(['git','show','HEAD:releases/v11/etsy-collector.service'],cwd=ROOT,capture_output=True,text=True,check=True).stdout
        self.assertEqual(source,original)
        self.assertIn('/usr/bin/flock',source)
        self.assertIn('/opt/etsy-tools/releases/v11/collector.py',source)
        self.assertNotIn('--shop-id',source)


if __name__=='__main__':
    unittest.main()
