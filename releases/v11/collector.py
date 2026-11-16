"""Collect public Etsy shop counters with a visible Chromium session."""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websocket
from review_sync import prepare_review_tables, sync_shop_reviews


DATA_DIR = Path(os.environ.get("ETSY_TOOLS_DATA_DIR", "/var/lib/etsy-tools"))
DB_PATH = DATA_DIR / "shops.sqlite3"
PROFILE = "/root/snap/chromium/common/etsy-collector-v03"
DEBUG_PORT = 9238


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def prepare_database():
    with database() as db:
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
    items = metric(r"\bAll\s+(\d[\d,]*)\b")
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
        "rating": float(rating.group(1)) if rating else None,
        "review_count": exact_reviews,
        "review_count_display": review_display,
    }
    found = sum(values[k] is not None for k in ("sales", "admirers", "item_count", "rating", "review_count"))
    if found == 0:
        raise ValueError("页面已打开，但未找到指标")
    return values, ("success" if found == 5 else "partial")


def endpoint():
    with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=2) as response:
        tabs = json.load(response)
    return next(tab["webSocketDebuggerUrl"] for tab in tabs if tab.get("type") == "page")


def launch_browser():
    log = open(DATA_DIR / "collector-chromium.log", "a")
    process = subprocess.Popen([
        "/snap/bin/chromium", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
        f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=http://localhost", f"--user-data-dir={PROFILE}",
        "--window-size=1440,900", "about:blank",
    ], stdout=log, stderr=log)
    for _ in range(25):
        try:
            ws = websocket.create_connection(endpoint(), timeout=20, origin="http://localhost")
            command(ws, 1, "Network.setCacheDisabled", {"cacheDisabled": True})
            return process, ws, log
        except Exception:
            if process.poll() is not None:
                break
            time.sleep(1)
    process.terminate()
    log.close()
    raise RuntimeError("图形浏览器未能启动")


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


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prepare_database()
    with database() as db:
        shops = db.execute("SELECT id,name,url FROM shops WHERE deleted_at IS NULL ORDER BY id").fetchall()
        run_id = db.execute("INSERT INTO collection_runs(started_at,status,total_shops) VALUES(?,?,?)",
                            (now(), "running", len(shops))).lastrowid
    print(f"run={run_id} shops={len(shops)}", flush=True)
    if not shops:
        with database() as db:
            db.execute("UPDATE collection_runs SET finished_at=?,status='completed' WHERE id=?", (now(), run_id))
        return
    process = ws = log = None
    review_failures = 0
    try:
        process, ws, log = launch_browser()
        for index, shop in enumerate(shops):
            values = {k: None for k in ("sales", "admirers", "item_count", "rating", "review_count", "review_count_display")}
            status, error = "failed", None
            try:
                values, status = collect(ws, shop, 100 + index * 10)
                if status == "partial":
                    error = "部分指标未显示"
            except Exception as exc:
                error = str(exc)[:500]
            stamp = now()
            with database() as db:
                db.execute("""INSERT INTO shop_snapshots
                    (run_id,shop_id,collected_at,status,sales,admirers,item_count,rating,review_count,review_count_display,error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, shop["id"], stamp, status, values["sales"], values["admirers"],
                     values["item_count"], values["rating"], values["review_count"], values["review_count_display"], error))
            print(json.dumps({"shop": shop["name"], "at": stamp, "status": status, **values, "error": error}, ensure_ascii=False), flush=True)
            review_result = sync_shop_reviews(command, DB_PATH, DATA_DIR, ws, shop["id"], run_id, values["review_count"])
            if review_result["status"] != "completed":
                review_failures += 1
            print(json.dumps({"shop": shop["name"], "review_sync": review_result}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(f"browser_error={exc}", file=sys.stderr, flush=True)
    finally:
        if ws:
            ws.close()
        if process:
            process.terminate()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
        if log:
            log.close()
        with database() as db:
            completed = db.execute("SELECT status,count(*) AS n FROM shop_snapshots WHERE run_id=? GROUP BY status", (run_id,)).fetchall()
            counts = {row["status"]: row["n"] for row in completed}
            missing = len(shops) - sum(counts.values())
            if missing:
                for shop in shops:
                    db.execute("""INSERT OR IGNORE INTO shop_snapshots(run_id,shop_id,collected_at,status,error)
                        VALUES(?,?,?,'failed','浏览器启动失败或采集意外中断')""", (run_id, shop["id"], now()))
                counts["failed"] = counts.get("failed", 0) + missing
            db.execute("""UPDATE collection_runs SET finished_at=?,status='completed',
                success_count=?,partial_count=?,failed_count=? WHERE id=?""",
                (now(), counts.get("success", 0), counts.get("partial", 0), counts.get("failed", 0), run_id))
    if counts.get("failed", 0) or review_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
