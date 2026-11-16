"""One daily snapshot per shop; keep per-run collection results for auditing."""

import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path


METRIC_COLUMNS = ("sales", "admirers", "item_count", "on_sale_count", "rating",
                  "review_count", "review_count_display")
RESULT_COLUMNS = ("run_id", "shop_id", "collected_at", "status", *METRIC_COLUMNS,
                  "error", "is_simulated")
COLUMNS_SQL = ",".join(RESULT_COLUMNS)
DAY_SQL = "date(datetime(collected_at,'+8 hours'))"
RESULTS_SCHEMA = """CREATE TABLE IF NOT EXISTS collection_results (
    run_id INTEGER NOT NULL REFERENCES collection_runs(id),
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    collected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    sales INTEGER,
    admirers INTEGER,
    item_count INTEGER,
    on_sale_count INTEGER,
    rating REAL,
    review_count INTEGER,
    review_count_display TEXT,
    error TEXT,
    is_simulated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id,shop_id)
)"""
DAILY_INDEX_SQL = f"""CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_shop_day
    ON shop_snapshots(shop_id,{DAY_SQL})"""


@contextmanager
def _atomic(db):
    # SAVEPOINT also works inside the collector's existing transaction. A failed
    # migration or write cannot leave the audit and daily records out of sync.
    db.execute("SAVEPOINT daily_snapshots")
    try:
        yield
        db.execute("RELEASE SAVEPOINT daily_snapshots")
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT daily_snapshots")
        db.execute("RELEASE SAVEPOINT daily_snapshots")
        raise


def _usable(alias=""):
    prefix = alias + "." if alias else ""
    fields = " OR ".join(f"{prefix}{key} IS NOT NULL" for key in METRIC_COLUMNS)
    return f"({prefix}status IN ('success','partial') AND ({fields}))"


def backup_before_daily_migration(database_path):
    """Create a consistent, private SQLite backup before changing legacy data.

    Call outside a write transaction and while holding the collector lock. The
    backup includes reviews and queue tables, not just the daily snapshot table.
    """
    path = Path(database_path).resolve()
    if not path.is_file():
        return None
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)) as source:
        exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shop_snapshots'").fetchone()
        indexed = source.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_snapshots_shop_day'").fetchone()
        if not exists or indexed or not source.execute("SELECT 1 FROM shop_snapshots LIMIT 1").fetchone():
            return None
        directory = path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, filename = tempfile.mkstemp(prefix="shops-before-daily-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-"),
                                               suffix=".sqlite3", dir=directory)
        os.close(descriptor)
        backup = Path(filename)
        try:
            with closing(sqlite3.connect(backup)) as target:
                source.backup(target)
                if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("整理前的数据库备份未通过完整性检查，已取消迁移。")
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    return backup


def ensure_daily_snapshots_schema(db):
    """Atomically archive legacy runs, fold duplicate days, and enforce uniqueness.

    Existing real values outrank demo data; usable data outranks failed attempts.
    Within the same source, the latest usable snapshot wins. Original per-run
    values remain in collection_results, so no batch history needs to be lost.
    """
    with _atomic(db):
        columns = {row[1] for row in db.execute("PRAGMA table_info(shop_snapshots)")}
        for name, declaration in (("on_sale_count", "INTEGER"), ("review_count_display", "TEXT"),
                                  ("is_simulated", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                db.execute(f"ALTER TABLE shop_snapshots ADD COLUMN {name} {declaration}")
        db.execute(RESULTS_SCHEMA)
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_snapshots_shop_day'").fetchone():
            return
        if db.execute(f"SELECT 1 FROM shop_snapshots WHERE {DAY_SQL} IS NULL LIMIT 1").fetchone():
            raise ValueError("存在无法解析采集日期的快照，已取消每日快照整理；原数据未改动。")
        db.execute(f"INSERT OR IGNORE INTO collection_results ({COLUMNS_SQL}) SELECT {COLUMNS_SQL} FROM shop_snapshots")
        db.execute(f"""DELETE FROM shop_snapshots WHERE id IN (
            SELECT id FROM (
                SELECT id,ROW_NUMBER() OVER (
                    PARTITION BY shop_id,{DAY_SQL}
                    ORDER BY {_usable()} DESC,is_simulated ASC,julianday(collected_at) DESC,id DESC
                ) AS position FROM shop_snapshots
            ) WHERE position > 1
        )""")
        db.execute(DAILY_INDEX_SQL)


def save_snapshot(db, run_id, shop_id, collected_at, status, values=None, error=None, is_simulated=False):
    """Upsert by Beijing calendar day and retain this run's independent outcome.

    Missing fields in a partial refresh keep known same-day fields from the same
    source. Never mix demo values into a real partial refresh. A failed attempt
    is audited but cannot erase usable daily data; a delayed older result cannot
    overwrite a newer usable result from the same source.
    """
    if status not in ("success", "partial", "failed"):
        raise ValueError("采集状态无效。")
    stamp = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    # SQLite and the existing collection history interpret naive timestamps as UTC.
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
    stamp = stamp.astimezone(timezone.utc).isoformat()
    values = values or {}
    row = (run_id, shop_id, stamp, status, *(values.get(key) for key in METRIC_COLUMNS),
           error, int(bool(is_simulated)))
    placeholders = ",".join("?" for _ in RESULT_COLUMNS)
    audit_updates = ",".join(f"{key}=excluded.{key}" for key in RESULT_COLUMNS if key not in ("run_id", "shop_id"))
    existing, incoming = _usable("shop_snapshots"), _usable("excluded")
    metric_updates = ",".join(
        f"{key}=CASE WHEN {existing} AND excluded.is_simulated=shop_snapshots.is_simulated "
        f"THEN COALESCE(excluded.{key},shop_snapshots.{key}) ELSE excluded.{key} END"
        for key in METRIC_COLUMNS)
    newer = "julianday(excluded.collected_at) >= julianday(shop_snapshots.collected_at)"
    with _atomic(db):
        db.execute(f"""INSERT INTO collection_results ({COLUMNS_SQL}) VALUES ({placeholders})
            ON CONFLICT(run_id,shop_id) DO UPDATE SET {audit_updates}""", row)
        db.execute(f"""INSERT INTO shop_snapshots ({COLUMNS_SQL}) VALUES ({placeholders})
            ON CONFLICT(shop_id,{DAY_SQL}) DO UPDATE SET
                run_id=excluded.run_id,collected_at=excluded.collected_at,status=excluded.status,
                {metric_updates},error=excluded.error,is_simulated=excluded.is_simulated
            WHERE ({incoming} AND (
                NOT {existing} OR excluded.is_simulated < shop_snapshots.is_simulated OR
                (excluded.is_simulated=shop_snapshots.is_simulated AND {newer})
            )) OR (NOT {incoming} AND NOT {existing} AND {newer})""", row)


def collection_result(db, run_id, shop_id):
    """Prefer the per-run audit; support databases awaiting their first migration."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_results'").fetchone():
        row = db.execute("SELECT * FROM collection_results WHERE run_id=? AND shop_id=?", (run_id, shop_id)).fetchone()
        if row is not None:
            return row
    return db.execute("SELECT * FROM shop_snapshots WHERE run_id=? AND shop_id=?", (run_id, shop_id)).fetchone()
