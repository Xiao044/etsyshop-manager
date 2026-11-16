"""Read-only, single-shop analytics using the dashboard's metric definitions."""

from collections import Counter
from datetime import date, timedelta

from counter_metrics import chart_axis, daily_changes
from finance_metrics import daily_finance, valid_finance_rows


METRICS = (
    {"key": "sales", "label": "Sales", "table_label": "Sales 净增", "format": "number", "daily": True,
     "today_label": "当日净增", "total_label": "累计销量", "stat_label": "区间净增"},
    {"key": "daily_sales", "label": "日销售额", "table_label": "日销售额", "format": "money", "daily": True,
     "today_label": "当日销售额 · USD", "total_label": "月累计销售额", "stat_label": "区间销售额 · 模拟"},
    {"key": "admirers", "label": "关注人数", "table_label": "关注净增", "format": "number", "daily": True,
     "today_label": "当日净增", "total_label": "累计关注", "stat_label": "区间净增"},
    {"key": "item_count", "label": "在售商品", "table_label": "商品净增", "format": "number", "daily": True,
     "today_label": "当日净增", "total_label": "在售总数", "stat_label": "区间净增"},
    {"key": "rating", "label": "评分", "table_label": "评分", "format": "rating", "daily": False,
     "today_label": "当日评分", "total_label": "最新评分", "stat_label": "区间均分"},
    {"key": "review_count", "label": "评价数", "table_label": "评价净增", "format": "number", "daily": True,
     "today_label": "当日净增", "total_label": "累计评价", "stat_label": "区间净增"},
    {"key": "bad_review_count", "label": "差评数", "table_label": "新增差评", "format": "number", "daily": True,
     "today_label": "当日新增", "total_label": "已收录差评总数", "stat_label": "区间新增"},
)


def format_value(value, kind="number"):
    if value is None:
        return "—"
    if kind == "money":
        return f"${value:,.2f}"
    if kind == "rating":
        return f"{value:.2f}"
    return f"{value:,}" if isinstance(value, int) else f"{value:,.2f}"


def _chart(values, first_day, last_day, daily, kind, axis=None):
    """Position points on calendar dates; never connect across a missing day."""
    if not values:
        return {"points": "", "chart_days": [], "axis_ticks": []}
    low, high, ticks = axis or chart_axis([value for _, value, _ in values], daily)
    span = (last_day - first_day).days or 1
    step = 636 / span
    points, commands = [], []
    previous_day = None
    for day, value, simulated in values:
        parsed_day = date.fromisoformat(day)
        x = round(80 + (parsed_day - first_day).days * step, 1)
        y = round(169 - (value - low) * 130 / (high - low), 1)
        connected = previous_day is not None and parsed_day - previous_day == timedelta(days=1)
        commands.append(f"{'L' if connected else 'M'}{x},{y}")
        left, right = max(70, x - step / 2), min(726, x + step / 2)
        points.append({"day": day, "value": value, "formatted": format_value(value, kind),
                       "simulated": bool(simulated), "x": x, "y": y,
                       "zone_x": round(left, 1), "zone_width": round(right - left, 1)})
        previous_day = parsed_day
    return {"points": " ".join(commands), "chart_days": points, "axis_ticks": ticks}


def _summary(totals, values, last_day, daily):
    current = totals[-1] if totals else None
    today = next((value for value in values if value[0] == last_day), None)
    return {"today_value": today[1] if today else None,
            "today_simulated": bool(today and today[2]),
            "total_value": current[1] if current else None,
            "total_day": current[0] if current else None,
            "total_simulated": bool(current and current[2]),
            "current": current[1] if current else None,
            "current_day": current[0] if current else None,
            "current_simulated": bool(current and current[2]), "count": len(values),
            "simulated_days": sum(bool(value[2]) for value in values),
            "stat_value": (round(sum(value[1] for value in values), 2) if daily else
                           sum(value[1] for value in values) / len(values)) if values else None,
            "average": sum(value[1] for value in values) / len(values) if values else None}


def build_item_counts(snapshots, first_day, last_day):
    """Two independently collected counters in one card, with a shared scale.

    Callers provide one preferred snapshot per Beijing day. Missing On sale
    counts are unknown, not zero; neither series can be used as the other's
    baseline. Deltas never bridge missing calendar days.
    """
    first, last = date.fromisoformat(first_day), date.fromisoformat(last_day)
    snapshots = sorted((dict(row) for row in snapshots if row["day"] <= last_day), key=lambda row: row["day"])
    series = []
    for key, short_label, label, style in (("item_count", "All", "所有品类", "all"),
                                           ("on_sale_count", "On sale", "促销商品", "sale")):
        totals = [(row["day"], row[key], bool(row["is_simulated"]))
                  for row in snapshots if row.get(key) is not None]
        values = daily_changes(totals, first_day, last_day)
        series.append({"key": key, "short_label": short_label, "label": label, "style": style,
                       **_summary(totals, values, last_day, True), "values": values})
    axis_values = [value for item in series for _, value, _ in item["values"]]
    axis = chart_axis(axis_values, True) if axis_values else None
    by_day = {}
    for item in series:
        item.update(_chart(item.pop("values"), first, last, True, "number", axis=axis))
        for point in item["chart_days"]:
            by_day.setdefault(point["day"], {})[item["key"]] = point
    points = []
    for day, daily in sorted(by_day.items()):
        # One hit area per day: hovering either line reports both values.
        anchor = next(iter(daily.values()))
        parts = [{"key": item["key"], "label": item["short_label"],
                  "value": daily.get(item["key"], {}).get("value"),
                  "formatted": daily.get(item["key"], {}).get("formatted", "—"),
                  "simulated": daily.get(item["key"], {}).get("simulated", False)} for item in series]
        points.append({**anchor, "parts": parts,
                       "formatted": " · ".join(f'{part["label"]}：{part["formatted"]}' for part in parts),
                       "kind": " · ".join(f'{part["label"]}：模拟数据' for part in parts if part["simulated"])})
    return {"series": series, "chart_days": points, "axis_ticks": axis[2] if axis else [],
            "by_day": {point["day"]: point["parts"] for point in points}}


def build_shop_data(snapshots, finance_rows, bad_review_days, first_day, last_day, review_available=False):
    """Build all seven charts and a complete calendar table without writing data.

    A real snapshot wins over a simulated one on the same Beijing calendar day.
    Counter deltas require both adjacent days, including the day before the range.
    Financial data deliberately remains marked as demo, regardless of its values.
    """
    first, last = date.fromisoformat(first_day), date.fromisoformat(last_day)
    calendar = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    by_day = {}
    for row in sorted((dict(row) for row in snapshots if row["day"] and row["day"] <= last_day),
                      key=lambda row: (row["day"], not bool(row["is_simulated"]),
                                       row["collected_at"], row.get("id", 0))):
        by_day[row["day"]] = row
    snapshots = [by_day[day] for day in sorted(by_day)]
    bad_counts = Counter(day for day in bad_review_days if day and day <= last_day)
    bad_total = sum(bad_counts.values())
    review_available = review_available or bool(bad_counts)

    # finance_demo contains monthly cumulative USD amounts, not real accounting.
    complete_finance = valid_finance_rows(finance_rows, last_day, complete=True)
    finance_daily = daily_finance(complete_finance)
    finance_by_day = {row["day"]: row for row in finance_daily}
    finance_calendar = [finance_by_day.get(day, {"day": day, "sales_usd": None,
                        "net_profit_usd": None, "marketing_usd": None, "marketing_ratio": None}) for day in calendar]

    cards, table_values = [], {}
    for definition in METRICS:
        card = dict(definition)
        key = card["key"]
        if key == "daily_sales":
            totals = [(row["day"], row["sales_usd"], True) for row in finance_daily if row["sales_usd"] is not None]
            values = [value for value in totals if first_day <= value[0] <= last_day]
            card["finance_rows"] = finance_calendar
            if not complete_finance:
                card["stat_label"] = "区间销售额"
            card["has_finance"] = any(row["sales_usd"] is not None or row["marketing_ratio"] is not None
                                      for row in finance_calendar)
        elif key == "bad_review_count":
            totals = [(last_day, bad_total, False)] if review_available else []
            values = [(day, bad_counts[day], False) for day in calendar] if review_available else []
        else:
            totals = [(row["day"], row[key], bool(row["is_simulated"])) for row in snapshots if row[key] is not None]
            values = (daily_changes(totals, first_day, last_day) if card["daily"] else
                      [value for value in totals if first_day <= value[0] <= last_day])
        card.update(_summary(totals, values, last_day, card["daily"]))
        if key == "daily_sales":
            latest = complete_finance[-1] if complete_finance else None
            card.update({"total_value": latest["sales_usd"] if latest else None,
                         "total_day": latest["day"] if latest else None,
                         "total_simulated": bool(latest)})
        elif key == "item_count":
            card["item_counts"] = build_item_counts(snapshots, first_day, last_day)
        if key != "daily_sales":
            card.update(_chart(values, first, last, card["daily"], card["format"]))
        table_values[key] = {day: {"value": value, "simulated": bool(simulated)} for day, value, simulated in values}
        cards.append(card)

    table_rows = []
    for day in reversed(calendar):
        cells, snapshot_sources = [], set()
        for card in cards:
            cell = table_values[card["key"]].get(day, {"value": None, "simulated": False})
            cell = {**cell, "key": card["key"], "formatted": format_value(cell["value"], card["format"])}
            if card["key"] == "item_count":
                cell["parts"] = card["item_counts"]["by_day"].get(day, [
                    {"key": item["key"], "label": item["short_label"], "value": None,
                     "formatted": "—", "simulated": False} for item in card["item_counts"]["series"]])
            cells.append(cell)
            if card["key"] not in {"daily_sales", "bad_review_count"}:
                for part in cell.get("parts", [cell]):
                    if part["value"] is not None:
                        snapshot_sources.add("simulated" if part["simulated"] else "collected")
        table_rows.append({"day": day, "cells": cells, "has_simulated": "simulated" in snapshot_sources,
                           "has_collected": "collected" in snapshot_sources,
                           "has_finance": table_values["daily_sales"].get(day, {}).get("value") is not None})
    visible_snapshots = [row for row in snapshots if first_day <= row["day"] <= last_day]
    return {"metrics": cards, "table_rows": table_rows, "first_day": first_day, "last_day": last_day,
            "latest_snapshot": snapshots[-1] if snapshots else None,
            "snapshot_days": len(visible_snapshots),
            "simulated_days": sum(bool(row["is_simulated"]) for row in visible_snapshots),
            "has_finance_data": bool(complete_finance),
            "has_data": any(card["count"] or card["total_value"] is not None or card.get("has_finance", False) or
                            any(item["total_value"] is not None for item in card.get("item_counts", {}).get("series", []))
                            for card in cards)}
