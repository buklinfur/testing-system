"""The router of the student API (docs/STUDENT_API.md §6): /api/contests and /api/submissions need a fully logged-in
student (deps.student_session); /api/load any fully logged-in user."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends

from . import contests, load, problems, submissions
from .deps import student_session
from .types import LABEL_PATTERN, SLUG_PATTERN

router = APIRouter(prefix="/api")
student = APIRouter(dependencies=[Depends(student_session)])
student.include_router(contests.router)
student.include_router(problems.router)
student.include_router(submissions.router)
router.include_router(student)
router.include_router(load.router)

# The submission reads its raw body itself (problems.submit: after the cheap refusals, with the limit of the problem)
# and is exempt from the 1 MB JSON limit by this pattern, matched against the whole path (re.fullmatch): a slug and a
# label as contest.schema.json writes them, so a neighbour (a trailing slash, a double slash, another case, another
# method) keeps the JSON limit.
UPLOAD_PATTERNS: tuple = (
    ("POST", re.compile(rf"/api/contests/{SLUG_PATTERN}/problems/{LABEL_PATTERN}/submissions")),
)
# the path prefixes of the student API (tools/gen-openapi.py, the sweeps of the tests)
PREFIXES = ("/api/contests", "/api/submissions", "/api/load")
