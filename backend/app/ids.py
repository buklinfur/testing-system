"""Bounded integers for every HTTP input of the public app (D-088).

The ids and most counters of the schema are int4: a larger value in a path, a query or a body would reach PostgreSQL
and fail there (sqlalchemy DataError -> 500). Every integer the public app takes is therefore one of these types,
which answer 422 `invalid_request` before any handler runs. The constraints live in `pydantic.Field` inside
`Annotated`, so the same type works in a path, a query and a body model.

Write them with a plain default - `item_id: Id`, `cursor: Optional[Id] = None`, `limit: Limit = 50` - never with
`= Path()` or `= Query(...)`: FastAPI 0.161 then drops the bounds of the annotation, from the validation and from the
OpenAPI alike (`limit: Limit = Query(50)` accepted 0). `tests/test_int_bounds_units.py` checks every integer of
every route in the OpenAPI of the app, and `tests/pg/test_int_bounds_pg.py` sends 2**31 to each.
"""
from __future__ import annotations

import re
from typing import Annotated, Optional

from pydantic import Field, StrictInt

DB_ID_MAX = 2_147_483_647  # int4

Id = Annotated[int, Field(ge=1, le=DB_ID_MAX)]  # a row id: users, groups, items, submissions, ...
StrictId = Annotated[StrictInt, Field(ge=1, le=DB_ID_MAX)]  # the same where the body takes only JSON integers
VersionNo = Annotated[int, Field(ge=1, le=DB_ID_MAX)]  # the number of a version (problem, material): int4 as well
After = Annotated[int, Field(ge=0, le=DB_ID_MAX)]  # "rows after this id" of a thread read oldest first; 0 = all
Count = Annotated[int, Field(ge=0, le=DB_ID_MAX)]  # a count given back to the server (expect: N)
Limit = Annotated[int, Field(ge=1, le=DB_ID_MAX)]  # a page size; the endpoint cuts it down to its own maximum


_DIGITS = re.compile(r"[0-9]{1,10}")


def parse_id(text: str) -> Optional[int]:
    """A number written inside a text - `#12` of a group or a material, `sum@3` of a version - or None when it is not
    one an int4 column can hold: ASCII digits only (str.isdigit() also takes "١٢"), 1..DB_ID_MAX. The caller answers
    "not found" for None, as for a number nothing has (D-088)."""
    if not _DIGITS.fullmatch(text or ""):
        return None
    value = int(text)
    return value if 1 <= value <= DB_ID_MAX else None
