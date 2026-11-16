"""What a student does with the class feed besides reading it (4b.2, D-100, docs/STUDENT_API.md §5): the files of an
item and of their own work, comments, materials and the library. Thin: each function finds the item as the student
sees it (feed.seen_item: item_not_found first, one answer for "no such item" and "not yours") and calls the classroom
service with the student as the viewer; every rule stays there. Refusals are FeedError with the service's code
(comments: rate_limited with retry_after_s); the HTTP layer maps them (student_api STUDENT_STATUS).

The student is always the User of the session and the functions take no other student: their private thread is
private_thread(student=themselves), their work's file is work_file(student=themselves).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..classroom import attachments, comments, works
from ..classroom import comment_rules
from ..materials import library as lib
from ..materials import service as materials
from ..models import Attachment, AssignmentSubmissionFile, User
from .feed import seen_item, student_attachment


# ----------------------------------------------------------------------------- files
def item_file(db: Session, user: User, item_id: int, attachment_id: int, now: datetime) -> Attachment:
    """A teacher's file of the item, where the student sees the statement (attachment_not_found otherwise)."""
    return attachments.item_file(db, user, seen_item(db, user, item_id, now), attachment_id, now)


def work_file(db: Session, user: User, item_id: int, file_id: int, now: datetime) -> AssignmentSubmissionFile:
    """A file of the student's own work on the item (the text answer answer.md too); file_not_found otherwise."""
    return works.work_file(db, user, seen_item(db, user, item_id, now), user, file_id)


# ----------------------------------------------------------------------------- comments
def shared_thread(db: Session, user: User, item_id: int, now: datetime, *, after: Optional[int] = None,
                  limit: int = comments.PAGE_MAX) -> dict:
    return comments.shared_thread(db, user, seen_item(db, user, item_id, now), now, after=after, limit=limit)


def my_thread(db: Session, user: User, item_id: int, now: datetime, *, after: Optional[int] = None,
              limit: int = comments.PAGE_MAX) -> dict:
    return comments.private_thread(db, user, seen_item(db, user, item_id, now), user, now, after=after, limit=limit)


def post_shared(db: Session, user: User, item_id: int, now: datetime, body: Optional[str], *,
                limit: Optional[int] = comment_rules.RATE_PER_WINDOW) -> dict:
    item = seen_item(db, user, item_id, now)
    return comments.comment_card(db, user, comments.post_shared(db, user, item, now, body, limit=limit))


def post_private(db: Session, user: User, item_id: int, now: datetime, body: Optional[str], *,
                 limit: Optional[int] = comment_rules.RATE_PER_WINDOW) -> dict:
    item = seen_item(db, user, item_id, now)
    return comments.comment_card(db, user, comments.post_private(db, user, item, now, body, limit=limit))


def read_shared(db: Session, user: User, item_id: int, now: datetime, up_to: Optional[int] = None) -> int:
    return comments.mark_read(db, user, seen_item(db, user, item_id, now), now, up_to=up_to)


def read_mine(db: Session, user: User, item_id: int, now: datetime, up_to: Optional[int] = None) -> int:
    return comments.mark_read(db, user, seen_item(db, user, item_id, now), now, student=user, up_to=up_to)


def delete_comment(db: Session, user: User, comment_id: int, now: datetime) -> None:
    """The student's own comment (comment_not_found for one they do not see, not_your_comment for one of
    another they see)."""
    comments.delete_own(db, user, comment_id, now)


# ----------------------------------------------------------------------------- materials and the library
def material(db: Session, user: User, material_id: int, now: datetime) -> dict:
    """The latest version of a material the student has a basis for (material_not_found otherwise, a missing id
    too); its files without sha256 or author."""
    works._student(user)
    found = materials.get_material(db, material_id)
    out = materials.material_view(db, user, found, now)
    out["attachments"] = [student_attachment(r) for r in
                          materials.version_attachments(db, materials.latest_version(db, found))]
    return out


def material_file(db: Session, user: User, material_id: int, attachment_id: int, now: datetime) -> Attachment:
    works._student(user)
    return materials.material_file(db, user, materials.get_material(db, material_id), attachment_id, now)


def library(db: Session, user: User, now: datetime, q: Optional[str] = None) -> dict:
    works._student(user)
    return lib.library_view(db, user, now, q)
