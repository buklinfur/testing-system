"""/api/feed and /api/works of a student: the class feed, the page of one item, a teacher's file of an item, the list
of one's own works (docs/STUDENT_API.md §6, D-101). The services are app/student/feed.py and classroom.py; the draft
and the hand-in are student_api/drafts.py.

An item the student does not see - none, another class's, a draft, before available_from - is 404 item_not_found,
one answer. Files go out only through content_files.file_response (docs/FILES.md: an attachment with nosniff; inline
only PNG, JPEG and WebP whose bytes and name agree; HEIC and HEIF never), after the service found the file of this
item for this student.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..auth_api import Current, get_db, now
from ..classroom.items import FeedError
from ..content_files import file_response
from ..ids import Id, Limit
from ..jsonable import plain
from ..student import classroom as sc
from ..student import feed as sf
from .deps import blobs, refused, student_session

router = APIRouter()


@router.get("/feed")
def get_feed(request: Request, cursor: Optional[Id] = None, limit: Limit = 50, db: Session = Depends(get_db),
             cur: Current = Depends(student_session)):
    """The student's feed, newest first: their current groups and personal addressing, each item once
    (next_cursor: pass it as cursor; at most 100 a page). An assignment row carries the own deadline with extensions,
    status, released grade, late and missing; a contest row the contest's row of /api/contests (it gives no access by
    itself); a material row {id, title}; every row the number of unread comments."""
    at = now(request)
    return plain({**sf.my_feed(db, cur.user, at, cursor=cursor, limit=limit), "server_time": at})


@router.get("/feed/{item_id}")
def get_item(item_id: Id, request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The page of one item. The statement (body_md), the teacher's files and links and the links of [[памятка:…]]
    only while the statement is open (statement_hidden otherwise); of an assignment the own work, hand-ins, draft
    (with expires_at) and can_submit always. 404 item_not_found."""
    at = now(request)
    settings = request.app.state.settings
    try:
        out = sf.item_page(db, cur.user, item_id, at, blobs=blobs(request), ttl_days=settings.files.draft_ttl_days)
    except FeedError as exc:
        raise refused(exc) from exc
    return plain({**out, "server_time": at})


@router.get("/feed/{item_id}/attachments/{attachment_id}")
def get_attachment(item_id: Id, attachment_id: Id, request: Request, db: Session = Depends(get_db),
                   cur: Current = Depends(student_session)):
    """A teacher's file of the item, while the student sees the statement. 404 item_not_found, attachment_not_found
    (another item's, a link, a hidden statement)."""
    try:
        row = sc.item_file(db, cur.user, item_id, attachment_id, now(request))
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.name, request=request)


@router.get("/works")
def list_works(request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """Every work of the student - archived items and items they are no longer addressed by included - the latest
    own hand-in first; the status and the grade as the student sees them (an unreleased grade never)."""
    at = now(request)
    return plain({"items": sf.my_works(db, cur.user, at), "server_time": at})
