"""Time on the way out and in.

* The API always answers in UTC, ISO 8601 with a `Z` suffix: `iso_utc`.
* JUDGE_TIMEZONE (default UTC) is used only for what people read in judgectl tables (`display`) and for times
  without an offset in contest.yaml (`parse_local`, session 4a.2).
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimezoneError(ValueError):
    pass


def zone(name: Optional[str]) -> tzinfo:
    """JUDGE_TIMEZONE -> tzinfo ("UTC" when empty); TimezoneError for an unknown name."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimezoneError(f"JUDGE_TIMEZONE={name!r} is not a known time zone (e.g. Asia/Novosibirsk)") from exc


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """'2026-11-26T15:07:59.223443Z'. Naive values are taken as UTC (the database returns aware ones)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def display(value: Optional[datetime], tz: tzinfo) -> str:
    """For people: '2026-11-26 22:07' in JUDGE_TIMEZONE."""
    if value is None:
        return "-"
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


YEARS = (1970, 2999)  # what a contest, a deadline or an audit filter may name


def parse_local(text: str, tz: tzinfo) -> datetime:
    """ISO 8601 ('2026-11-10 09:00', '2026-11-10T09:00:00+07:00', '...Z'); without an offset it is local time of tz.
    ValueError for anything else, including a year outside YEARS: 0001-01-01 or 9999-12-31 with an offset cannot even
    be moved to UTC (OverflowError), and a window or a deadline near them would overflow later (D-088)."""
    value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    value = value.replace(tzinfo=tz) if value.tzinfo is None else value
    try:
        year = value.astimezone(timezone.utc).year
    except OverflowError as exc:
        raise ValueError(f"time out of range: {text!r}") from exc
    if not YEARS[0] <= year <= YEARS[1]:
        raise ValueError(f"time out of range (years {YEARS[0]}..{YEARS[1]}): {text!r}")
    return value
