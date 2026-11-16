"""Transactional, shop-scoped database deletion with shared-batch preservation."""

from datetime import datetime, timezone


SHOP_TABLES = {
    "collection_job_tasks", "collection_jobs", "collection_tasks",
    "shop_snapshots", "collection_results", "review_sync_runs", "reviews", "finance_demo",
}
RUN_REFERENCES = (("shop_snapshots", "run_id"), ("collection_results", "run_id"),
                  ("review_sync_runs", "collection_run_id"), ("collection_tasks", "run_id"))


class ShopDeletionBlocked(ValueError):
    pass


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _tables(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def assert_no_active_collections(db):
    tables = _tables(db)
    if "collection_tasks" in tables and db.execute("SELECT 1 FROM collection_tasks WHERE status='running' LIMIT 1").fetchone():
        raise ShopDeletionBlocked("当前有采集任务正在执行，请等待结束后再永久删除店铺，避免采集进程重新写入数据。")


def delete_shop_records(db, shop_id, now):
    """Caller owns BEGIN IMMEDIATE and the collector lock for the whole operation."""
    if not db.execute("SELECT 1 FROM shops WHERE id=?", (shop_id,)).fetchone():
        return None
    tables = _tables(db)
    # Unknown non-FK tables must not silently leave personal/shop data behind.
    for name in tables - SHOP_TABLES:
        if "shop_id" in {row[1] for row in db.execute("PRAGMA table_info(" + _quote(name) + ")")}:
            raise ShopDeletionBlocked(f"发现尚未支持的店铺关联表 {name}，已取消删除，原数据未改动。")
    run_ids = set()
    for table, column in RUN_REFERENCES:
        if table in tables:
            run_ids.update(row[0] for row in db.execute(f"SELECT DISTINCT {column} FROM {table} WHERE shop_id=? AND {column} IS NOT NULL", (shop_id,)))
    affected_jobs = set()
    if "collection_jobs" in tables:
        affected_jobs.update(r[0] for r in db.execute("SELECT id FROM collection_jobs WHERE shop_id=?", (shop_id,)))
        if "collection_job_tasks" in tables and "collection_tasks" in tables:
            affected_jobs.update(r[0] for r in db.execute("""SELECT DISTINCT l.job_id FROM collection_job_tasks l
                JOIN collection_tasks t ON t.id=l.task_id WHERE t.shop_id=?""", (shop_id,)))
            db.execute("""DELETE FROM collection_job_tasks WHERE task_id IN
                (SELECT id FROM collection_tasks WHERE shop_id=?) OR job_id IN
                (SELECT id FROM collection_jobs WHERE shop_id=?)""", (shop_id, shop_id))
        db.execute("DELETE FROM collection_jobs WHERE shop_id=?", (shop_id,))
        if "collection_job_tasks" in tables:
            for job_id in affected_jobs:
                db.execute("""DELETE FROM collection_jobs WHERE id=? AND NOT EXISTS
                    (SELECT 1 FROM collection_job_tasks WHERE job_id=?)""", (job_id, job_id))
    for table in ("collection_tasks", "review_sync_runs", "reviews", "finance_demo", "collection_results", "shop_snapshots"):
        if table in tables:
            db.execute(f"DELETE FROM {table} WHERE shop_id=?", (shop_id,))
    removed_runs = 0
    for run_id in run_ids:
        surviving_shops = set()
        for table, column in RUN_REFERENCES:
            if table in tables:
                surviving_shops.update(r[0] for r in db.execute(f"SELECT DISTINCT shop_id FROM {table} WHERE {column}=?", (run_id,)))
        if not surviving_shops:
            db.execute("DELETE FROM collection_runs WHERE id=?", (run_id,))
            removed_runs += 1
            continue
        # Per-run audit has priority over the current daily projection.
        statuses = {}
        if "collection_tasks" in tables:
            for row in db.execute("SELECT shop_id,metrics_status FROM collection_tasks WHERE run_id=?", (run_id,)):
                statuses[row[0]] = row[1] or "failed"
        for table in ("shop_snapshots", "collection_results"):
            if table in tables:
                for row in db.execute(f"SELECT shop_id,status FROM {table} WHERE run_id=?", (run_id,)):
                    statuses[row[0]] = row[1]
        counts = {status: sum(statuses.get(shop, "failed") == status for shop in surviving_shops)
                  for status in ("success", "partial")}
        db.execute("""UPDATE collection_runs SET total_shops=?,success_count=?,partial_count=?,failed_count=?,
            status=CASE WHEN status='running' THEN 'interrupted' ELSE status END,
            finished_at=CASE WHEN status='running' THEN ? ELSE finished_at END WHERE id=?""",
            (len(surviving_shops), counts["success"], counts["partial"],
             len(surviving_shops)-sum(counts.values()), datetime.fromtimestamp(now, timezone.utc).isoformat(), run_id))
    db.execute("DELETE FROM shops WHERE id=?", (shop_id,))
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ShopDeletionBlocked("关联约束检查未通过，已取消删除，原数据未改动。")
    return {"affected_jobs": affected_jobs, "removed_runs": removed_runs}
