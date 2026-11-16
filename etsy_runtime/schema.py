"""Additive snapshot migrations shared by the web app and collector."""


def ensure_item_counts_schema(db):
    """Keep unknown historical On sale counts NULL, never infer them from All."""
    for table in ("shop_snapshots", "collection_results"):
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if columns and "on_sale_count" not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN on_sale_count INTEGER")
