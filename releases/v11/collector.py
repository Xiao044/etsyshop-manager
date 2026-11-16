"""Collect public Etsy shop counters with a visible Chromium session."""

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import websocket
from access_guard import AccessRestrictedError, ensure_page_accessible
from review_sync import prepare_review_tables, sync_shop_reviews


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" if os.name == "nt" else Path("/var/lib/etsy-tools")
DATA_DIR = Path(os.environ.get("ETSY_TOOLS_DATA_DIR", str(DEFAULT_DATA_DIR))).resolve()
DB_PATH = DATA_DIR / "shops.sqlite3"
PROFILE = os.environ.get(
    "ETSY_BROWSER_PROFILE",
    str(DATA_DIR / "browser-profile") if os.name == "nt" else "/root/snap/chromium/common/etsy-collector-v03",
)
DEBUG_PORT = int(os.environ.get("ETSY_BROWSER_DEBUG_PORT", "9238"))

sys.path.insert(0, str(PROJECT_ROOT))
from etsy_runtime.locks import FileLock
from etsy_runtime.schema import ensure_item_counts_schema
from etsy_runtime.snapshots import backup_before_daily_migration, ensure_daily_snapshots_schema, save_snapshot


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def database():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def prepare_database():
    backup_before_daily_migration(DB_PATH)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            total_shops INTEGER NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS shop_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
            UNIQUE(run_id, shop_id)
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_shop_time ON shop_snapshots(shop_id, collected_at)")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(shop_snapshots)")}
        if "review_count_display" not in columns:
            db.execute("ALTER TABLE shop_snapshots ADD COLUMN review_count_display TEXT")
        ensure_item_counts_schema(db)
        ensure_daily_snapshots_schema(db)
        prepare_review_tables(db)


def command(ws, request_id, method, params=None):
    ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
    while True:
        message = json.loads(ws.recv())
        if message.get("id") == request_id:
            if "error" in message:
                raise RuntimeError(str(message["error"]))
            return message.get("result", {})


def page_text(ws, request_id):
    expression = "JSON.stringify({title:document.title,url:location.href,text:document.body?.innerText||''})"
    result = command(ws, request_id, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return json.loads(result.get("result", {}).get("value", "{}"))


def parse(page, shop_name):
    ensure_page_accessible(page.get("text", ""))
    if shop_name.lower() not in page.get("title", "").lower():
        raise ValueError("页面标题与店铺不符或被访问限制")
    text = page.get("text", "")
    start = text.lower().find(shop_name.lower())
    if start < 0:
        raise ValueError("页面未显示店铺名称")
    header = text[start:start + 750]
    metric = lambda pattern: re.search(pattern, header, re.I)
    sales = metric(r"(?<![\d,.])(\d[\d,]*)\s+Sales\b")
    admirers = metric(r"(?<![\d,.])(\d[\d,]*)\s+Admirers\b")
    # Section counts can appear below the short shop header. Match complete
    # sidebar labels/lines so product titles such as "On sale 50%" are ignored.
    sidebar = text[start:]
    section_count = lambda label: re.search(
        rf"(?im)^[ \t]*{label}\s+(\d[\d,]*)[ \t]*\r?$", sidebar)
    items = section_count("All")
    on_sale = section_count(r"On[ \t]+sale")
    rating = re.search(re.escape(shop_name) + r".{0,120}?\b([0-5](?:\.\d)?)\s+\(([\d,.]+[kKmM]?)\)", header, re.I | re.S)
    review_display = rating.group(2) if rating else None
    reviews_section = re.search(
        r"(?m)^Reviews\s+Sort by:[^\n]*\s+([0-5](?:\.\d)?)\s*[^\d\n]{0,16}\s*\(([\d,]+)\)",
        text,
    )
    exact_reviews = int(reviews_section.group(2).replace(",", "")) if reviews_section else None
    values = {
        "sales": int(sales.group(1).replace(",", "")) if sales else None,
        "admirers": int(admirers.group(1).replace(",", "")) if admirers else None,
        "item_count": int(items.group(1).replace(",", "")) if items else None,
        "on_sale_count": int(on_sale.group(1).replace(",", "")) if on_sale else None,
        "rating": float(rating.group(1)) if rating else None,
        "review_count": exact_reviews,
        "review_count_display": review_display,
    }
    found = sum(values[k] is not None for k in ("sales", "admirers", "item_count", "on_sale_count", "rating", "review_count"))
    if found == 0:
        raise ValueError("页面已打开，但未找到指标")
    return values, ("success" if found == 6 else "partial")


def endpoint():
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=2) as response:
        tabs = json.load(response)
    return next(tab["webSocketDebuggerUrl"] for tab in tabs if tab.get("type") == "page")


def browser_executable():
    configured = os.environ.get("ETSY_BROWSER_PATH")
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"浏览器不存在: {candidate}")
        return str(candidate.resolve())
    if os.name == "nt":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        ]
    else:
        candidates = [Path("/snap/bin/chromium")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ("chrome", "msedge", "chromium", "chromium-browser", "google-chrome"):
        executable = shutil.which(name)
        if executable:
            return executable
    raise FileNotFoundError("未找到 Chrome/Edge/Chromium；请设置 ETSY_BROWSER_PATH。")


def browser_arguments(executable, windows=None):
    windows = os.name == "nt" if windows is None else windows
    args = [executable]
    if not windows:
        args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    args += [
        f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=http://localhost", f"--user-data-dir={Path(PROFILE).resolve()}",
        "--no-first-run", "--no-default-browser-check", "--lang=en-US",
        "--window-size=1440,900",
    ]
    proxy = os.environ.get("ETSY_BROWSER_PROXY")
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")
    return args


def close_browser(process, ws=None):
    if ws:
        try:
            ws.send(json.dumps({"id": 999999, "method": "Browser.close"}))
        except Exception:
            pass
        finally:
            ws.close()
    if process:
        try:
            process.wait(timeout=7)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=7)


def launch_browser():
    # Refuse to attach to or close another developer's debugging browser.
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", DEBUG_PORT))
        except OSError as exc:
            raise RuntimeError(f"调试端口 {DEBUG_PORT} 已占用；关闭之前的采集浏览器或更换端口。") from exc
    executable = browser_executable()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Path(PROFILE).mkdir(parents=True, exist_ok=True)
    log = open(DATA_DIR / "collector-chromium.log", "a", encoding="utf-8")
    try:
        process = subprocess.Popen(browser_arguments(executable), stdout=log, stderr=log)
    except Exception:
        log.close()
        raise
    for _ in range(25):
        try:
            ws = websocket.create_connection(
                endpoint(), timeout=20, origin="http://localhost",
                http_no_proxy=["localhost", "127.0.0.1"],
            )
            command(ws, 1, "Network.setCacheDisabled", {"cacheDisabled": True})
            print(f"browser={executable} profile={PROFILE} debug_port={DEBUG_PORT}", flush=True)
            return process, ws, log
        except Exception:
            if process.poll() is not None:
                break
            time.sleep(1)
    close_browser(process)
    log.close()
    raise RuntimeError("图形浏览器未能启动；请检查 collector-chromium.log")


def collect(ws, shop, request_id):
    command(ws, request_id, "Page.navigate", {"url": shop["url"]})
    deadline = time.monotonic() + 35
    last_error = "页面加载超时"
    partial = None
    while time.monotonic() < deadline:
        time.sleep(3)
        page = page_text(ws, request_id + 1)
        try:
            values, status = parse(page, shop["name"])
            if status == "success":
                return values, status
            partial = values
            if time.monotonic() > deadline - 15:
                return partial, "partial"
        except ValueError as exc:
            last_error = str(exc)
    if partial:
        return partial, "partial"
    raise ValueError(last_error)


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop-id", type=int, action="append", help="仅采集指定店铺，可重复指定")
    parser.add_argument("--skip-reviews", action="store_true", help="仅验证公开指标，不同步评价")
    parser.add_argument("--keep-browser-open", action="store_true", help="本地调试：结束后保留独立浏览器")
    parser.add_argument("--task-id", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prepare_database()
    with database() as db:
        query = "SELECT id,name,url FROM shops WHERE deleted_at IS NULL"
        params = []
        if args.shop_id:
            query += " AND id IN (%s)" % ",".join("?" * len(args.shop_id))
            params = args.shop_id
        shops = db.execute(query + " ORDER BY id", params).fetchall()
        if args.shop_id and {row["id"] for row in shops} != set(args.shop_id):
            raise ValueError("指定店铺不存在或已删除；请先运行本地 db-info。")
        run_id = db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES(?,?,?)",
                            (now(), "running", len(shops))).lastrowid
        if args.task_id is not None:
            if len(shops) != 1:
                raise ValueError("队列任务只能采集一家店铺。")
            changed = db.execute("UPDATE collection_tasks SET run_id=?,phase='metrics' WHERE id=? AND shop_id=? AND status='running'", (run_id, args.task_id, shops[0]["id"])).rowcount
            if changed != 1:
                raise ValueError("队列任务状态无效。")
    print(f"run={run_id} shops={len(shops)}", flush=True)
    if not shops:
        with database() as db:
            db.execute("UPDATE collection_runs SET finished_at=?,status='completed' WHERE id=?", (now(), run_id))
        return
    process = ws = log = None
    review_failures = 0
    blocked_reason = None
    try:
        process, ws, log = launch_browser()
        for index, shop in enumerate(shops):
            values = {k: None for k in ("sales", "admirers", "item_count", "on_sale_count", "rating", "review_count", "review_count_display")}
            status, error = "failed", None
            try:
                values, status = collect(ws, shop, 100 + index * 10)
                if status == "partial":
                    error = "部分指标未显示"
            except AccessRestrictedError as exc:
                blocked_reason = error = str(exc)[:500]
            except Exception as exc:
                error = str(exc)[:500]
            stamp = now()
            with database() as db:
                save_snapshot(db, run_id, shop["id"], stamp, status, values, error)
            print(json.dumps({"shop": shop["name"], "at": stamp, "status": status, **values, "error": error}, ensure_ascii=False), flush=True)
            if blocked_reason:
                print(json.dumps({"run": run_id, "status": "blocked", "error_code": AccessRestrictedError.code, "error": blocked_reason}, ensure_ascii=False), flush=True)
                break
            if args.skip_reviews:
                continue
            if status == "failed":
                review_failures += 1
                print(json.dumps({"shop": shop["name"], "review_sync": {"status": "skipped", "error": "店铺页面采集失败"}}, ensure_ascii=False), flush=True)
                continue
            if args.task_id is not None:
                with database() as db:
                    db.execute("UPDATE collection_tasks SET phase='reviews' WHERE id=? AND status='running'", (args.task_id,))
            review_result = sync_shop_reviews(command, DB_PATH, DATA_DIR, ws, shop["id"], run_id, values["review_count"])
            if review_result["status"] != "completed":
                review_failures += 1
            print(json.dumps({"shop": shop["name"], "review_sync": review_result}, ensure_ascii=False), flush=True)
            if review_result.get("error_code") == AccessRestrictedError.code:
                blocked_reason = review_result["error"]
                break
    except Exception as exc:
        print(f"browser_error={exc}", file=sys.stderr, flush=True)
    finally:
        if args.keep_browser_open:
            if ws:
                ws.close()
            print("独立采集浏览器已保留；再次采集前请手动关闭该窗口。", flush=True)
        else:
            close_browser(process, ws)
        if log:
            log.close()
        with database() as db:
            # Daily rows may belong to an earlier successful run when this
            # refresh failed. Count this batch's own results, not daily data.
            completed_shops = {row["shop_id"] for row in db.execute(
                "SELECT shop_id FROM collection_results WHERE run_id=?", (run_id,))}
            missing_error = ("未访问：本批遇到 Etsy 访问限制，后续店铺已停止采集。" if blocked_reason
                             else "浏览器启动失败或采集意外中断")
            for shop in shops:
                if shop["id"] not in completed_shops:
                    save_snapshot(db, run_id, shop["id"], now(), "failed", error=missing_error)
            completed = db.execute("SELECT status,count(*) AS n FROM collection_results WHERE run_id=? GROUP BY status",
                                   (run_id,)).fetchall()
            counts = {row["status"]: row["n"] for row in completed}
            db.execute("""UPDATE collection_runs SET finished_at=?,status=?,
                success_count=?,partial_count=?,failed_count=? WHERE id=?""",
                (now(), "blocked" if blocked_reason else "completed", counts.get("success", 0),
                 counts.get("partial", 0), counts.get("failed", 0), run_id))
    if blocked_reason or counts.get("failed", 0) or review_failures:
        raise SystemExit(1)


def main(argv=None):
    with FileLock(DATA_DIR / "collector.lock"):
        return _main(argv)


if __name__ == "__main__":
    main()
