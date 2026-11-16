"""SQLite-backed collection jobs and per-shop FIFO tasks; no in-memory queue."""

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .locks import FileLock, LockBusy
from .shop_deletion import ShopDeletionBlocked, assert_no_active_collections, delete_shop_records

COOLDOWN_SECONDS = 60
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "partial", "failed", "blocked", "interrupted", "skipped")
SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_jobs (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK(scope IN ('all','shop','selection')),
    shop_id INTEGER REFERENCES shops(id),
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS collection_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    shop_name TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    owner TEXT,
    run_id INTEGER REFERENCES collection_runs(id),
    metrics_status TEXT,
    reviews_status TEXT,
    error_code TEXT,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_one_active_shop
    ON collection_tasks(shop_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_collection_task_fifo ON collection_tasks(status,id);
CREATE TABLE IF NOT EXISTS collection_job_tasks (
    job_id TEXT NOT NULL REFERENCES collection_jobs(id),
    task_id INTEGER NOT NULL REFERENCES collection_tasks(id),
    reused TEXT,
    PRIMARY KEY(job_id,task_id)
);
CREATE TABLE IF NOT EXISTS collection_control (
    id INTEGER PRIMARY KEY CHECK(id=1),
    paused_reason TEXT,
    worker_id TEXT,
    worker_pid INTEGER,
    heartbeat_at REAL,
    resource_message TEXT
);
INSERT OR IGNORE INTO collection_control(id) VALUES(1);
"""


class QueueError(ValueError):
    def __init__(self, message, code="invalid_request", status=400, retry_after=None):
        super().__init__(message)
        self.code, self.status, self.retry_after = code, status, retry_after


class Store:
    def __init__(self, path, clock=time.time):
        self.path = Path(path).resolve()
        self.clock = clock

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        with self.connect() as db:
            db.executescript(SCHEMA)

    def _refresh_jobs(self, db, now, extra_ids=()):
        extra_ids = list(extra_ids)
        query = "SELECT id,finished_at FROM collection_jobs WHERE status IN ('queued','running')"
        if extra_ids:
            query += " OR id IN (%s)" % ",".join("?" * len(extra_ids))
        for job in db.execute(query, extra_ids).fetchall():
            states = [r[0] for r in db.execute("SELECT t.status FROM collection_tasks t JOIN collection_job_tasks l ON l.task_id=t.id WHERE l.job_id=?", (job["id"],))]
            if "running" in states:
                status = "running"
            elif "queued" in states:
                status = "running" if any(s not in ACTIVE for s in states) else "queued"
            elif "blocked" in states:
                status = "blocked"
            elif "interrupted" in states:
                status = "interrupted"
            elif states and all(s == "failed" for s in states):
                status = "failed"
            elif "failed" in states or "partial" in states:
                status = "partial"
            else:
                status = "completed"
            db.execute("UPDATE collection_jobs SET status=?,finished_at=? WHERE id=?", (status, None if status in ACTIVE else (job["finished_at"] if job["finished_at"] is not None else now), job["id"]))

    def submit(self, scope, shop_ids=None, origin="web"):
        if scope not in ("all", "shop", "selection"):
            raise QueueError("刷新范围无效。")
        ids = list(dict.fromkeys(shop_ids or []))
        if scope != "all" and (not ids or any(type(i) is not int or not 1 <= i <= 9223372036854775807 for i in ids)):
            raise QueueError("店铺 ID 无效。")
        if scope == "shop" and len(ids) != 1:
            raise QueueError("单店刷新必须指定一家店铺。")
        now = self.clock()
        with self.connect(write=True) as db:
            self._refresh_jobs(db, now)
            if db.execute("SELECT paused_reason FROM collection_control WHERE id=1").fetchone()[0]:
                raise QueueError("采集队列因访问受限已暂停。请先确认正常访问恢复，再解除暂停。", "queue_paused", 409)
            if scope == "all":
                existing = db.execute("SELECT id FROM collection_jobs WHERE scope='all' AND status IN ('queued','running') ORDER BY created_at LIMIT 1").fetchone()
            elif scope == "shop":
                existing = db.execute("SELECT id FROM collection_jobs WHERE scope='shop' AND shop_id=? AND status IN ('queued','running') ORDER BY created_at LIMIT 1", (ids[0],)).fetchone()
            else:
                existing = None
            if existing:
                return {"job": self._job(db, existing["id"]), "merged": True}
            if scope == "all":
                shops = db.execute("SELECT id,name FROM shops WHERE deleted_at IS NULL ORDER BY id").fetchall()
            else:
                shops = db.execute("SELECT id,name FROM shops WHERE deleted_at IS NULL AND id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids).fetchall()
                if len(shops) != len(ids):
                    raise QueueError("指定店铺不存在或已删除。", "shop_not_found", 404)
            if not shops:
                raise QueueError("采集名单为空，请先添加店铺。", "no_shops")
            task_links = []
            for shop in shops:
                active = db.execute("SELECT id FROM collection_tasks WHERE shop_id=? AND status IN ('queued','running')", (shop["id"],)).fetchone()
                if active:
                    task_links.append((active["id"], "active"))
                    continue
                recent = db.execute("SELECT id,finished_at FROM collection_tasks WHERE shop_id=? AND finished_at>? AND status<>'skipped' ORDER BY finished_at DESC LIMIT 1", (shop["id"], now - COOLDOWN_SECONDS)).fetchone()
                if recent:
                    if scope == "shop":
                        seconds = max(1, int(COOLDOWN_SECONDS - (now - recent["finished_at"]) + .999))
                        raise QueueError(f"该店刚完成采集，请在 {seconds} 秒后刷新。", "cooldown", 429, seconds)
                    task_links.append((recent["id"], "cooldown"))
                    continue
                task_id = db.execute("INSERT INTO collection_tasks(shop_id,shop_name,status,created_at) VALUES(?,?,'queued',?)", (shop["id"], shop["name"], now)).lastrowid
                task_links.append((task_id, None))
            job_id = uuid.uuid4().hex
            db.execute("INSERT INTO collection_jobs(id,scope,shop_id,origin,status,created_at) VALUES(?,?,?,?,'queued',?)", (job_id, scope, ids[0] if scope == "shop" else None, origin, now))
            db.executemany("INSERT INTO collection_job_tasks(job_id,task_id,reused) VALUES(?,?,?)", [(job_id, task_id, reused) for task_id, reused in task_links])
            self._refresh_jobs(db, now)
            return {"job": self._job(db, job_id), "merged": any(reused for _, reused in task_links)}

    def _job(self, db, job_id):
        row = db.execute("SELECT * FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise QueueError("任务不存在。", "job_not_found", 404)
        job = dict(row)
        job["tasks"] = [dict(r) for r in db.execute("SELECT t.id,t.shop_id,t.shop_name,t.status,t.phase,t.created_at,t.started_at,t.finished_at,t.run_id,t.metrics_status,t.reviews_status,t.error_code,t.error,l.reused FROM collection_tasks t JOIN collection_job_tasks l ON l.task_id=t.id WHERE l.job_id=? ORDER BY t.id", (job_id,))]
        counts = {s: sum(t["status"] == s for t in job["tasks"]) for s in ACTIVE + TERMINAL}
        job["progress"] = {"total": len(job["tasks"]), "done": sum(counts[s] for s in TERMINAL), **counts}
        return job

    def job(self, job_id):
        with self.connect() as db:
            return self._job(db, job_id)

    def delete_job(self, job_id):
        """Delete a terminal request record, not collection data or shared tasks."""
        with self.connect(write=True) as db:
            self._refresh_jobs(db, self.clock())
            job = db.execute("SELECT status FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise QueueError("刷新记录不存在或已删除。", "job_not_found", 404)
            active = db.execute("""SELECT 1 FROM collection_tasks t
                JOIN collection_job_tasks l ON l.task_id=t.id
                WHERE l.job_id=? AND t.status IN ('queued','running') LIMIT 1""", (job_id,)).fetchone()
            if job["status"] in ACTIVE or active:
                raise QueueError("进行中的刷新记录不能删除，请等待任务结束。", "job_active", 409)
            db.execute("DELETE FROM collection_job_tasks WHERE job_id=?", (job_id,))
            db.execute("DELETE FROM collection_jobs WHERE id=?", (job_id,))
            # Keep task outcomes for cooldown/dedup and other request groups.
            # Browser diagnostic files and business audit rows are independent.
        return {"deleted_job_id": job_id,
                "message": "刷新记录已从数据库删除；采集数据、冷却状态和诊断日志保留。"}

    def delete_shop(self, shop_id):
        """Permanently remove a shop and its dependent rows, atomically.

        Acquire the write transaction before the OS resource lock: a queued
        worker cannot claim a task while deletion is choosing its scope. Never
        touch an active collector's lock or kill another shop's process.
        """
        try:
            with self.connect(write=True) as db:
                if not db.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
                    return False
                assert_no_active_collections(db)
                with FileLock(self.path.parent / "collector.lock"):
                    result = delete_shop_records(db, shop_id, self.clock())
                    if result is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_jobs'").fetchone():
                        self._refresh_jobs(db, self.clock(), result["affected_jobs"])
                return result is not None
        except (LockBusy, ShopDeletionBlocked, sqlite3.IntegrityError) as exc:
            message = (str(exc) if isinstance(exc, ShopDeletionBlocked) else
                       "数据库存在尚未处理的关联约束，已取消删除，原数据未改动。" if isinstance(exc, sqlite3.IntegrityError) else
                       "采集浏览器正在运行，请等待采集结束后再永久删除店铺。")
            raise QueueError(message, "shop_delete_blocked", 409) from exc

    def state(self):
        now = self.clock()
        with self.connect() as db:
            control = dict(db.execute("SELECT * FROM collection_control WHERE id=1").fetchone())
            online = bool(control["worker_id"] and control["heartbeat_at"] is not None and now - control["heartbeat_at"] < 15)
            ids = [r[0] for r in db.execute("SELECT id FROM collection_jobs ORDER BY (status IN ('queued','running')) DESC,created_at DESC LIMIT 8")]
            tasks = [dict(r) for r in db.execute("SELECT id,shop_id,shop_name,status,phase,finished_at FROM collection_tasks WHERE status IN ('queued','running') OR finished_at>? ORDER BY id", (now - COOLDOWN_SECONDS,))]
            return {"server_time": now, "active_shop_count": db.execute("SELECT count(*) FROM shops WHERE deleted_at IS NULL").fetchone()[0], "worker": {"online": online, "paused": bool(control["paused_reason"]), "paused_reason": control["paused_reason"], "resource_message": control["resource_message"]}, "jobs": [self._job(db, i) for i in ids], "tasks": tasks, "cooldown_seconds": COOLDOWN_SECONDS}

    def register_worker(self, worker_id, pid):
        with self.connect(write=True) as db:
            db.execute("UPDATE collection_control SET worker_id=?,worker_pid=?,heartbeat_at=? WHERE id=1", (worker_id, pid, self.clock()))

    def heartbeat(self, worker_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE collection_control SET heartbeat_at=? WHERE id=1 AND worker_id=?", (self.clock(), worker_id))

    def resource_message(self, worker_id, message):
        with self.connect(write=True) as db:
            db.execute("UPDATE collection_control SET resource_message=? WHERE id=1 AND worker_id=?", (message, worker_id))

    def unregister_worker(self, worker_id):
        with self.connect(write=True) as db:
            db.execute("UPDATE collection_control SET worker_id=NULL,worker_pid=NULL,heartbeat_at=NULL,resource_message=NULL WHERE id=1 AND worker_id=?", (worker_id,))

    def recover_interrupted(self):
        now = self.clock()
        with self.connect(write=True) as db:
            # A collector can finish with a restriction after its parent Worker
            # has crashed. Preserve that stop signal before recovering other work.
            blocked = [r[0] for r in db.execute("""SELECT t.id FROM collection_tasks t
                LEFT JOIN collection_runs r ON r.id=t.run_id
                WHERE t.status='running' AND (r.status='blocked' OR EXISTS (
                    SELECT 1 FROM review_sync_runs s
                    WHERE s.collection_run_id=t.run_id AND s.status='blocked'))""")]
            if blocked:
                for task_id in blocked:
                    db.execute("UPDATE collection_tasks SET status='blocked',phase='finished',finished_at=?,error_code='access_restricted',error='恢复检查发现上次采集遭遇访问限制；队列已暂停。' WHERE id=?", (now, task_id))
                db.execute("UPDATE collection_control SET paused_reason='上次采集遭遇 Etsy 访问限制；请人工确认正常访问恢复。' WHERE id=1")
                db.execute("UPDATE collection_tasks SET status='blocked',phase='finished',finished_at=?,error_code='access_restricted',error='未访问：检测到访问限制，队列已停止。' WHERE status='queued'", (now,))
            runs = [r[0] for r in db.execute("SELECT run_id FROM collection_tasks WHERE status='running' AND run_id IS NOT NULL")]
            for run_id in runs:
                db.execute("UPDATE collection_runs SET status='interrupted',finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now') WHERE id=? AND status='running'", (run_id,))
                db.execute("UPDATE review_sync_runs SET status='interrupted',finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now') WHERE collection_run_id=? AND status='running'", (run_id,))
            db.execute("UPDATE collection_tasks SET status='interrupted',phase='finished',finished_at=?,error_code='worker_interrupted',error='采集进程意外退出；未自动重试，请手动重新刷新。' WHERE status='running'", (now,))
            self._refresh_jobs(db, now)

    def claim(self, worker_id):
        now = self.clock()
        with self.connect(write=True) as db:
            if db.execute("SELECT paused_reason FROM collection_control WHERE id=1").fetchone()[0]:
                return None
            # Worker OS lock is the primary guard; this also defends accidental callers.
            if db.execute("SELECT 1 FROM collection_tasks WHERE status='running'").fetchone():
                return None
            while True:
                task = db.execute("SELECT t.*,s.deleted_at FROM collection_tasks t JOIN shops s ON s.id=t.shop_id WHERE t.status='queued' ORDER BY t.id LIMIT 1").fetchone()
                if task is None:
                    self._refresh_jobs(db, now)
                    return None
                if task["deleted_at"] is not None:
                    db.execute("UPDATE collection_tasks SET status='skipped',phase='finished',finished_at=?,error='店铺已删除，未访问。' WHERE id=?", (now, task["id"]))
                    continue
                db.execute("UPDATE collection_tasks SET status='running',phase='metrics',started_at=?,owner=? WHERE id=? AND status='queued'", (now, worker_id, task["id"]))
                self._refresh_jobs(db, now)
                return dict(db.execute("SELECT * FROM collection_tasks WHERE id=?", (task["id"],)).fetchone())

    def finish(self, task_id, status, metrics_status=None, reviews_status=None, error=None, error_code=None):
        if status not in TERMINAL:
            raise ValueError("Invalid terminal task status")
        now = self.clock()
        with self.connect(write=True) as db:
            changed = db.execute("UPDATE collection_tasks SET status=?,phase='finished',finished_at=?,metrics_status=?,reviews_status=?,error=?,error_code=? WHERE id=? AND status='running'", (status, now, metrics_status, reviews_status, error, error_code, task_id)).rowcount
            if not changed:
                return
            if status in ("interrupted", "failed"):
                run = db.execute("SELECT run_id FROM collection_tasks WHERE id=?", (task_id,)).fetchone()
                if run and run["run_id"]:
                    db.execute("UPDATE collection_runs SET status='interrupted',finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now') WHERE id=? AND status='running'", (run["run_id"],))
                    db.execute("UPDATE review_sync_runs SET status='interrupted',finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now') WHERE collection_run_id=? AND status='running'", (run["run_id"],))
            if status == "blocked":
                reason = error or "Etsy 临时限制访问，请确认正常访问恢复后再解除暂停。"
                db.execute("UPDATE collection_control SET paused_reason=? WHERE id=1", (reason,))
                db.execute("UPDATE collection_tasks SET status='blocked',phase='finished',finished_at=?,error_code='access_restricted',error='未访问：检测到访问限制，队列已停止。' WHERE status='queued'", (now,))
            self._refresh_jobs(db, now)

    def resume(self):
        with self.connect(write=True) as db:
            db.execute("UPDATE collection_control SET paused_reason=NULL WHERE id=1")
        # Old blocked tasks remain terminal; resuming never retries them automatically.
