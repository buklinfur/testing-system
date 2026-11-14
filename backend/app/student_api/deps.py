"""What every student endpoint depends on: a fully logged-in student, the one table "refusal code -> HTTP status",
the blob store.

Access: no live session -> 401 not_authenticated; an admin between the password and the TOTP code -> 401
mfa_required; an admin -> 403 student_required. Unsafe methods need X-CSRF-Token as in /api/auth; the Origin of
unsafe requests is checked earlier, by the public app (docs/STUDENT_API.md §6).
"""
from __future__ import annotations

from fastapi import Depends, Request, Response
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth_api import Current, _session, get_db
from ..blobstore import BlobStore
from ..models import Role
from ..student.common import StudentError
from ..submissions.intake import CODES as INTAKE_CODES


def student_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Current:
    cur = _session(request, response, db, allow_pending=False)
    if cur.user.role != Role.student:
        raise ApiError(403, "student_required", "this is the student API")
    return cur


# Every refusal code of the student services (app/student/*: StudentError, the refusals of access.can_enter,
# can_start and can_submit, deadline_rules, standings_open_for, and the intake) -> HTTP status:
# 404 what the student does not see (one answer for "not yours" and "not there"), 403 seen but closed to them,
# 409 the state or the time forbids it now, 422 the input is never valid, 413 too large, 429 wait (Retry-After).
# tests/test_student_api_units.py keeps it complete; tests/pg/test_student_api_pg.py reaches every code by a request.
STUDENT_STATUS = {
    # 404
    "contest_not_found": 404, "problem_not_found": 404, "submission_not_found": 404, "file_not_found": 404,
    "group_not_found": 404,
    # 403
    "no_access": 403, "standings_closed": 403, "wrong_code": 403, "student_required": 403,
    # 409: the state or the time
    "not_participant": 409, "press_start": 409, "not_started_yet": 409, "statement_hidden": 409,
    "not_available_yet": 409, "contest_over": 409, "deadline_passed": 409, "submission_limit": 409,
    "already_joined": 409, "already_started": 409, "not_personal": 409, "not_published": 409,
    "admins_do_not_join": 409, "problem_hidden": 409,
    # 422: the input (the intake's own statuses: source_too_large is 413)
    **INTAKE_CODES, "label_needs_contest": 422, "invalid_request": 422,
    # 429: wait
    "rate_limited": 429, "cooldown": 429,
}
# codes no request of a student can reach (the test of reachability skips them, each with its reason)
UNREACHABLE = {
    "admins_do_not_join": "an admin is refused student_required before any service runs",
    "problem_hidden": "a hidden problem is problem_not_found (listed_problem) before can_submit",
    "not_available_yet": "a problem before available_from is problem_not_found (listed_problem) before can_submit, "
                         "at the same `now`",
}


def refused(exc: StudentError) -> ApiError:
    """The ApiError of a refusal; a wait (rate_limited, cooldown) carries Retry-After and retry_after_s."""
    status = STUDENT_STATUS[exc.code]
    if exc.retry_after_s is not None:
        wait = max(1, int(exc.retry_after_s))
        return ApiError(status, exc.code, str(exc), headers={"Retry-After": str(wait)}, retry_after_s=wait)
    return ApiError(status, exc.code, str(exc) if str(exc) != exc.code else None)


def blobs(request: Request) -> BlobStore:
    return BlobStore(request.app.state.settings.blob_dir)
