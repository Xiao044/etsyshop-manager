"""Convert monthly cumulative snapshots into daily financial values."""

from datetime import date, timedelta


def valid_finance_rows(rows, last_day=None, complete=False):
    """Discard unknown dates and empty records, never synthesize amounts."""
    fields = ("sales_usd", "net_profit_usd", "marketing_usd")
    result = []
    for row in rows:
        row = dict(row)
        try:
            day = date.fromisoformat(row["day"]).isoformat()
        except (TypeError, ValueError, KeyError):
            continue
        if last_day is not None and day > last_day:
            continue
        present = [row.get(field) is not None for field in fields]
        if (complete and not all(present)) or not any(present):
            continue
        result.append({**row, "day": day})
    return sorted(result, key=lambda row: row["day"])


def daily_finance(rows):
    result = []
    previous = None
    fields = ("sales_usd", "net_profit_usd", "marketing_usd")
    for row in valid_finance_rows(rows):
        day = date.fromisoformat(row["day"])
        item = {"day": row["day"], **{field: None for field in fields},
                "marketing_ratio": None}
        if row["sales_usd"] is not None and row["sales_usd"] > 0 and row["marketing_usd"] is not None:
            item["marketing_ratio"] = round(row["marketing_usd"] / row["sales_usd"] * 100, 4)
        if day.day == 1:
            baseline = dict.fromkeys(fields, 0)
        elif previous and date.fromisoformat(previous["day"]) == day - timedelta(days=1):
            baseline = previous
        else:
            baseline = None
        if baseline is not None:
            for field in fields:
                if row[field] is not None and baseline[field] is not None:
                    item[field] = round(row[field] - baseline[field], 2)
        result.append(item)
        previous = row
    return result
