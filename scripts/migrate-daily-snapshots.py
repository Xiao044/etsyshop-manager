"""Back up and migrate a local database to one snapshot per Beijing shop day.

This does not collect data, open a browser, or change the production server.
"""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from etsy_runtime.locks import FileLock, LockBusy
from etsy_runtime.snapshots import backup_before_daily_migration, ensure_daily_snapshots_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data/shops.sqlite3')
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        parser.error('本地开发数据库不存在。')
    with FileLock(database.parent / 'collector.lock'):
        with closing(sqlite3.connect(database, timeout=30)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_tasks'").fetchone():
                if db.execute("SELECT 1 FROM collection_tasks WHERE status IN ('queued','running') LIMIT 1").fetchone():
                    raise ValueError('有排队或运行中的采集任务，请等本地队列结束后再整理。')
            before = db.execute('SELECT count(*) FROM shop_snapshots').fetchone()[0]
            backup = backup_before_daily_migration(database)
            with db:
                db.execute('BEGIN IMMEDIATE')
                ensure_daily_snapshots_schema(db)
                if db.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('数据库外键检查失败，已回滚。')
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('数据库完整性检查失败，已回滚。')
            after = db.execute('SELECT count(*) FROM shop_snapshots').fetchone()[0]
            audit = db.execute('SELECT count(*) FROM collection_results').fetchone()[0]
    print(json.dumps({'database': str(database), 'backup': str(backup) if backup else None,
                      'snapshots_before': before, 'snapshots_after': after,
                      'duplicate_rows_merged': before - after, 'collection_results': audit,
                      'integrity': 'ok', 'foreign_keys': 'ok'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, LockBusy) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
