"""/api/admin comment threads of the class feed: the shared thread of an item, the private thread of a student on an
assignment, read marks, moderation, the teachers' inbox (D-072..D-077, docs/ADMIN_API.md §5.9, docs/CLASSROOM.md
§9). The same services as `judgectl comment`.

The reader is the admin of the session: unread counts and read marks are their own, another admin's marks never
change. A private thread is the pair (item, student) of the path and is found by that pair only; no comment id from
a body or a query reaches another thread (up_to of another thread is 404 comment_not_found). Only teachers name a
student in the path: the student endpoints of 4b.2 take the student from the session. Comments are untrusted text
(ADMIN_API §4.1) and go out as they are. A thread is read oldest first, so its cursor is after / next_after; the
inbox is newest first with cursor / next_cursor like every other list.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from sqlalchemy.orm import Session

from ..auth import audit
from ..auth_api import Current, get_db, now
from ..classroom import comments
from ..classroom.items import FeedError
from ..ids import After, Id, Limit
from ..jsonable import plain
from .deps import DEFAULT_LIMIT, MAX_LIMIT, Page, admin_actor, admin_session, page
from .feed import Body, get_group, get_item, get_user, refused

router = APIRouter()


class CommentBody(Body):
    """Markdown, at most 4000 characters (comment_too_long); the generous limit here only guards the parser."""
    body: str = Field(max_length=100_000)


class CommentReadBody(Body):
    up_to: Optional[Id] = Field(None, description="a comment of this thread; by default the last one")


class CommentDeleteBody(Body):
    reason: Optional[str] = Field(None, max_length=10_000, description="at most 200 characters (reason_too_long)")


def _limit(limit: Limit = DEFAULT_LIMIT) -> int:
    return min(limit, MAX_LIMIT)


def _thread(db: Session, request: Request, cur: Current, item_id: int, user_id: Optional[int],
            after: Optional[int], limit: int) -> dict:
    item = get_item(db, item_id)
    student = get_user(db, user_id) if user_id is not None else None
    try:
        if student is None:
            view = comments.shared_thread(db, cur.user, item, now(request), after=after, limit=limit)
        else:
            view = comments.private_thread(db, cur.user, item, student, now(request), after=after, limit=limit)
    except FeedError as exc:
        raise refused(exc) from exc
    return plain(view)


def _post(db: Session, request: Request, actor: audit.Actor, cur: Current, item_id: int, user_id: Optional[int],
          body: CommentBody) -> dict:
    item = get_item(db, item_id)
    student = get_user(db, user_id) if user_id is not None else None
    try:
        comment = comments.teacher_post(db, actor, item, now(request), body.body, student=student)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(comments.comment_card(db, cur.user, comment))


def _read(db: Session, request: Request, cur: Current, item_id: int, user_id: Optional[int],
          body: Optional[CommentReadBody]) -> dict:
    item = get_item(db, item_id)
    student = get_user(db, user_id) if user_id is not None else None
    try:
        unread = comments.mark_read(db, cur.user, item, now(request), student=student,
                                    up_to=body.up_to if body is not None else None)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {"unread": unread}


# ----------------------------------------------------------------------------- the shared thread
@router.get("/items/{item_id}/comments")
def shared_thread(item_id: Id, request: Request, after: Optional[After] = None,
                  limit: int = Depends(_limit), db: Session = Depends(get_db),
                  cur: Current = Depends(admin_session)):
    """Oldest first, deleted comments with their text and reason; `unread` is this admin's. Reading sets no mark
    (POST …/read does). thread_id null: nobody has written yet."""
    return _thread(db, request, cur, item_id, None, after, limit)


@router.post("/items/{item_id}/comments", status_code=201)
def post_shared(item_id: Id, body: CommentBody, request: Request, db: Session = Depends(get_db),
                cur: Current = Depends(admin_session), actor: audit.Actor = Depends(admin_actor)):
    """A teacher writes even with comments off and in a contest item; not in a draft (not_published)."""
    return _post(db, request, actor, cur, item_id, None, body)


@router.post("/items/{item_id}/comments/read")
def read_shared(item_id: Id, request: Request, body: Optional[CommentReadBody] = None, db: Session = Depends(get_db),
                cur: Current = Depends(admin_session)):
    """This admin's mark: up to `up_to` or the last comment; it only grows. {unread}: what is left."""
    return _read(db, request, cur, item_id, None, body)


# ----------------------------------------------------------------------------- the private thread of a student
@router.get("/items/{item_id}/students/{user_id}/comments")
def private_thread(item_id: Id, user_id: Id, request: Request, after: Optional[After] = None,
                   limit: int = Depends(_limit), db: Session = Depends(get_db),
                   cur: Current = Depends(admin_session)):
    """The thread of the pair (item, student) of the path: an assignment and a student addressed now or with work
    or a thread there, else 404 thread_not_found."""
    return _thread(db, request, cur, item_id, user_id, after, limit)


@router.post("/items/{item_id}/students/{user_id}/comments", status_code=201)
def post_private(item_id: Id, user_id: Id, body: CommentBody, request: Request, db: Session = Depends(get_db),
                 cur: Current = Depends(admin_session), actor: audit.Actor = Depends(admin_actor)):
    """A reply or the first message to the student (the thread is made by it); archived items too."""
    return _post(db, request, actor, cur, item_id, user_id, body)


@router.post("/items/{item_id}/students/{user_id}/comments/read")
def read_private(item_id: Id, user_id: Id, request: Request, body: Optional[CommentReadBody] = None,
                 db: Session = Depends(get_db), cur: Current = Depends(admin_session)):
    return _read(db, request, cur, item_id, user_id, body)


# ----------------------------------------------------------------------------- moderation and the inbox
@router.post("/comments/{comment_id}/delete")
def delete_comment(comment_id: Id, request: Request, body: Optional[CommentDeleteBody] = None,
                   db: Session = Depends(get_db), cur: Current = Depends(admin_session),
                   actor: audit.Actor = Depends(admin_actor)):
    """Soft: students no longer see it, teachers see the text, who deleted it and the reason. The answer is the
    deleted comment with its item and thread. Again: 409 already_deleted."""
    try:
        comment = comments.delete_comment(db, actor, comment_id, now(request),
                                          reason=body.reason if body is not None else None)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(comments.comment_card(db, cur.user, comment))


@router.get("/comments/threads")
def threads(request: Request, item_id: Optional[Id] = None, group_id: Optional[Id] = None,
            awaiting: Optional[bool] = None, unread: Optional[bool] = None, pg: Page = Depends(page),
            db: Session = Depends(get_db), cur: Current = Depends(admin_session)):
    """The inbox: every thread with comments, the one with the newest comment first (the cursor is that comment's
    id). awaiting: the last comment not deleted is a student's; unread: for this admin; group_id: private threads
    of its current members and shared threads of items addressed to it."""
    item = get_item(db, item_id) if item_id is not None else None
    group = get_group(db, group_id) if group_id is not None else None
    return plain(comments.threads_query(db, cur.user, now(request), item=item, group=group, awaiting=awaiting,
                                        unread=unread, cursor=pg.cursor, limit=pg.limit))

