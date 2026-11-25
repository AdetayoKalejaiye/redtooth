"""Relative time phrases -> [since, until) windows in local time."""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Optional

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _at(day: datetime, h: int, m: int = 0) -> datetime:
    return datetime.combine(day.date(), time(h, m), tzinfo=day.tzinfo)


def parse_time(text: str, now: Optional[datetime] = None) -> tuple[Optional[float], Optional[float], Optional[str], str]:
    """Return (since_ts, until_ts, label, text_without_phrase)."""
    now = now or datetime.now().astimezone()
    t = text.lower()
    today0 = _at(now, 0)
    rules: list[tuple[str, callable]] = [
        (r"\bthis morning\b", lambda: (today0, min(now, _at(now, 12)) if now.hour >= 12 else now, "this morning")),
        (r"\bthis afternoon\b", lambda: (_at(now, 12), min(now, _at(now, 18)), "this afternoon")),
        (r"\b(this evening|tonight)\b", lambda: (_at(now, 17), now, "this evening")),
        (r"\blast night\b", lambda: (_at(now - timedelta(days=1), 17), _at(now, 5), "last night")),
        (r"\byesterday morning\b", lambda: (_at(now - timedelta(days=1), 0), _at(now - timedelta(days=1), 12), "yesterday morning")),
        (r"\byesterday afternoon\b", lambda: (_at(now - timedelta(days=1), 12), _at(now - timedelta(days=1), 18), "yesterday afternoon")),
        (r"\byesterday\b", lambda: (today0 - timedelta(days=1), today0, "yesterday")),
        (r"\b(today|earlier today)\b", lambda: (today0, now, "today")),
        (r"\b(just now|a few minutes ago|a minute ago|minutes ago)\b", lambda: (now - timedelta(minutes=30), now, "in the last 30 minutes")),
        (r"\b(an hour ago|an hour back|in the last hour)\b", lambda: (now - timedelta(hours=2), now, "in the last couple of hours")),
        (r"\b(recently|lately)\b", lambda: (now - timedelta(days=3), now, "recently")),
        (r"\bthis week\b", lambda: (today0 - timedelta(days=now.weekday()), now, "this week")),
        (r"\blast week\b", lambda: (today0 - timedelta(days=now.weekday() + 7), today0 - timedelta(days=now.weekday()), "last week")),
        (r"\bthis month\b", lambda: (today0.replace(day=1), now, "this month")),
    ]
    for pat, fn in rules:
        m = re.search(pat, t)
        if m:
            since, until, label = fn()
            return since.timestamp(), until.timestamp(), label, (text[: m.start()] + text[m.end():]).strip()
    m = re.search(r"\b(?:on |last )?(" + "|".join(WEEKDAYS) + r")\b", t)
    if m:
        wd = WEEKDAYS.index(m.group(1))
        delta = (now.weekday() - wd) % 7 or 7
        day = today0 - timedelta(days=delta)
        return day.timestamp(), (day + timedelta(days=1)).timestamp(), f"on {m.group(1).title()}", (text[: m.start()] + text[m.end():]).strip()
    m = re.search(r"\b(\d+) (hours?|days?) ago\b", t)
    if m:
        n = int(m.group(1))
        unit = timedelta(hours=1) if m.group(2).startswith("hour") else timedelta(days=1)
        center = now - n * unit
        return (center - unit).timestamp(), (center + unit).timestamp(), m.group(0), (text[: m.start()] + text[m.end():]).strip()
    return None, None, None, text
