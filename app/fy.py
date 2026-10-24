"""Indian FY helpers. FY Y-(Y+1) = 1 Apr Y to 31 Mar Y+1."""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, datetime
from typing import Iterable


MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def parse_fy(label: str) -> tuple[date, date, str]:
    text = (label or "").strip().upper().replace("FY", "").replace(" ", "")
    text = text.replace("/", "-")
    m = re.search(r"(20\d{2})\s*[-–]\s*(\d{2,4})", text)
    if not m:
        m = re.search(r"(20\d{2})$", text)
        if not m:
            raise ValueError(f"Could not parse financial year from {label!r}")
        start_year = int(m.group(1))
        end_year = start_year + 1
    else:
        start_year = int(m.group(1))
        end_raw = m.group(2)
        end_year = int(end_raw) if len(end_raw) == 4 else 2000 + int(end_raw)
        if end_year < 100:
            end_year += 2000
    start = date(start_year, 4, 1)
    end = date(end_year, 3, 31)
    return start, end, f"{start_year}-{str(end_year)[-2:]}"


def fy_for_date(d: date) -> str:
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    m = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", value)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def overlap_days(a_start: date, a_end: date, b_start: date, b_end: date) -> int:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end < start:
        return 0
    return (end - start).days + 1


def period_coverage(
    period_start: str | None,
    period_end: str | None,
    fy_start: date,
    fy_end: date,
) -> dict:
    start = parse_iso_date(period_start)
    end = parse_iso_date(period_end) or start
    if not start:
        return {"status": "unknown", "fraction": 0.0, "days_in_fy": 0, "days_total": 0}
    if end < start:
        start, end = end, start
    total = (end - start).days + 1
    inside = overlap_days(start, end, fy_start, fy_end)
    fraction = inside / total if total else 0.0
    if inside == 0:
        status = "outside"
    elif inside == total:
        status = "full"
    else:
        status = "partial"
    return {
        "status": status,
        "fraction": round(fraction, 4),
        "days_in_fy": inside,
        "days_total": total,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


def parse_month_label(label: str, default_year: int | None = None) -> tuple[date, date] | None:
    text = (label or "").strip()
    m = re.search(r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*(20\d{2})?", text, re.I)
    if not m:
        return None
    month = MONTHS[m.group(1).lower()]
    year = int(m.group(2)) if m.group(2) else default_year
    if not year:
        return None
    return month_bounds(year, month)


def available_fys(years: Iterable[int] | None = None) -> list[str]:
    years = list(years) if years else list(range(2022, 2028))
    return [f"{y}-{str(y + 1)[-2:]}" for y in years]
