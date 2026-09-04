from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


SEOUL = ZoneInfo("Asia/Seoul")


def parse_date_text(value: str | None) -> date | None:
    if not value:
        return None
    value = value.strip()
    match = re.search(r"(20\d{2})\s*[./년-]\s*(\d{1,2})\s*[./월-]\s*(\d{1,2})", value)
    if not match:
        match = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})", value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_datetime_text(value: str | None, *, default_tz: ZoneInfo = SEOUL) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    parsed_date = parse_date_text(raw)
    if parsed_date and not re.search(r"\d{1,2}:\d{2}", raw):
        return datetime.combine(parsed_date, datetime.min.time(), tzinfo=default_tz)
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        match = re.search(r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\s+(\d{1,2}):(\d{2})", raw)
        if not match:
            return None
        parsed = datetime(
            int(match.group(1)), int(match.group(2)), int(match.group(3)), int(match.group(4)), int(match.group(5))
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def parse_deadline(value: str | None) -> datetime | None:
    if not value:
        return None
    if re.search(r"상시|채용시|수시", value):
        return None
    dates = re.findall(r"20\d{2}\s*[./년-]\s*\d{1,2}\s*[./월-]\s*\d{1,2}", value)
    if not dates:
        dates = re.findall(r"20\d{2}[./-]\d{1,2}[./-]\d{1,2}", value)
    if not dates:
        return None
    return parse_datetime_text(dates[-1])


def days_until(deadline: date | datetime | None, today: date | None = None) -> int | None:
    if deadline is None:
        return None
    due = deadline.date() if isinstance(deadline, datetime) else deadline
    return (due - (today or datetime.now(SEOUL).date())).days


def within_days(left: date | datetime | None, right: date | datetime | None, days: int) -> bool:
    if left is None or right is None:
        return False
    ldate = left.date() if isinstance(left, datetime) else left
    rdate = right.date() if isinstance(right, datetime) else right
    return abs((ldate - rdate).days) <= days
