from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo
import os


def zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def configured_timezone() -> str:
    return os.getenv("TZ", "Asia/Seoul")


def now(name: str | None = None) -> datetime:
    return datetime.now(zone(name or configured_timezone()))


def today(name: str | None = None) -> date:
    return now(name).date()


def local_date(instant: datetime, name: str | None = None) -> date:
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(zone(name or configured_timezone())).date()
