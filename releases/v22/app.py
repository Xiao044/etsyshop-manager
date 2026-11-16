"""Private Etsy shop list manager for the Singapore server."""

import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from finance_metrics import daily_finance
from counter_metrics import daily_changes, chart_axis


DATA_DIR = Path(os.environ.get("ETSY_TOOLS_DATA_DIR", "/var/lib/etsy-tools"))
DB_PATH = DATA_DIR / "shops.sqlite3"
SECRET_PATH = DATA_DIR / "session_secret"


def load_secret():
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text().strip()
    return secrets.token_hex(32)


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
app.secret_key = load_secret()
app.config.update(
    MAX_CONTENT_LENGTH=8192,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
)


def connect_db():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            """CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug_key TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            )"""
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(shops)")}
        if "internal_shop_id" not in columns:
            db.execute("ALTER TABLE shops ADD COLUMN internal_shop_id TEXT")
        if "operator_id" not in columns:
            db.execute("ALTER TABLE shops ADD COLUMN operator_id INTEGER REFERENCES operators(id)")


def normalize_metadata(shop_id, operator):
    shop_id = shop_id.strip()
    operator = " ".join(operator.split())
    if len(shop_id) > 80:
        raise ValueError("店铺ID不能超过 80 个字符。")
    if len(operator) > 50:
        raise ValueError("运营名称不能超过 50 个字符。")
    return shop_id or None, operator or None


def operator_id_for(db, name, now):
    if name is None:
        return None
    name_key = name.casefold()
    db.execute(
        "INSERT INTO operators (name, name_key, created_at) VALUES (?, ?, ?) ON CONFLICT(name_key) DO NOTHING",
        (name, name_key, now),
    )
    return db.execute("SELECT id FROM operators WHERE name_key = ?", (name_key,)).fetchone()["id"]


@app.template_filter("beijing_date")
def beijing_date(value):
    return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")


def normalize_shop_url(value):
    value = value.strip()
    if not value:
        raise ValueError("请输入 Etsy 店铺链接。")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ValueError("店铺链接格式不正确。") from exc
    if parts.scheme not in {"http", "https"} or host not in {"etsy.com", "www.etsy.com"}:
        raise ValueError("请填写 etsy.com 的店铺主页链接。")
    if parts.username or parts.password or port:
        raise ValueError("店铺链接格式不正确。")
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) == 3 and re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", segments[0], re.I):
        segments = segments[1:]
    if len(segments) != 2 or segments[0].lower() != "shop" or not re.fullmatch(r"[A-Za-z0-9]{3,64}", segments[1]):
        raise ValueError("请填写店铺主页链接，例如 https://www.etsy.com/shop/FrameLessShop。")
    name = segments[1]
    return name, name.casefold(), f"https://www.etsy.com/shop/{name}"


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf():
    expected = session.get("csrf_token", "")
    supplied = request.form.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(expected, supplied)


@app.before_request
def local_host_only():
    public_host = os.environ.get("ETSY_TOOLS_PUBLIC_HOST", "")
    public_prefix = os.environ.get("ETSY_TOOLS_PUBLIC_PREFIX", "")
    is_public = bool(public_host and public_prefix and
                     request.host == public_host and request.script_root == public_prefix)
    if request.host not in {"localhost:8765", "127.0.0.1:8765"} and not is_public:
        return "此页面仅供本机安全连接访问。", 400


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https://i.etsystatic.com; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def index():
    with connect_db() as db:
        shops = db.execute(
            """SELECT shops.id, shops.name, shops.url, shops.created_at,
                      shops.internal_shop_id, operators.name AS operator_name
               FROM shops LEFT JOIN operators ON operators.id = shops.operator_id
               WHERE shops.deleted_at IS NULL ORDER BY shops.id DESC"""
        ).fetchall()
        operators = db.execute("SELECT name FROM operators ORDER BY name_key").fetchall()
    return render_template("index.html", shops=shops, operators=operators, csrf_token=csrf_token())


@app.get("/dashboard")
def dashboard():
    metrics = {
        "sales": "Sales", "daily_sales": "日销售额", "admirers": "关注人数", "item_count": "在售商品",
        "rating": "评分", "review_count": "评价数", "bad_review_count": "差评数",
    }
    selected = request.args.get("metric", "sales")
    if selected not in metrics:
        selected = "sales"
    scope = request.args.get("scope", "all")
    if scope not in {"all", "internal", "external"}:
        scope = "all"
    days = 7 if request.args.get("days") == "7" else 30
    today = datetime.now(timezone(timedelta(hours=8))).date()
    first_day = (today - timedelta(days=days-1)).isoformat()
    is_daily = selected in {"sales", "admirers", "item_count", "review_count", "bad_review_count"}
    with connect_db() as db:
        operators = db.execute("""SELECT DISTINCT o.id,o.name FROM operators o
            JOIN shops s ON s.operator_id=o.id WHERE s.deleted_at IS NULL
            ORDER BY o.name_key""").fetchall()
        valid_operators = {str(row["id"]) for row in operators}
        operator_id = request.args.get("operator", "")
        if operator_id not in valid_operators:
            operator_id = ""
        shops = db.execute("""SELECT s.id,s.name,s.internal_shop_id,s.operator_id,o.name AS operator_name
            FROM shops s LEFT JOIN operators o ON o.id=s.operator_id
            WHERE s.deleted_at IS NULL ORDER BY s.id""").fetchall()
        rows = db.execute("""SELECT shop_id,date(datetime(collected_at,'+8 hours')) AS day,
            collected_at,is_simulated,sales,admirers,item_count,rating,review_count
            FROM shop_snapshots
            WHERE date(datetime(collected_at,'+8 hours')) <= ?
            ORDER BY shop_id,day,is_simulated DESC,collected_at""", (today.isoformat(),)).fetchall()
        bad_rows = db.execute("""SELECT shop_id,COALESCE(review_date,date(datetime(first_seen_at,'+8 hours'))) AS day
            FROM reviews WHERE stars < 5""").fetchall()
        review_totals = {row["shop_id"]: row["n"] for row in db.execute(
            "SELECT shop_id,count(*) AS n FROM reviews GROUP BY shop_id")}
    finance_by_shop = {}
    if selected == "daily_sales":
        with connect_db() as db:
            finance_rows = db.execute("SELECT * FROM finance_demo ORDER BY shop_id,day").fetchall()
        for row in finance_rows:
            finance_by_shop.setdefault(row["shop_id"], []).append(dict(row))
    bad_by_shop = {}
    for row in bad_rows:
        bad_by_shop.setdefault(row["shop_id"], []).append(row["day"])
    by_shop = {}
    for row in rows:
        by_shop.setdefault(row["shop_id"], {})[row["day"]] = row
    cards = []
    for shop in shops:
        is_internal = bool(shop["internal_shop_id"] or shop["operator_name"])
        if operator_id and str(shop["operator_id"]) != operator_id:
            continue
        if scope == "internal" and not is_internal:
            continue
        if scope == "external" and is_internal:
            continue
        if selected == "daily_sales":
            start = (today - timedelta(days=days-1)).isoformat()
            finance_rows = daily_finance(finance_by_shop.get(shop["id"], []))
            cumulative = finance_by_shop.get(shop["id"], [])
            latest = next((r for r in reversed(cumulative) if r["day"] <= today.isoformat()), None)
            latest_ratio = (latest["marketing_usd"] / latest["sales_usd"] * 100
                            if latest and latest["sales_usd"] > 0 else None)
            cards.append({"shop": shop, "internal": is_internal,
                          "latest": latest, "latest_ratio": latest_ratio,
                          "rows": [row for row in finance_rows if start <= row["day"] <= today.isoformat()]})
            continue
        daily = list(by_shop.get(shop["id"], {}).values())
        if selected == "bad_review_count":
            dates = [(today - timedelta(days=i)).isoformat() for i in range(days,-1,-1)]
            values = [(day, sum(review_day <= day for review_day in bad_by_shop.get(shop["id"], [])), 0)
                      for day in dates]
        else:
            values = [(row["day"], row[selected], row["is_simulated"]) for row in daily if row[selected] is not None]
        current = values[-1][1] if values else None
        current_day = values[-1][0] if values else None
        values = (daily_changes(values, first_day, today.isoformat()) if is_daily else
                  [v for v in values if v[0] >= first_day])
        if values:
            lo, hi, axis_ticks = chart_axis([v[1] for v in values], is_daily)
            span = hi - lo or 1
            width = max(len(values) - 1, 1)
            chart_days = [
                {"x": round(80 + i * 636 / width, 1),
                 "y": round(169 - (value - lo) * 130 / span, 1),
                 "day": day, "value": value, "simulated": bool(simulated)}
                for i, (day, value, simulated) in enumerate(values)
            ]
            for i, point in enumerate(chart_days):
                left = 80 if i == 0 else (chart_days[i - 1]["x"] + point["x"]) / 2
                right = 740 if i == len(chart_days) - 1 else (point["x"] + chart_days[i + 1]["x"]) / 2
                point["zone_x"] = round(left, 1)
                point["zone_width"] = round(round(right, 1) - point["zone_x"], 1)
            commands = []
            for i, point in enumerate(chart_days):
                connected = i > 0 and (datetime.fromisoformat(point["day"]) - datetime.fromisoformat(chart_days[i-1]["day"])).days == 1
                commands.append(f"{'L' if connected else 'M'}{point['x']},{point['y']}")
            points = " ".join(commands)
            change = sum(v[1] for v in values) if is_daily else values[-1][1] - values[0][1]
            elapsed_days = (datetime.fromisoformat(values[-1][0]) - datetime.fromisoformat(values[0][0])).days
            average = round(change / len(values), 2) if is_daily else (round(change / elapsed_days, 2) if elapsed_days else None)
        else:
            points, change, average, chart_days = "", None, None, []
            axis_ticks = []
        cards.append({"shop": shop, "internal": is_internal,
                      "points": points, "chart_days": chart_days,
                      "current": current, "change": change, "average": average,
                      "current_day": current_day, "axis_ticks": axis_ticks,
                      "start": values[0][0] if values else None,
                      "end": values[-1][0] if values else None,
                      "days": len(values), "simulated": sum(v[2] for v in values)})
        cards[-1]["review_total"] = review_totals.get(shop["id"], 0)
    return render_template("dashboard.html", cards=cards, metrics=metrics,
                           selected=selected, scope=scope, days=days, is_daily=is_daily,
                           operators=operators, operator_id=operator_id,
                           today=today.isoformat())


@app.get("/shops/<int:shop_id>/reviews")
def shop_reviews(shop_id):
    bad_only = request.args.get("bad") == "1"
    with connect_db() as db:
        shop = db.execute("SELECT id,name FROM shops WHERE id=? AND deleted_at IS NULL", (shop_id,)).fetchone()
        if shop is None:
            abort(404)
        sql = """SELECT buyer_name,review_date,review_date_raw,stars,review_text,
                 photo_urls,photo_files,response_date,response_date_raw,response_text
                 FROM reviews WHERE shop_id=?"""
        if bad_only:
            sql += " AND stars < 5"
        sql += " ORDER BY COALESCE(review_date,first_seen_at) DESC, review_id DESC"
        rows = db.execute(sql, (shop_id,)).fetchall()
    reviews = []
    for row in rows:
        item = dict(row)
        files = json.loads(item.pop("photo_files") or "[]")
        urls = json.loads(item.pop("photo_urls") or "[]")
        item["photos"] = [url_for("review_image", filename=name) for name in files]
        if not files:
            item["photos"] = [url for url in urls if url.startswith("https://i.etsystatic.com/")]
        reviews.append(item)
    return render_template("reviews_fragment.html", reviews=reviews, shop=shop, bad_only=bad_only)


@app.get("/review-images/<path:filename>")
def review_image(filename):
    return send_from_directory(DATA_DIR / "review_images", filename)


@app.post("/shops/add")
def add_shop():
    if not valid_csrf():
        return "页面已过期，请刷新后重试。", 403
    try:
        name, slug_key, url = normalize_shop_url(request.form.get("shop_url", ""))
        shop_id, operator = normalize_metadata(request.form.get("shop_id", ""), request.form.get("operator", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"), code=303)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_db() as db:
        existing = db.execute("SELECT id, deleted_at FROM shops WHERE slug_key = ?", (slug_key,)).fetchone()
        if existing and existing["deleted_at"] is None:
            flash("这家店铺已经在采集名单中。", "info")
        elif existing:
            operator_id = operator_id_for(db, operator, now)
            db.execute(
                "UPDATE shops SET name = ?, url = ?, created_at = ?, deleted_at = NULL, internal_shop_id = ?, operator_id = ? WHERE id = ?",
                (name, url, now, shop_id, operator_id, existing["id"]),
            )
            flash("店铺已重新加入采集名单。", "success")
        else:
            operator_id = operator_id_for(db, operator, now)
            db.execute(
                "INSERT INTO shops (name, slug_key, url, created_at, internal_shop_id, operator_id) VALUES (?, ?, ?, ?, ?, ?)",
                (name, slug_key, url, now, shop_id, operator_id),
            )
            flash("店铺已加入采集名单。", "success")
    return redirect(url_for("index"), code=303)


@app.post("/shops/<int:shop_id>/update")
def update_shop(shop_id):
    if not valid_csrf():
        return "页面已过期，请刷新后重试。", 403
    try:
        internal_shop_id, operator = normalize_metadata(request.form.get("shop_id", ""), request.form.get("operator", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"), code=303)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_db() as db:
        exists = db.execute("SELECT id FROM shops WHERE id = ? AND deleted_at IS NULL", (shop_id,)).fetchone()
        if exists:
            operator_id = operator_id_for(db, operator, now)
            db.execute(
                "UPDATE shops SET internal_shop_id = ?, operator_id = ? WHERE id = ?",
                (internal_shop_id, operator_id, shop_id),
            )
            flash("店铺信息已保存。", "success")
        else:
            flash("这家店铺已不在采集名单中。", "info")
    return redirect(url_for("index"), code=303)


@app.post("/shops/<int:shop_id>/delete")
def delete_shop(shop_id):
    if not valid_csrf():
        return "页面已过期，请刷新后重试。", 403
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE shops SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (now, shop_id)
        )
    if cursor.rowcount:
        flash("店铺已从采集名单删除；历史记录仍保留。", "success")
    else:
        flash("这家店铺已不在采集名单中。", "info")
    return redirect(url_for("index"), code=303)


@app.get("/finance")
def finance():
    return redirect(url_for("dashboard", metric="daily_sales"))


@app.get("/healthz")
def healthz():
    with connect_db() as db:
        db.execute("SELECT 1").fetchone()
    return {"status": "ok"}


init_db()
