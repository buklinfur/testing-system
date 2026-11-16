"""Path and query texts of the student API: bounded in length (the services look them up exactly; a malformed one is
simply not found). The patterns of the submission upload path (routes.UPLOAD_PATTERNS) are those of
protocol/schemas/contest.schema.json: a slug and a label that cannot exist are never exempt from the JSON limit."""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

SLUG_PATTERN = r"[a-z0-9][a-z0-9-]{1,62}"  # contest.schema.json "slug"
LABEL_PATTERN = r"[A-Z][A-Z0-9]{0,7}"  # contest.schema.json "label"
ID_PATTERN = r"[1-9][0-9]{0,9}"  # a row id in a path (a larger one than int4 is then 422 by ids.Id)

Slug = Annotated[str, Field(min_length=1, max_length=64)]
Label = Annotated[str, Field(min_length=1, max_length=8)]
