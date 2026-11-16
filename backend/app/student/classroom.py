"""What a student does with the class feed besides reading it (4b.2, D-100, D-101, docs/STUDENT_API.md §5): the files
of an item and of their own work, the draft and the hand-in, comments, materials and the library. Thin: each function
finds the item as the student sees it (feed.seen_item: item_not_found first, one answer for "no such item" and "not
yours"; the work and the draft also need a manual assignment: not_an_assignment) and calls the classroom service with
the student as the viewer; every rule stays there. Refusals are FeedError with the service's code
(comments: rate_limited with retry_after_s); the HTTP layer maps them (student_api STUDENT_STATUS).

The student is always the User of the session and the functions take no other student: their private thread is
private_thread(student=themselves), their work's file is work_file(student=themselves).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..blobstore import BlobStore, Incoming
from ..classroom import attachments, comments, drafts, works
from ..classroom import attachment_rules as arules
from ..classroom import comment_rules
from ..classroom.items import FeedError
from ..materials import library as lib
from ..materials import service as materials
from ..models import Attachment, AssignmentSubmissionFile, FeedItem, FeedItemKind, User, WorkDraftFile
from .feed import seen_item, student_attachment


def assignment(db: Session, user: User, item_id: int, now: datetime) -> FeedItem:
    """The item as seen_item finds it, and a manual assignment: the work and the draft of any other kind is
    not_an_assignment (409), for reading as for writing (D-101)."""
    item = seen_item(db, user, item_id, now)
    if item.kind != FeedItemKind.assignment:
        raise FeedError(f"item {item_id} is not an assignment", "not_an_assignment")
    return item


# ----------------------------------------------------------------------------- files
def item_file(db: Session, user: User, item_id: int, attachment_id: int, now: datetime) -> Attachment:
    """A teacher's file of the item, where the student sees the statement (attachment_not_found otherwise)."""
    return attachments.item_file(db, user, seen_item(db, user, item_id, now), attachment_id, now)


def work_file(db: Session, user: User, item_id: int, file_id: int, now: datetime) -> AssignmentSubmissionFile:
    """A file of the student's own work on the item (the text answer answer.md too); file_not_found otherwise."""
    return works.work_file(db, user, assignment(db, user, item_id, now), user, file_id)


# ----------------------------------------------------------------------------- the draft and the hand-in
def draft(db: Session, user: User, item_id: int, now: datetime, ttl_days: int = arules.DRAFT_TTL_DAYS) -> list:
    """The live files of the student's draft, each with the draft's expires_at (drafts.draft_view)."""
    return drafts.draft_view(db, user, assignment(db, user, item_id, now), now, ttl_days)


def draft_file(db: Session, user: User, item_id: int, file_id: int, now: datetime) -> WorkDraftFile:
    return drafts.draft_file(db, user, assignment(db, user, item_id, now), file_id, now)


def precheck_upload(db: Session, user: User, item_id: int, name: str, now: datetime, *, limits: arules.Limits,
                    quota: Optional[int], declared: Optional[int]) -> FeedItem:
    """The cheap refusals of one more draft file before its body (drafts.precheck); no lock, nothing written, the
    caller rolls back. Returns the item for add_draft_file."""
    item = assignment(db, user, item_id, now)
    drafts.precheck(db, user, item, name, now, limits=limits, quota=quota, declared=declared)
    return item


def add_draft_file(db: Session, user: User, item: FeedItem, upload: Incoming, now: datetime, *,
                   limits: arules.Limits, quota: Optional[int], ttl_days: int = arules.DRAFT_TTL_DAYS) -> dict:
    """The file after its body (drafts.add_file decides everything again under the student's lock): {file, draft},
    the whole draft too, as one more file moves the expires_at of every file."""
    row = drafts.add_file(db, user, item, upload, now, limits=limits, quota=quota)
    out = drafts.draft_view(db, user, item, now, ttl_days)
    return {"file": next(f for f in out if f["id"] == row.id), "draft": out}


def remove_draft_file(db: Session, user: User, item_id: int, file_id: int, now: datetime) -> None:
    drafts.remove(db, user, assignment(db, user, item_id, now), file_id, now)


def draft_from_last(db: Session, user: User, item_id: int, now: datetime, *, limits: arules.Limits,
                    ttl_days: int = arules.DRAFT_TTL_DAYS) -> dict:
    """The files of the last hand-in copied into the draft (drafts.from_last); {draft}. Nothing to copy changes
    nothing, expires_at included."""
    item = assignment(db, user, item_id, now)
    drafts.from_last(db, user, item, now, limits=limits)
    return {"draft": drafts.draft_view(db, user, item, now, ttl_days)}


def hand_in(db: Session, user: User, item_id: int, now: datetime, *, blobs: BlobStore, text: Optional[str],
            draft_ids, links, text_limit: int, limits: arules.Limits, quota: Optional[int],
            ip: Optional[str] = None) -> dict:
    """A hand-in of the text, the named draft files (by id, in this order) and the links (works.submit_work):
    {submission: {id, at, late}, work: what the student sees of the work now}."""
    item = assignment(db, user, item_id, now)
    sub, decision = works.submit_work(db, user, item, now, blobs=blobs, text=text, text_limit=text_limit, ip=ip,
                                      links=links, drafts=draft_ids, limits=limits, quota=quota)
    return {"submission": {"id": sub.id, "at": sub.created_at, "late": bool(decision.late)},
            "work": works.student_work(db, user, item, now)}


# ----------------------------------------------------------------------------- comments
# The private thread is the pair (manual assignment, the student): my_thread, post_private and read_mine find the item
# with assignment() (not_an_assignment for any other kind, as the work and the draft, D-103).
def shared_thread(db: Session, user: User, item_id: int, now: datetime, *, after: Optional[int] = None,
                  limit: int = comments.PAGE_MAX) -> dict:
    return comments.shared_thread(db, user, seen_item(db, user, item_id, now), now, after=after, limit=limit)


def my_thread(db: Session, user: User, item_id: int, now: datetime, *, after: Optional[int] = None,
              limit: int = comments.PAGE_MAX) -> dict:
    return comments.private_thread(db, user, assignment(db, user, item_id, now), user, now, after=after, limit=limit)


def post_shared(db: Session, user: User, item_id: int, now: datetime, body: Optional[str], *,
                limit: Optional[int] = comment_rules.RATE_PER_WINDOW) -> dict:
    item = seen_item(db, user, item_id, now)
    return comments.comment_card(db, user, comments.post_shared(db, user, item, now, body, limit=limit))


def post_private(db: Session, user: User, item_id: int, now: datetime, body: Optional[str], *,
                 limit: Optional[int] = comment_rules.RATE_PER_WINDOW) -> dict:
    item = assignment(db, user, item_id, now)
    return comments.comment_card(db, user, comments.post_private(db, user, item, now, body, limit=limit))


def read_shared(db: Session, user: User, item_id: int, now: datetime, up_to: Optional[int] = None) -> int:
    return comments.mark_read(db, user, seen_item(db, user, item_id, now), now, up_to=up_to)


def read_mine(db: Session, user: User, item_id: int, now: datetime, up_to: Optional[int] = None) -> int:
    return comments.mark_read(db, user, assignment(db, user, item_id, now), now, student=user, up_to=up_to)


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
