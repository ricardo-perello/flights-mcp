"""Choose which departure dates a single search is allowed to hit."""

from __future__ import annotations

from datetime import date, timedelta

MAX_DATES = 7


def enumerate_dates(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end before start")
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
        if len(days) > 366:
            break
    return days


def select_dates(start: date, end: date, *, cap: int = MAX_DATES) -> tuple[list[date], list[date]]:
    """Return (searched, omitted). A window longer than `cap` is sampled, endpoints included."""
    if cap < 1:
        raise ValueError("cap must be at least 1")
    every = enumerate_dates(start, end)
    if len(every) <= cap:
        return every, []
    chosen: list[int] = []
    last = len(every) - 1
    for step in range(cap):
        index = round(step * last / (cap - 1))
        if index not in chosen:
            chosen.append(index)
    fill = 0
    while len(chosen) < cap and fill < len(every):
        if fill not in chosen:
            chosen.append(fill)
        fill += 1
    chosen.sort()
    picked = {index for index in chosen}
    searched = [every[index] for index in chosen]
    omitted = [day for index, day in enumerate(every) if index not in picked]
    return searched, omitted
