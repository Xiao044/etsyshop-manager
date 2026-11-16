"""Convert monthly cumulative snapshots into daily financial values."""

from datetime import date, timedelta


def daily_finance(rows):
    result = []
    previous = None
    fields = ("sales_usd", "net_profit_usd", "marketing_usd")
    for row in sorted(rows, key=lambda item: item["day"]):
        day = date.fromisoformat(row["day"])
        item = {"day": row["day"], **{field: None for field in fields},
                "marketing_ratio": None}
        if row["sales_usd"] > 0:
            item["marketing_ratio"] = round(row["marketing_usd"] / row["sales_usd"] * 100, 4)
        if day.day == 1:
            baseline = dict.fromkeys(fields, 0)
        elif previous and date.fromisoformat(previous["day"]) == day - timedelta(days=1):
            baseline = previous
        else:
            baseline = None
        if baseline is not None:
            for field in fields:
                item[field] = round(row[field] - baseline[field], 2)
        result.append(item)
        previous = row
    return result
