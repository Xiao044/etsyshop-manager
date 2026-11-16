"""Single persistent worker. Executes the existing collector, never HTTP scraping."""

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .locks import FileLock, LockBusy
from .queue import Store
from .snapshots import collection_result

ROOT = Path(__file__).resolve().parents[1]


def stop_process_tree(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=7)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait(timeout=7)


class CollectorExecutor:
    def __init__(self, store, timeout=45 * 60):
        self.store, self.timeout = store, timeout

    def __call__(self, task, heartbeat):
        data = self.store.path.parent
        logs = data / "logs"
        logs.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["ETSY_TOOLS_DATA_DIR"] = str(data)
        env.setdefault("ETSY_BROWSER_PROFILE", str(data / "browser-profile"))
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        args = [sys.executable, "-u", str(ROOT / "releases/v11/collector.py"), "--shop-id", str(task["shop_id"]), "--task-id", str(task["id"])]
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        started = time.monotonic()
        with (logs / f"collection-task-{task['id']}.log").open("ab") as log:
            process = subprocess.Popen(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, **kwargs)
            try:
                while process.poll() is None:
                    heartbeat()
                    if time.monotonic() - started > self.timeout:
                        stop_process_tree(process)
                        return {"status": "interrupted", "error_code": "timeout", "error": "采集超过 45 分钟，已终止本店任务；未自动重试。"}
                    time.sleep(1)
            except BaseException:
                stop_process_tree(process)
                raise
        with self.store.connect() as db:
            run = db.execute("SELECT r.* FROM collection_runs r JOIN collection_tasks t ON t.run_id=r.id WHERE t.id=?", (task["id"],)).fetchone()
            if run is None:
                return {"status": "failed", "error_code": "collector_start_failed", "error": "采集器未正常启动；请检查本地采集日志和浏览器占用情况。"}
            snapshot = collection_result(db, run["id"], task["shop_id"])
            review = db.execute("SELECT * FROM review_sync_runs WHERE collection_run_id=? AND shop_id=? ORDER BY id DESC LIMIT 1", (run["id"], task["shop_id"])).fetchone()
            metrics = snapshot["status"] if snapshot else "failed"
            reviews = review["status"] if review else "skipped"
            error = (review["error"] if review else None) or (snapshot["error"] if snapshot else None)
            if run["status"] == "blocked" or reviews == "blocked":
                return {"status": "blocked", "metrics_status": metrics, "reviews_status": reviews, "error_code": "access_restricted", "error": error or "Etsy 暂时限制访问，已停止队列。"}
            if run["status"] not in ("completed", "blocked"):
                return {"status": "interrupted", "metrics_status": metrics, "reviews_status": reviews, "error_code": "collector_interrupted", "error": "采集器未完成批次收尾；已保存的数据保留，未自动重试。"}
            if process.returncode and metrics == "success" and reviews == "completed":
                return {"status": "failed", "metrics_status": metrics, "reviews_status": reviews, "error_code": "collector_exit", "error": "采集进程异常退出，请检查日志。"}
            status = "failed" if metrics == "failed" else ("succeeded" if metrics == "success" and reviews == "completed" else "partial")
            return {"status": status, "metrics_status": metrics, "reviews_status": reviews, "error": error}


def execute_one(store, worker_id, executor):
    task = store.claim(worker_id)
    if task is None:
        return False
    try:
        result = executor(task, lambda: store.heartbeat(worker_id))
    except (KeyboardInterrupt, SystemExit):
        store.finish(task["id"], "interrupted", error_code="worker_stopped", error="Worker 已停止；未自动重试。")
        raise
    except Exception:
        # Detailed exceptions stay in the worker log, not in a public error response.
        import traceback
        traceback.print_exc()
        result = {"status": "failed", "error_code": "worker_error", "error": "采集执行异常，请检查 Worker 日志。"}
    store.finish(task["id"], **result)
    print(f"task={task['id']} shop={task['shop_name']} status={result['status']}", flush=True)
    return True


def browser_port_busy():
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(("127.0.0.1", int(os.environ.get("ETSY_BROWSER_DEBUG_PORT", "9238")))) == 0


def run_worker(database, once=False, executor=None, sleep=time.sleep):
    store = Store(database)
    store.initialize()
    worker_id = uuid.uuid4().hex
    with FileLock(store.path.parent / "collection-worker.lock"):
        store.register_worker(worker_id, os.getpid())
        recovered = False
        try:
            while True:
                store.heartbeat(worker_id)
                try:
                    with FileLock(store.path.parent / "collector.lock"):
                        if not recovered:
                            store.recover_interrupted()
                            recovered = True
                except LockBusy:
                    store.resource_message(worker_id, "另一采集进程仍在运行，当前队列等待资源释放。")
                    if once:
                        return
                    sleep(1)
                    continue
                if executor is None and browser_port_busy():
                    store.resource_message(worker_id, "独立采集浏览器仍占用调试端口，请先关闭之前保留的采集窗口。")
                    if once:
                        return
                    sleep(1)
                    continue
                store.resource_message(worker_id, None)
                worked = execute_one(store, worker_id, executor or CollectorExecutor(store))
                if once:
                    return
                if not worked:
                    sleep(1)
        finally:
            store.unregister_worker(worker_id)
