"""The class feed of a student (4b.2, D-100, docs/STUDENT_API.md §5): the feed itself, the page of one item, the list
of their own works. Read only; nothing here writes.

Every rule is the classroom's (classroom/access.py, deadline_rules, grading.student_view, comment_rules): this module
collects what they need and shapes it for the student. An item exists for the student only as items.student_sees
says (in their feed, or their own work is on it); anything else - a draft, an item before available_from, another
class's, no item at all - is item_not_found, one answer. A contest item gives no access by itself (D-059): its row
carries the contest's own row of the list (student.contests.contest_row), and the contest is opened at
/api/contests/{slug} by the contest's rules.

What a student never gets here: sha256 of a file, who of the teachers created something, unreleased grades (by
grading.student_view), other students' anything.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import deadline_rules
from ..blobstore import BlobStore
from ..classroom import access, attachments, comments, drafts, grading, works
from ..classroom import attachment_rules as arules
from ..classroom import items as feed
from ..classroom.items import FeedError
from ..contests import access as contest_access
from ..contests import service as contests
from ..materials import service as materials
from ..models import (AssignmentSubmission, AssignmentWork, Contest, FeedItem, FeedItemKind, FeedItemState, Material,
                      SubmissionFileKind, User)
from .common import Seen
from .contests import contest_row

PAGE_MAX = 100


def _v(value):
    return getattr(value, "value", value)


def seen_item(db: Session, user: User, item_id: int, now: datetime) -> FeedItem:
    """The item `item_id` if the student sees it at all (items.student_sees), else item_not_found."""
    works._student(user)
    item = db.get(FeedItem, item_id) if isinstance(item_id, int) else None
    if item is None or not feed.student_sees(db, user, item, now):
        raise FeedError(f"no item {item_id} here", "item_not_found")
    return item


def at(item: FeedItem) -> datetime:
    """The moment of the item in the feed: available_from, else published_at, else created_at (items.feed_order)."""
    return item.available_from or item.published_at or item.created_at


# ----------------------------------------------------------------------------- pieces of a row
def _deadline(item: FeedItem, extensions) -> dict:
    return {"available_from": item.available_from, "due_at": deadline_rules.effective_due(item, extensions),
            "after_due": {"statement": _v(item.after_due_statement), "submit": _v(item.after_due_submit)}}


def _assignment(db: Session, user: User, item: FeedItem, now: datetime, extensions) -> dict:
    work = works.work_of(db, item, user)
    view = grading.student_view(item, now, works.events(db, work) if work is not None else [], extensions)
    return {"answer_kind": _v(item.answer_kind), "grading": _v(item.grading), "max_points": item.max_points,
            **_deadline(item, extensions), "status": view["status"], "grade": view["grade"], "late": view["late"],
            "missing": view["missing"], "submissions": view["submissions"],
            "returned_comment": view["returned_comment"]}


def _contest(db: Session, user: User, item: FeedItem, now: datetime) -> dict:
    contest = db.get(Contest, item.contest_id)
    participation, in_group = contests.access_inputs(db, contest, user)
    full = contest_access.can_view_contest(user, contest, participation, now, in_group)
    return contest_row(user, Seen(contest, participation, in_group, full), now)


def _material(db: Session, item: FeedItem) -> dict:
    material = db.get(Material, item.material_id)
    return {"id": material.id, "title": material.title}


def _row(db: Session, user: User, item: FeedItem, now: datetime, unread: int) -> dict:
    out = {"id": item.id, "kind": _v(item.kind), "title": item.title, "at": at(item), "unread": unread,
           "archived": item.state == FeedItemState.archived}
    if item.kind == FeedItemKind.assignment:
        out["assignment"] = _assignment(db, user, item, now, feed.due_moments(db, item, user))
    elif item.kind == FeedItemKind.contest:
        out["contest"] = _contest(db, user, item, now)
    elif item.kind == FeedItemKind.material:
        out["material"] = _material(db, item)
    return out


# ----------------------------------------------------------------------------- the feed
def my_feed(db: Session, user: User, now: datetime, *, cursor: Optional[int] = None, limit: int = 50) -> dict:
    """The student's feed (items.student_feed: their current groups and personal addressing, each item once, newest
    first), a page of `limit` after the item `cursor` (next_cursor: the id of the last item of the page, while more
    follow). The cursor is a position in the order (at, id), so an item appearing or leaving between pages moves
    nothing else."""
    works._student(user)
    limit = max(1, min(int(limit), PAGE_MAX))
    found = feed.student_feed(db, user, now)
    if cursor is not None:
        mark = db.get(FeedItem, cursor) if isinstance(cursor, int) else None
        if mark is None:
            found = []
        else:
            key = (at(mark), mark.id)
            found = [item for item in found if (at(item), item.id) < key]
    page = found[:limit]
    unread = comments.unread_by_item(db, user, page, now)
    return {"items": [_row(db, user, item, now, unread[item.id]) for item in page],
            "next_cursor": page[-1].id if len(found) > limit else None}


# ----------------------------------------------------------------------------- the page of an item
def student_attachment(row) -> dict:
    """A teacher's attachment as a student sees it: no sha256, no author."""
    if row.kind.value == "file":
        return {"id": row.id, "kind": "file", "name": row.name, "size": row.size}
    return {"id": row.id, "kind": "link", "url": row.url, "title": row.title}


def _can_submit(user: User, item: FeedItem, now: datetime, in_audience: bool, extensions, status) -> dict:
    """Whether a hand-in would be accepted now, the answer itself aside: access.can_submit_work with a stand-in
    answer of the item's kind, so the code is about the item, the deadline and the grade (late: accepted as late).
    `status`: the one the student sees (grading.student_status, D-102)."""
    kind = _v(item.answer_kind)
    d = access.can_submit_work(user, item, now, in_audience=in_audience, extensions=extensions,
                               work_status=status,
                               text="x" if kind in ("text", "text_or_files") else None,
                               attachments=1 if kind == "files" else 0)
    return {"allowed": d.allowed, "reason": d.reason, "late": d.late}


def _history(db: Session, blobs: BlobStore, work) -> list:
    """The student's own hand-ins, oldest first: moment, late, the text answer, the files (the text answer is not
    among them: it is `text`) and the links. Reviews are not listed: what of them the student sees is in status and
    grade (grading.student_view)."""
    if work is None:
        return []
    out = []
    for sub in db.execute(select(AssignmentSubmission).where(AssignmentSubmission.work_id == work.id)
                          .order_by(AssignmentSubmission.seq)).scalars():
        listed = works.submission_files(db, sub)
        out.append({"id": sub.id, "at": sub.created_at, "late": sub.late, "text": works.answer_text(db, blobs, sub),
                    "files": [{"id": f["id"], "name": f["name"], "size": f["size"]} for f in listed["files"]
                              if f["kind"] == SubmissionFileKind.file.value],
                    "links": listed["links"]})
    return out


def _unread(db: Session, user: User, item: FeedItem, now: datetime, student: Optional[User]) -> Optional[int]:
    """Unread comments of a thread the student reads now; None: they do not read it."""
    try:
        return comments.unread_count(db, user, item, now, student=student)
    except FeedError:
        return None


def item_page(db: Session, user: User, item_id: int, now: datetime, *, blobs: BlobStore,
              ttl_days: int = arules.DRAFT_TTL_DAYS) -> dict:
    """The page of one item for the student. The body, the teacher's files and the links of [[памятка:…]] where
    the student sees the statement (attachments.can_view: an assignment's is hidden after the due moment with
    after_due_statement = hidden and when archived - then statement_hidden and none of them); the own work, its
    hand-ins, the draft and the private thread always. item_not_found as seen_item."""
    item = seen_item(db, user, item_id, now)
    open_ = attachments.can_view(db, user, item, now)
    out = {"id": item.id, "kind": _v(item.kind), "title": item.title, "at": at(item),
           "archived": item.state == FeedItemState.archived, "statement_hidden": not open_,
           "body_md": item.body_md if open_ else None,
           "attachments": [student_attachment(r) for r in attachments.list_attachments(db, item)] if open_ else None,
           "links": materials.resolve(db, user, [item.body_md], now) if open_ else [],
           "comments": {"enabled": item.comments_enabled, "shared_unread": _unread(db, user, item, now, None),
                        "private_unread": None}}
    if item.kind == FeedItemKind.assignment:
        extensions = feed.due_moments(db, item, user)
        work = works.work_of(db, item, user)
        out["assignment"] = _assignment(db, user, item, now, extensions)
        out["can_submit"] = _can_submit(user, item, now, feed.in_audience(db, item, user), extensions,
                                       works.seen_status(db, item, work))
        out["hand_ins"] = _history(db, blobs, work)
        out["draft"] = drafts.draft_view(db, user, item, now, ttl_days)
        out["comments"]["private_unread"] = _unread(db, user, item, now, user)
    elif item.kind == FeedItemKind.contest:
        out["contest"] = _contest(db, user, item, now)
    elif item.kind == FeedItemKind.material:
        out["material"] = _material(db, item)
    return out


# ----------------------------------------------------------------------------- my works
def my_works(db: Session, user: User, now: datetime) -> list:
    """Every work of the student (access.can_see_own_work: also archived items, items they are no longer
    addressed by, statements hidden), the latest hand-in first. The order is by the student's own hand-ins only: a
    teacher's review, released or not, moves nothing."""
    works._student(user)
    last = func.max(AssignmentSubmission.created_at)
    rows = db.execute(select(AssignmentWork, FeedItem, last.label("last"))
                      .join(FeedItem, FeedItem.id == AssignmentWork.item_id)
                      .outerjoin(AssignmentSubmission, AssignmentSubmission.work_id == AssignmentWork.id)
                      .where(AssignmentWork.user_id == user.id)
                      .group_by(AssignmentWork.id, FeedItem.id)
                      .order_by(last.desc().nulls_last(), AssignmentWork.id.desc())).all()
    out = []
    for work, item, last_at in rows:
        if not access.can_see_own_work(user, item, True):
            continue
        extensions = feed.due_moments(db, item, user)
        view = grading.student_view(item, now, works.events(db, work), extensions)
        out.append({"item": {"id": item.id, "title": item.title, "archived": item.state == FeedItemState.archived},
                    **_deadline(item, extensions), "status": view["status"], "grade": view["grade"],
                    "late": view["late"], "submissions": view["submissions"], "last_hand_in_at": last_at})
    return out
