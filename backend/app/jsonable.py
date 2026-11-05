"""Plain JSON data for answers (the admin API) and judgectl --json: times in UTC with Z, enums as their value,
Decimal scores as exact strings. FastAPI would otherwise write datetimes with the offset of the database connection."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from .timeutil import iso_utc


def plain(value):
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    return value
