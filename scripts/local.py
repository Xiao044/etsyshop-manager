"""Local-only SQLite, Flask and Windows browser development commands."""

import argparse
import hashlib
import importlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from etsy_runtime.queue import Store, QueueError
from etsy_runtime.locks import LockBusy

TABLES = ("operators", "shops", "collection_runs", "shop_snapshots", "reviews", "review_sync_runs", "finance_demo")


@contextmanager
def connect_readonly(path):
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    try:
        yield con
    finally:
        con.close()


def validate_database(path):
    with connect_readonly(path) as con:
        result = con.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise ValueError(f"SQLite 完整性检查失败: {result}")
        missing = set(TABLES) - {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if missing:
            raise ValueError(f"缺少数据表: {', '.join(sorted(missing))}")
        if con.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("数据库存在外键约束错误。")


def initialize_database(data_dir, snapshot=None):
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "shops.sqlite3"
    if snapshot:
        source = Path(snapshot).expanduser().resolve()
        if target.exists():
            raise FileExistsError(f"开发库已存在，拒绝覆盖: {target}")
        if not source.is_file():
            raise FileNotFoundError(source)
        if source == target:
            raise ValueError("快照源文件不能是开发库本身。")
        validate_database(source)
        fd, name = tempfile.mkstemp(prefix="import-", suffix=".sqlite3", dir=data_dir)
        os.close(fd)
        temporary = Path(name)
        try:
            with connect_readonly(source) as src, closing(sqlite3.connect(temporary)) as dst:
                with dst:
                    src.backup(dst)
            validate_database(temporary)
            # A second check also prevents overwriting a database created during import.
            if target.exists():
                raise FileExistsError(f"开发库已存在，拒绝覆盖: {target}")
            temporary.rename(target)
        finally:
            temporary.unlink(missing_ok=True)
        metadata = {
            "source": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "Local development copy. Production is not connected.",
        }
        (data_dir / "snapshot-source.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        with closing(sqlite3.connect(target)) as con:
            with con:
                con.executescript((ROOT / "sql/schema.sql").read_text(encoding="utf-8"))
    validate_database(target)
    secret_path = data_dir / "session_secret"
    if not secret_path.exists():
        with secret_path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_hex(32))
    return target


def database_info(path):
    validate_database(path)
    print(f"database={path}")
    print("integrity=ok foreign_keys=ok")
    with connect_readonly(path) as con:
        for table in TABLES:
            print(f"{table}={con.execute('SELECT count(*) FROM ' + table).fetchone()[0]}")
        print("active shops (public names only):")
        for row in con.execute("SELECT id,name,url FROM shops WHERE deleted_at IS NULL ORDER BY id"):
            print(json.dumps(dict(row), ensure_ascii=False))
        latest = con.execute("SELECT id,started_at,finished_at,status,total_shops,success_count,partial_count,failed_count FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            print("latest_run=" + json.dumps(dict(latest), ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data", help="默认固定使用仓库下 data，不继承生产数据目录")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db", help="创建空开发库，或从已有本地快照导入；绝不覆盖")
    init.add_argument("--snapshot", type=Path)
    commands.add_parser("db-info", help="只读检查 SQLite 完整性和各表数量")
    cleanup = commands.add_parser("clean-simulated", help="预览本地模拟记录清理；--apply 才备份并删除")
    cleanup.add_argument("--apply", action="store_true")
    query = commands.add_parser("sql", help="对本地库执行只读 SQL")
    query.add_argument("--query", required=True)
    web = commands.add_parser("web", help="启动仅监听本机的 v22 开发网站")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--debug", action="store_true", help="只在本机使用开发调试器")
    collect = commands.add_parser("collect", help="使用独立 Chrome/Edge 配置写入本地开发库")
    scope = collect.add_mutually_exclusive_group(required=True)
    scope.add_argument("--shop-id", action="append", type=int)
    scope.add_argument("--all-shops", action="store_true")
    worker = commands.add_parser("worker", help="启动全局串行采集 Worker，保持此进程运行")
    worker.add_argument("--once", action="store_true", help="最多执行一个任务后退出")
    commands.add_parser("queue-status", help="显示持久化队列和 Worker 状态")
    commands.add_parser("resume-queue", help="确认正常访问恢复后解除暂停，不重试旧任务")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    # Override even an inherited production variable: all subcommands are local-only.
    os.environ["ETSY_TOOLS_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("ETSY_BROWSER_PROFILE", str(data_dir / "browser-profile"))
    database = data_dir / "shops.sqlite3"
    if args.command == "init-db":
        initialize_database(data_dir, args.snapshot)
        database_info(database)
        return
    if not database.is_file():
        parser.error("本地开发库不存在，请先运行 init-db。")
    validate_database(database)
    if args.command == "clean-simulated":
        from etsy_runtime.maintenance import clean_simulated
        print(json.dumps(clean_simulated(database, apply=args.apply), ensure_ascii=False, indent=2))
    elif args.command == "db-info":
        database_info(database)
    elif args.command == "sql":
        with connect_readonly(database) as con:
            cursor = con.execute(args.query)
            print(json.dumps([dict(row) for row in cursor], ensure_ascii=False, indent=2))
    elif args.command == "web":
        os.environ["ETSY_TOOLS_LOCAL_PORT"] = str(args.port)
        sys.path.insert(0, str(ROOT / "releases/v22"))
        website = importlib.import_module("app")
        print(f"local database={database}", flush=True)
        website.app.run(host="127.0.0.1", port=args.port, debug=args.debug, use_reloader=False)
    elif args.command in ("collect", "worker", "queue-status", "resume-queue"):
        store = Store(database)
        store.initialize()
        if args.command == "collect":
            ids = args.shop_id or []
            scope = "all" if args.all_shops else ("shop" if len(set(ids)) == 1 else "selection")
            print(json.dumps(store.submit(scope, ids, origin="cli"), ensure_ascii=False, indent=2))
            print("已提交持久化队列。请保持 scripts/local.py worker 运行。")
        elif args.command == "worker":
            from etsy_runtime.worker import run_worker
            run_worker(database, once=args.once)
        elif args.command == "queue-status":
            print(json.dumps(store.state(), ensure_ascii=False, indent=2))
        else:
            store.resume()
            print("已解除暂停。旧任务不会自动重试。")



if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, sqlite3.Error, LockBusy) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
