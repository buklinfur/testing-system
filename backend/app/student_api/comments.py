"""/api/feed/{item}/comments, /api/feed/{item}/my-thread and /api/comments of a student: the shared thread of an
item, their own private thread of an assignment, read marks, deleting their own comment (docs/STUDENT_API.md §6,
D-103; the rules: docs/CLASSROOM.md §9, D-074). The services are app/student/classroom.py over classroom/comments.py.

The student is the user of the session and nothing else: no path, body or query names a student, so the private
thread is always their own (my-thread), and a comment id from a body (up_to) or a path (delete) is looked for only
where the student reads - up_to of another thread is 404 comment_not_found, a comment of a thread they do not read
(another student's private thread, an item outside their feed, a deleted one) is 404 comment_not_found too; a
comment of someone else that they do see is 403 not_your_comment. The item is found as the feed finds it
(item_not_found first); the private thread needs a manual assignment (409 not_an_assignment, reading included, as
the work and the draft). A thread they may not read is 404 thread_not_found. Comments are untrusted Markdown,
returned as stored; a student sees no deleted comment and of an author only the display name and whether a teacher
wrote it (`mine`: their own). Too many comments: 429 rate_limited with Retry-After and retry_after_s.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..auth_api import Current, get_db, now
from ..classroom import comments
from ..classroom.items import FeedError
from ..ids import After, Id, Limit
from ..jsonable import plain
from ..student import classroom as sc
from .deps import refused, student_session

router = APIRouter()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommentBody(Body):
    """Markdown, at most 4000 characters (comment_too_long); the generous limit here only guards the parser."""
    body: str = Field(max_length=100_000)


class ReadBody(Body):
    up_to: Optional[Id] = Field(None, description="a comment of this thread; by default the last one")


def _limit(limit: Limit = comments.PAGE_MAX) -> int:
    return min(limit, comments.PAGE_MAX)


def _view(fn, db: Session, request: Request, cur: Current, item_id: int, after: Optional[int], limit: int) -> dict:
    try:
        return plain(fn(db, cur.user, item_id, now(request), after=after, limit=limit))
    except FeedError as exc:
        raise refused(exc) from exc


def _post(fn, db: Session, request: Request, cur: Current, item_id: int, body: CommentBody) -> dict:
    try:
        out = fn(db, cur.user, item_id, now(request), body.body,
                 limit=request.app.state.settings.comment_rate_per_min)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(out)


def _read(fn, db: Session, request: Request, cur: Current, item_id: int, body: Optional[ReadBody]) -> dict:
    try:
        unread = fn(db, cur.user, item_id, now(request), up_to=body.up_to if body is not None else None)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"unread": unread}


# ----------------------------------------------------------------------------- the shared thread of an item
@router.get("/feed/{item_id}/comments")
def shared_thread(item_id: Id, request: Request, after: Optional[After] = None, limit: int = Depends(_limit),
                  db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """Oldest first, a page of at most 100 after the id `after` (next_after: the cursor of the next page); can_post
    and cannot_post (the code a comment would get now, the body and the rate aside); unread: the student's own.
    Reading sets no mark. 404 item_not_found, thread_not_found (the statement hidden, an archived item)."""
    return _view(sc.shared_thread, db, request, cur, item_id, after, limit)


@router.post("/feed/{item_id}/comments", status_code=201)
def post_shared(item_id: Id, body: CommentBody, request: Request, db: Session = Depends(get_db),
                cur: Current = Depends(student_session)):
    """A comment in the shared thread: 201, the comment. 403 no_access (not in the audience now, the statement
    hidden); 409 item_archived, comments_disabled, comments_teacher_only (a contest item); 422 comment_required,
    comment_too_long, comment_invalid; 429 rate_limited."""
    return _post(sc.post_shared, db, request, cur, item_id, body)


@router.post("/feed/{item_id}/comments/read")
def read_shared(item_id: Id, request: Request, body: Optional[ReadBody] = None, db: Session = Depends(get_db),
                cur: Current = Depends(student_session)):
    """The student's mark: up to `up_to` (a comment of this thread they see) or the last one; it only grows.
    {unread}: what is left. 404 comment_not_found (up_to of another thread, deleted, missing)."""
    return _read(sc.read_shared, db, request, cur, item_id, body)


# ----------------------------------------------------------------------------- the own private thread
@router.get("/feed/{item_id}/my-thread")
def my_thread(item_id: Id, request: Request, after: Optional[After] = None, limit: int = Depends(_limit),
              db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The student's own private thread with the teachers on an assignment: once it exists always (archived, after
    the due moment, removed from the group), before that while the assignment is in the feed. thread_id null:
    nobody has written yet. 404 item_not_found, thread_not_found; 409 not_an_assignment."""
    return _view(sc.my_thread, db, request, cur, item_id, after, limit)


@router.post("/feed/{item_id}/my-thread", status_code=201)
def post_mine(item_id: Id, body: CommentBody, request: Request, db: Session = Depends(get_db),
              cur: Current = Depends(student_session)):
    """A message to the teachers in the own thread (made by the first one); the due moment, a hidden statement and
    comments_enabled do not matter. 403 no_access (not in the audience now); 409 not_an_assignment, item_archived;
    422 and 429 as the shared thread."""
    return _post(sc.post_private, db, request, cur, item_id, body)


@router.post("/feed/{item_id}/my-thread/read")
def read_mine(item_id: Id, request: Request, body: Optional[ReadBody] = None, db: Session = Depends(get_db),
              cur: Current = Depends(student_session)):
    return _read(sc.read_mine, db, request, cur, item_id, body)


# ----------------------------------------------------------------------------- deleting one's own comment
@router.post("/comments/{comment_id}/delete", status_code=204)
def delete_comment(comment_id: Id, request: Request, db: Session = Depends(get_db),
                   cur: Current = Depends(student_session)):
    """Soft: nobody but the teachers sees it any more; it still counts in the rate. 404 comment_not_found (not seen:
    another student's private thread, an item outside the feed, deleted already, missing); 403 not_your_comment."""
    try:
        sc.delete_comment(db, cur.user, comment_id, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)
