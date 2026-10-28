"""Start and end dates for a period phrase, given today. Used by tests only, never at runtime.

It writes the vocabulary in config/thelook.yaml as code, so the expected dates in
eval/questions.yaml can be checked against the same rules the prompt states.

    resolve_period("last quarter", date(2026, 10, 28))   # (2026-07-01, 2026-09-30)
"""
import calendar
import re
from datetime import date, timedelta

ALL_TIME_START = date(2019, 1, 1)
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
QUARTER_START = {1: 1, 2: 4, 3: 7, 4: 10}


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_month(year, month, by):
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def _quarter(year, q):
    start = QUARTER_START[q]
    return date(year, start, 1), _month_end(year, start + 2)


def resolve_period(phrase, today):
    """(start, end) for the phrase, or ValueError if the vocabulary has no rule for it."""
    p = " ".join(phrase.lower().split())

    if p == "all time":
        return ALL_TIME_START, today
    if m := re.fullmatch(r"last (\d+) months", p):
        n = int(m[1])
        y, mo = _shift_month(today.year, today.month, -n)
        py, pm = _shift_month(today.year, today.month, -1)
        return date(y, mo, 1), _month_end(py, pm)

    # The current period through today.
    if p in ("this year", "year to date"):
        return date(today.year, 1, 1), today
    if p == "this month":
        return date(today.year, today.month, 1), today
    if p == "this quarter":
        q = (today.month - 1) // 3 + 1
        return _quarter(today.year, q)[0], today

    # The most recent complete period.
    if p == "last year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if p == "last month":
        y, mo = _shift_month(today.year, today.month, -1)
        return date(y, mo, 1), _month_end(y, mo)
    if p == "last quarter":
        q = (today.month - 1) // 3 + 1
        return _quarter(today.year, q - 1) if q > 1 else _quarter(today.year - 1, 4)

    # Halves, with a year or "this year"/"last year".
    if m := re.fullmatch(r"(first|second) half of (this year|last year|\d{4})", p):
        year = {"this year": today.year, "last year": today.year - 1}.get(m[2]) or int(m[2])
        return (date(year, 1, 1), date(year, 6, 30)) if m[1] == "first" else (date(year, 7, 1), date(year, 12, 31))

    # A year on its own, or a quarter or month with a year.
    if re.fullmatch(r"\d{4}", p):
        return date(int(p), 1, 1), date(int(p), 12, 31)
    if m := re.fullmatch(r"q([1-4]) (\d{4})", p):
        return _quarter(int(m[2]), int(m[1]))
    if m := re.fullmatch(r"([a-z]+) (\d{4})", p):
        if m[1] in MONTHS:
            mo, y = MONTHS[m[1]], int(m[2])
            return date(y, mo, 1), _month_end(y, mo)

    # A named month or quarter with no year: the most recent one that has already started.
    if p in MONTHS:
        mo = MONTHS[p]
        y = today.year if mo <= today.month else today.year - 1
        return date(y, mo, 1), _month_end(y, mo)
    if m := re.fullmatch(r"q([1-4])", p):
        q = int(m[1])
        y = today.year if QUARTER_START[q] <= today.month else today.year - 1
        return _quarter(y, q)

    raise ValueError(f"no rule for the period phrase {phrase!r}")
