-- Local SQLite schema only; no shop data, reviews, credentials or demo records.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS operators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );

CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug_key TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT
            , internal_shop_id TEXT, operator_id INTEGER REFERENCES operators(id));

CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            total_shops INTEGER NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 0,
            partial_count INTEGER NOT NULL DEFAULT 0,
            failed_count INTEGER NOT NULL DEFAULT 0
        );

CREATE TABLE IF NOT EXISTS finance_demo (shop_id INTEGER, day TEXT, sales_usd REAL, net_profit_usd REAL, marketing_usd REAL, is_simulated INTEGER DEFAULT 1, PRIMARY KEY(shop_id,day));

CREATE TABLE IF NOT EXISTS review_sync_runs (
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
    );

CREATE TABLE IF NOT EXISTS reviews (
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
    );

CREATE TABLE IF NOT EXISTS shop_snapshots (
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
            error TEXT, review_count_display TEXT, is_simulated INTEGER NOT NULL DEFAULT 0,
            UNIQUE(run_id, shop_id)
        );

CREATE INDEX IF NOT EXISTS idx_reviews_bad ON reviews(shop_id,stars,review_date);

CREATE INDEX IF NOT EXISTS idx_reviews_shop_date ON reviews(shop_id,review_date DESC,review_id DESC);

CREATE INDEX IF NOT EXISTS idx_snapshots_shop_time ON shop_snapshots(shop_id, collected_at);

-- Latest shop metrics per Beijing calendar day; per-run attempts remain separate.
CREATE UNIQUE INDEX IF NOT EXISTS idx_snapshots_shop_day
    ON shop_snapshots(shop_id,date(datetime(collected_at,'+8 hours')));

CREATE TABLE IF NOT EXISTS collection_results (
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
);
