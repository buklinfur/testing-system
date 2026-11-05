"""/api/admin students' work on manual assignments: the review queue, the card of a work, grades, returns, releases
(D-059, D-060; docs/ADMIN_API.md §5.8, docs/CLASSROOM.md §4-§7). The same services as `judgectl work`.

A work is the pair (item, student) of the path and is found by that pair only: no work id, submission id or other
item reaches another work (a student's work on item 1 asked as /items/2/works/<student> is 404 work_not_found).
Answers and comments are untrusted text (ADMIN_API §4.1) and go out as they are.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictBool
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..classroom import works
from ..classroom.items import FeedError
from ..jsonable import plain
from .deps import Page, admin_actor, page, users_by_id
from .feed import POINTS, Body, blobs, get_group, get_item, get_user, refused

router = APIRouter()

QUEUE_STATUS = Literal["assigned", "submitted", "graded", "returned"]


class GradeBody(Body):
    """score for grading points (a string or a JSON number: at most two decimals), passed for pass_fail, neither
    for none; release: the student sees it at once (grades_release manual)."""
    score: Optional[POINTS] = None
    passed: Optional[StrictBool] = None
    comment: Optional[str] = Field(None, max_length=10_000)
    release: StrictBool = False


class ReturnBody(Body):
    comment: str = Field(max_length=10_000)


class ReleaseBody(Body):
    user_ids: Optional[list[int]] = Field(None, min_length=1, max_length=200)


def _card(db: Session, request: Request, item, student) -> dict:
    card = works.work_card(db, blobs(request), item, student, now(request))
    if card is None:
        raise ApiError(404, "work_not_found", f"{student.login} is not addressed by item {item.id} and has no work "
                                              "in it")
    return plain(card)


@router.get("/works")
def review_queue(request: Request, item_id: Optional[int] = None, group_id: Optional[int] = None,
                 status: Optional[QUEUE_STATUS] = None, late: Optional[bool] = None, missing: Optional[bool] = None,
                 pg: Page = Depends(page), db: Session = Depends(get_db)):
    """With item_id: every student addressed now and everyone with work on it (assigned: no work yet), the cursor is
    the user id; without: only works that exist, on every assignment, the cursor is the work id (missing needs
    item_id). group_id: its current members; late: of the latest hand-in; missing: nothing handed in, due passed."""
    item = get_item(db, item_id) if item_id is not None else None
    group = get_group(db, group_id) if group_id is not None else None
    try:
        result = works.review_queue(db, now(request), item=item, group=group, status=status, late=late,
                                    missing=missing, cursor=pg.cursor, limit=pg.limit)
    except FeedError as exc:
        raise refused(exc) from exc
    return plain(result)


@router.get("/items/{item_id}/works/{user_id}")
def work_card(item_id: int, user_id: int, request: Request, db: Session = Depends(get_db)):
    """Every hand-in with its text answer, every review (unreleased grades too), the cached status, what the student
    sees, missing. An addressee without work: status assigned, no events (a grade can be given from here). 404
    work_not_found: not addressed and no work on this item."""
    return _card(db, request, get_item(db, item_id), get_user(db, user_id))


@router.post("/items/{item_id}/works/{user_id}/grade")
def grade(item_id: int, user_id: int, body: GradeBody, request: Request, db: Session = Depends(get_db),
          actor: audit.Actor = Depends(admin_actor)):
    item, student = get_item(db, item_id), get_user(db, user_id)
    try:
        result = works.grade_work(db, actor, item, student, now(request), score=body.score, passed=body.passed,
                                  comment=body.comment, release=body.release)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"result": result, "work": _card(db, request, item, student)}


@router.post("/items/{item_id}/works/{user_id}/return")
def return_work(item_id: int, user_id: int, body: ReturnBody, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    """Back for rework with a comment: the student sees it at once and may hand in again."""
    item, student = get_item(db, item_id), get_user(db, user_id)
    try:
        result = works.return_work(db, actor, item, student, now(request), body.comment)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"result": result, "work": _card(db, request, item, student)}


@router.post("/items/{item_id}/release")
def release(item_id: int, body: ReleaseBody, request: Request, db: Session = Depends(get_db),
            actor: audit.Actor = Depends(admin_actor)):
    """Release the current grades of every work of the item, or of these students: one result per student -
    released, nothing_to_release (no work or not graded) or already_released. Unknown user_ids: 404 before
    anything changes; release_not_needed (immediate or grading none): 409 for the whole call."""
    item = get_item(db, item_id)
    students = users_by_id(db, body.user_ids) if body.user_ids is not None else None
    try:
        results = works.release_grades(db, actor, item, now(request), students)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"results": results}
