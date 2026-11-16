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
from ..classroom.items import FeedError
from ..models import Role
from ..student.common import StudentError
from ..submissions.intake import CODES as INTAKE_CODES


def student_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Current:
    cur = _session(request, response, db, allow_pending=False)
    if cur.user.role != Role.student:
        raise ApiError(403, "student_required", "this is the student API")
    return cur


# Every refusal code of the student services -> HTTP status: 404 what the student does not see (one answer for "not
# yours" and "not there"), 403 seen but closed to them, 409 the state or the time forbids it now, 422 the input is
# never valid, 413 too large, 429 wait (Retry-After). Two parts, one table: the contests of 4b.1 (app/student/*:
# StudentError, the refusals of access.can_enter, can_start and can_submit, deadline_rules, standings_open_for, and
# the intake) and the class feed of 4b.2 (FeedError of app/student/feed.py, classroom.py and the classroom services
# they call, the Decision and Refusal codes of classroom/access.py and attachment_rules.py). A code of both has one
# status. tests/test_student_api_units.py keeps it complete; tests/pg/test_student_api_pg.py reaches every code of
# CONTEST_STATUS by a request, tests/pg/test_student_classroom_api_pg.py every code of CLASSROOM_STATUS.
CONTEST_STATUS = {
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
# The class feed (D-101). Not here although the classroom rules have them: not_published and not_available_yet of an
# item (feed.seen_item answers item_not_found first; both codes are the contests' and reached there - UNREACHABLE is
# kept by code, not by route), too_many_attachments (a teacher's limit).
CLASSROOM_STATUS = {
    # 404
    "item_not_found": 404, "attachment_not_found": 404, "file_not_found": 404, "draft_file_not_found": 404,
    # 403: removed from the group, the item still seen through the own work
    "no_access": 403, "student_required": 403,
    # 409: the state or the time
    "not_an_assignment": 409, "item_archived": 409, "already_graded": 409, "deadline_passed": 409,
    "quota_exceeded": 409,
    # 422: the answer, the files, the links
    "answer_required": 422, "text_not_expected": 422, "text_too_long": 422, "files_required": 422,
    "files_not_expected": 422, "too_many_files": 422, "submission_too_large": 422, "file_type_not_allowed": 422,
    "file_content_mismatch": 422, "file_empty": 422, "file_name_invalid": 422, "duplicate_name": 422,
    "link_invalid": 422, "link_title_invalid": 422, "not_a_student": 422, "invalid_request": 422,
    # 413: a draft file over JUDGE_SUBMISSION_FILE_MAX_MB, by Content-Length or while the body arrives
    "file_too_large": 413,
}
STUDENT_STATUS = {**CONTEST_STATUS, **CLASSROOM_STATUS}
# codes no request of a student can reach (the tests of reachability skip them, each with its reason); kept by code:
# a code reachable on one route is not here even when another route cannot reach it
UNREACHABLE = {
    "admins_do_not_join": "an admin is refused student_required before any service runs",
    "problem_hidden": "a hidden problem is problem_not_found (listed_problem) before can_submit",
    "not_available_yet": "a problem before available_from is problem_not_found (listed_problem) before can_submit, "
                         "at the same `now`; an item before it is item_not_found (feed.seen_item)",
    "not_a_student": "the classroom services' guard: an admin is refused student_required first",
    "file_name_invalid": "the name is cleaned before the rules see it (content_files.clean_name: never empty, no "
                         "separators or control characters, cut to 150)",
}


def refused(exc) -> ApiError:
    """The ApiError of a refusal, a StudentError or a FeedError of the class feed (its data - the index of a file or a
    link, the quota's used/adding/quota, the id of a draft file - in the body, every reason in problems when there
    are several); a wait (rate_limited, cooldown) carries Retry-After and retry_after_s."""
    status = STUDENT_STATUS[exc.code]
    if isinstance(exc, FeedError):
        data = dict(exc.data)
        wait = data.pop("retry_after_s", None)
        headers = None
        if wait is not None:
            data["retry_after_s"] = wait = max(1, int(wait))
            headers = {"Retry-After": str(wait)}
        return ApiError(status, exc.code, str(exc), problems=exc.problems if len(exc.problems) > 1 else None,
                        headers=headers, **data)
    if exc.retry_after_s is not None:
        wait = max(1, int(exc.retry_after_s))
        return ApiError(status, exc.code, str(exc), headers={"Retry-After": str(wait)}, retry_after_s=wait)
    return ApiError(status, exc.code, str(exc) if str(exc) != exc.code else None)


def blobs(request: Request) -> BlobStore:
    return BlobStore(request.app.state.settings.blob_dir)
