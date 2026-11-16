"""Store public Etsy reviews from the shop's Most recent review pages."""

import hashlib
import itertools
import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from access_guard import AccessRestrictedError, ensure_page_accessible


IDS = itertools.count(10000)
EXTRACT_JS = r"""JSON.stringify((() => {
  const section = document.querySelector('#reviews');
  const items = [...(section?.querySelectorAll('li[data-region="review"]') || [])].map(e => {
    const attribution = e.querySelector('.shop2-review-attribution');
    const response = e.querySelector('[data-region="response"]');
    const responseAttribution = response?.querySelector('p.fs-mask');
    const photos = [...e.querySelectorAll('img')]
      .filter(img => (img.currentSrc || img.src || '').includes('/iap/') || /added a photo/i.test(img.alt || ''))
      .map(img => img.currentSrc || img.src);
    return {
      id: e.getAttribute('data-review-region'),
      buyer: attribution?.querySelector('a')?.textContent?.trim() || null,
      date: attribution?.innerText?.match(/\s+on\s+(.+)$/i)?.[1]?.trim() || null,
      stars: Number(e.querySelector('input[name="initial-rating"]')?.value || 0),
      content: e.querySelector('.review-item p.prose')?.innerText?.trim() || '',
      photos: [...new Set(photos)],
      response_date: responseAttribution?.innerText?.match(/\s+responded on\s+(.+)$/i)?.[1]?.trim() || null,
      response_content: response?.querySelector('.review-text')?.innerText?.trim() || null,
      listing_url: e.querySelector('[data-region="listing"] a[href*="/listing/"]')?.href || null
    };
  });
  const next = [...(section?.querySelectorAll('a[data-page]') || [])]
    .find(a => /Next page/i.test(a.textContent || ''));
  return {
    page_text: (document.body?.innerText || '').slice(0, 4096),
    sort: section?.querySelector('#sort-reviews-menu .wt-menu__trigger__label')?.innerText?.trim() || '',
    items,
    next_page: next?.getAttribute('data-page') || null
  };
})())"""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(raw):
    if not raw:
        return None
    raw = re.sub(r"\bSept\b", "Sep", raw, flags=re.I)
    for fmt in ("%d %b, %Y", "%d %B, %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def prepare_review_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS reviews (
        shop_id INTEGER NOT NULL REFERENCES shops(id),
        review_id TEXT NOT NULL,
        buyer_name TEXT,
        review_date TEXT,
        review_date_raw TEXT,
        stars INTEGER NOT NULL CHECK(stars BETWEEN 1 AND 5),
        review_text TEXT NOT NULL DEFAULT '',
        photo_urls TEXT NOT NULL DEFAULT '[]',
        photo_files TEXT NOT NULL DEFAULT '[]',
        response_date TEXT,
        response_date_raw TEXT,
        response_text TEXT,
        listing_url TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY(shop_id, review_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_reviews_shop_date ON reviews(shop_id,review_date DESC,review_id DESC)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_reviews_bad ON reviews(shop_id,stars,review_date)")
    db.execute("""CREATE TABLE IF NOT EXISTS review_sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        collection_run_id INTEGER REFERENCES collection_runs(id),
        shop_id INTEGER NOT NULL REFERENCES shops(id),
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        pages_read INTEGER NOT NULL DEFAULT 0,
        reviews_added INTEGER NOT NULL DEFAULT 0,
        stopped_on_duplicate INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )""")


def evaluate(command, ws, expression):
    result = command(ws, next(IDS), "Runtime.evaluate", {"expression": expression, "returnByValue": True})
    value = result.get("result", {}).get("value")
    if value is None:
        raise RuntimeError("浏览器未返回评论数据")
    return value


def read_state(command, ws):
    state = json.loads(evaluate(command, ws, EXTRACT_JS))
    ensure_page_accessible(state.get("page_text", ""))
    return state


def choose_most_recent(command, ws):
    current = read_state(command, ws)
    if "Most recent" in current["sort"]:
        return current
    expression = """(() => {
      const menu = document.querySelector('#sort-reviews-menu');
      const trigger = menu?.querySelector('[data-wt-menu-trigger]');
      const newest = menu?.querySelector('[data-sort-option="Recency"]');
      if (!trigger || !newest) return 'missing';
      trigger.click(); newest.click(); return 'clicked';
    })()"""
    if evaluate(command, ws, expression) != "clicked":
        raise RuntimeError("找不到 Most recent 排序选项")
    before = [item["id"] for item in current["items"]]
    stable_recent = 0
    for _ in range(15):
        time.sleep(1)
        state = read_state(command, ws)
        after = [item["id"] for item in state["items"]]
        if "Most recent" in state["sort"] and after:
            if after != before:
                return state
            stable_recent += 1
            if stable_recent >= 4:
                return state
    raise RuntimeError("评论切换到 Most recent 后未完成加载")


def next_page(command, ws, previous_ids):
    # Check the current page before issuing another network-producing click.
    read_state(command, ws)
    expression = """(() => {
      const link = [...document.querySelectorAll('#reviews a[data-page]')]
        .find(a => /Next page/i.test(a.textContent || ''));
      if (!link) return 'missing'; link.click(); return 'clicked';
    })()"""
    if evaluate(command, ws, expression) != "clicked":
        raise RuntimeError("下一页按钮消失")
    for _ in range(18):
        time.sleep(1)
        state = read_state(command, ws)
        ids = [item["id"] for item in state["items"]]
        if ids and ids != previous_ids and "Most recent" in state["sort"]:
            return state
    raise RuntimeError("下一页评论未完成加载")


def save_photo(url, directory):
    if urlsplit(url).hostname != "i.etsystatic.com":
        return None
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:32]
    existing = next(directory.glob(digest + ".*"), None)
    if existing:
        return existing.name
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            if urlsplit(response.geturl()).hostname != "i.etsystatic.com":
                return None
            content_type = response.headers.get_content_type()
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type)
            if suffix is None:
                return None
            data = response.read(2_000_001)
            if len(data) > 2_000_000:
                return None
        target = directory / (digest + suffix)
        temporary = directory / (digest + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        return target.name
    except Exception:
        return None


def store_review(db, shop_id, item, image_directory):
    review_id = item.get("id")
    stars = item.get("stars")
    if not review_id or not isinstance(stars, (int, float)) or not 1 <= stars <= 5:
        raise ValueError("评论缺少稳定 ID 或星数")
    photo_urls = item.get("photos") or []
    photo_files = [name for url in photo_urls if (name := save_photo(url, image_directory))]
    stamp = timestamp()
    db.execute("""INSERT INTO reviews (
        shop_id,review_id,buyer_name,review_date,review_date_raw,stars,review_text,
        photo_urls,photo_files,response_date,response_date_raw,response_text,
        listing_url,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (shop_id, review_id, item.get("buyer"), parse_date(item.get("date")), item.get("date"),
         int(stars), item.get("content") or "", json.dumps(photo_urls), json.dumps(photo_files),
         parse_date(item.get("response_date")), item.get("response_date"),
         item.get("response_content"), item.get("listing_url"), stamp, stamp))


def update_existing(db, shop_id, item):
    db.execute("""UPDATE reviews SET
        response_date=COALESCE(?,response_date),
        response_date_raw=COALESCE(?,response_date_raw),
        response_text=COALESCE(?,response_text),
        last_seen_at=? WHERE shop_id=? AND review_id=?""",
        (parse_date(item.get("response_date")), item.get("response_date"),
         item.get("response_content"), timestamp(), shop_id, item["id"]))


def sync_shop_reviews(command, db_path, data_dir, ws, shop_id, run_id, known_review_count=None):
    """Read 50 newest reviews on first run; later stop after a page contains an existing ID."""
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    first_sync = db.execute("""SELECT 1 FROM review_sync_runs
        WHERE shop_id=? AND status='completed' LIMIT 1""", (shop_id,)).fetchone() is None
    existing_total = db.execute("SELECT count(*) FROM reviews WHERE shop_id=?", (shop_id,)).fetchone()[0]
    remaining = max(0, 50 - existing_total)
    sync_id = db.execute("""INSERT INTO review_sync_runs(collection_run_id,shop_id,started_at,status)
        VALUES(?,?,?,'running')""", (run_id, shop_id, timestamp())).lastrowid
    db.commit()
    pages = added = 0
    error_code = None
    duplicate = False
    status = "completed"
    error = None
    image_directory = Path(data_dir) / "review_images"
    try:
        if known_review_count == 0:
            state = None
        else:
            state = choose_most_recent(command, ws)
        for _ in range(100):
            if state is None:
                break
            if "Most recent" not in state["sort"]:
                raise RuntimeError("评论排序不是 Most recent")
            items = state["items"]
            if not items:
                raise RuntimeError("当前评论页没有评论卡片")
            ids = [item["id"] for item in items]
            if any(not review_id for review_id in ids) or len(set(ids)) != len(ids):
                raise RuntimeError("评论页缺少稳定的评价 ID")
            pages += 1
            existing = {row["review_id"] for row in db.execute(
                "SELECT review_id FROM reviews WHERE shop_id=? AND review_id IN (%s)" % ",".join("?" * len(ids)),
                [shop_id, *ids])}
            duplicate = bool(existing) and not first_sync
            for item in items:
                if item["id"] in existing:
                    update_existing(db, shop_id, item)
                elif not first_sync or added < remaining:
                    store_review(db, shop_id, item, image_directory)
                    added += 1
                # Image downloads for the next item must not hold this write lock.
                db.commit()
            db.commit()
            if duplicate or (first_sync and added >= remaining) or not state["next_page"]:
                break
            state = next_page(command, ws, ids)
        else:
            status, error = "partial", "达到 100 页安全上限，可能仍有未采集评价"
    except AccessRestrictedError as exc:
        status, error = "blocked", str(exc)[:500]
        error_code = exc.code
    except Exception as exc:
        status, error = "failed", str(exc)[:500]
    finally:
        db.execute("""UPDATE review_sync_runs SET finished_at=?,status=?,pages_read=?,
            reviews_added=?,stopped_on_duplicate=?,error=? WHERE id=?""",
            (timestamp(), status, pages, added, int(duplicate), error, sync_id))
        db.commit()
        db.close()
    return {"status": status, "pages": pages, "added": added,
            "duplicate_stop": duplicate, "error": error, "error_code": error_code}
