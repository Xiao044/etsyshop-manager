"""Explicit, backed-up cleanup of simulated local records only."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .locks import FileLock

SIMULATED_TABLES = ("shop_snapshots", "collection_results", "finance_demo")


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _fingerprint(db, table, filtered=False):
    query = "SELECT * FROM " + _quoted(table)
    if filtered:
        query += " WHERE is_simulated IS NOT 1"
    columns = [r[1] for r in db.execute("PRAGMA table_info(" + _quoted(table) + ")")]
    query += " ORDER BY " + ",".join(_quoted(name) for name in columns)
    digest = hashlib.sha256()
    count = 0
    for row in db.execute(query):
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=lambda value: {"bytes": value.hex()}).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return {"count": count, "sha256": digest.hexdigest()}


def simulated_counts(db):
    tables = set(_tables(db))
    counts = {}
    for name in SIMULATED_TABLES:
        if name not in tables:
            counts[name] = 0
            continue
        if "is_simulated" not in {r[1] for r in db.execute("PRAGMA table_info(" + _quoted(name) + ")")}:
            raise ValueError(f"{name} 没有 is_simulated 标记，拒绝根据名称或数值猜测删除。")
        counts[name] = db.execute("SELECT count(*) FROM " + _quoted(name) + " WHERE is_simulated=1").fetchone()[0]
    return counts


def _require_idle(db):
    tables = set(_tables(db))
    if "collection_tasks" in tables and db.execute("SELECT 1 FROM collection_tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
        raise ValueError("存在排队或执行中的采集任务，暂不清理；请先等待任务结束。")
    if "collection_runs" in tables and db.execute("SELECT 1 FROM collection_runs WHERE status='running' LIMIT 1").fetchone():
        raise ValueError("存在尚未结束的采集批次，暂不清理。")


def clean_simulated(database, apply=False):
    path = Path(database).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        counts = simulated_counts(db)
    if not apply:
        return {"dry_run": True, "database": str(path), "would_delete": counts, "total": sum(counts.values())}
    # Maintenance and collection are mutually exclusive across processes. Stop the
    # persistent Worker first rather than changing its state behind its back.
    with FileLock(path.parent / "collection-worker.lock"), FileLock(path.parent / "collector.lock"):
        with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            _require_idle(db)
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("数据库检查未通过，未删除任何记录。")
            directory = path.parent / "backups"
            directory.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = directory / f"before-remove-simulated-{stamp}.sqlite3"
            with closing(sqlite3.connect(backup)) as destination:
                db.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("备份校验失败，未删除任何记录。")
            try:
                db.execute("BEGIN IMMEDIATE")
                _require_idle(db)
                tables = _tables(db)
                counts = simulated_counts(db)
                before = {table: _fingerprint(db, table, table in SIMULATED_TABLES) for table in tables}
                for name, count in counts.items():
                    if count:
                        db.execute("DELETE FROM " + _quoted(name) + " WHERE is_simulated=1")
                after = {table: _fingerprint(db, table, table in SIMULATED_TABLES) for table in tables}
                if before != after:
                    raise ValueError("真实记录校验不一致，已回滚删除。")
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("删除后数据库检查异常，已回滚删除。")
                db.commit()
            except BaseException:
                db.rollback()
                raise
    report = {"dry_run": False, "database": str(path), "backup": str(backup), "deleted": counts,
              "total": sum(counts.values()), "real_records_unchanged": True,
              "preserved_records": {name: value["count"] for name, value in after.items()},
              "integrity": "ok", "foreign_keys": "ok"}
    report_path = path.parent / f"remove-simulated-{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    return report
