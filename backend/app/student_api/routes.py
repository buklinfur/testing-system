"""The router of the student API (docs/STUDENT_API.md §6): /api/contests, /api/submissions, /api/feed and /api/works
need a fully logged-in student (deps.student_session); /api/load any fully logged-in user."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends

from . import contests, drafts, feed, load, problems, submissions
from .deps import student_session
from .types import ID_PATTERN, LABEL_PATTERN, SLUG_PATTERN

router = APIRouter(prefix="/api")
student = APIRouter(dependencies=[Depends(student_session)])
student.include_router(contests.router)
student.include_router(problems.router)
student.include_router(submissions.router)
student.include_router(feed.router)
student.include_router(drafts.router)
router.include_router(student)
router.include_router(load.router)

# The submission and the upload of a draft file read their raw body themselves (problems.submit, drafts.upload_draft_file:
# after the cheap refusals, with their own limit) and are exempt from the 1 MB JSON limit by these patterns, matched
# against the whole path (re.fullmatch): a slug and a label as contest.schema.json writes them, an item id as a
# positive number without leading zeros, so a neighbour (a trailing slash, a double slash, another case, another
# method, another route of the draft) keeps the JSON limit.
UPLOAD_PATTERNS: tuple = (
    ("POST", re.compile(rf"/api/contests/{SLUG_PATTERN}/problems/{LABEL_PATTERN}/submissions")),
    ("POST", re.compile(rf"/api/feed/{ID_PATTERN}/draft/files")),
)
# the path prefixes of the student API (tools/gen-openapi.py, the sweeps of the tests)
PREFIXES = ("/api/contests", "/api/submissions", "/api/load", "/api/feed", "/api/works")
