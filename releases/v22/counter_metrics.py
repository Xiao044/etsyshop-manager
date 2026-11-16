"""Daily net changes from consecutive cumulative snapshots."""

from datetime import date, timedelta
import math


def chart_axis(values, daily=True):
    low, high = min(values), max(values)
    if daily:
        low, high = min(0, low), max(0, high)
    if low == high:
        high = low + 1
    raw = (high - low) / 4
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(n * magnitude for n in (1, 2, 5, 10) if n * magnitude >= raw)
    if daily:
        step = max(1, step)
    bottom = math.floor(low / step) * step
    top = math.ceil(high / step) * step
    decimals = max(0, -math.floor(math.log10(step)))
    ticks = []
    for i in range(round((top - bottom) / step) + 1):
        value = round(bottom + i * step, 10)
        ticks.append({"y": round(169 - (value-bottom)*130/(top-bottom), 1),
                      "label": f"{value:,.{decimals}f}"})
    return bottom, top, ticks


def daily_changes(totals, first_day, last_day):
    previous = None
    changes = []
    for day, value, simulated in sorted(totals):
        if previous and date.fromisoformat(day) - date.fromisoformat(previous[0]) == timedelta(days=1):
            if first_day <= day <= last_day:
                changes.append((day, value - previous[1], bool(simulated or previous[2])))
        previous = (day, value, simulated)
    return changes
