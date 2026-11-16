"""A student's draft of a manual assignment: files uploaded one at a time before the hand-in (4b.2, D-097..D-099,
docs/CLASSROOM.md §10 «Черновик ученика»).

The student is always the User of the session (the command line's in dev_seed); a draft is found by the pair (item,
student), and a draft file by its id inside that pair only: another student's or another item's id is
draft_file_not_found, like a missing or a deleted one.

Who may add a file. Exactly whoever could hand in one file now: access.can_submit_work with one attachment and no text
(not_published, item_archived, no_access, deadline_passed, already_graded, files_not_expected, ...), after the item
is visible to the student at all (items.student_sees: in their feed, or their own work is there) - otherwise
item_not_found, one answer for "no such item" and "not yours".

Order of an upload (D-082, D-095): precheck before the body, without locks or writes (the caller rolls back): the
item, can_submit_work, attachment_rules.check_draft_name, the quota by what is known (the declared length). Then the
body goes into a blobstore.Incoming, and add_file decides everything again under the student's lock
(locks.STUDENT_FILES, the one the hand-in takes first too): the item, can_submit_work, check_draft_file (count, the
file, the repeated name, the sum), the quota - and only then takes the file into the store and writes the row.

The store is shared and content-addressed, so an upload must not tell whether the same bytes were stored before (a
teacher's solution, say): the answer and every code are the same either way. The quota counts only the student's own
rows (their hand-ins and drafts, quota_used): a blob someone else stored is new to them and costs its size; adopting
it leaves the stored file as it is and drops the copy (Incoming.adopt).

A deleted file keeps its row (deleted_at) and so its place in the quota until the reaper removes it (purge); the blob
stays for the collector of module 6. The draft of a pair lives as a whole (D-100): its files expire JUDGE_DRAFT_TTL_DAYS
after the later of the student's own due moment (with extensions) and the last change of the draft (an upload, a
delete, from_last); without a due moment, after the last change. Every file shows that moment (expires_at).

Functions flush but never commit; a refusal raises FeedError and the caller rolls back.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import deadline_rules, locks
from ..blobstore import Incoming
from ..content_files import clean_name
from ..models import AssignmentSubmissionFile, FeedItem, SubmissionFileKind, User, WorkDraftFile
from . import access
from . import attachment_rules as rules
from . import attachments as files_service
from . import items as feed
from . import works
from .items import FeedError


# ----------------------------------------------------------------------------- reading
def _live(db: Session, item: FeedItem, user: User, lock: bool = False) -> list[WorkDraftFile]:
    query = (select(WorkDraftFile).where(WorkDraftFile.item_id == item.id, WorkDraftFile.user_id == user.id,
                                         WorkDraftFile.deleted_at.is_(None)).order_by(WorkDraftFile.id))
    if lock:
        query = query.with_for_update()
    return list(db.execute(query).scalars())


def describe(row: WorkDraftFile, expires_at: Optional[datetime] = None) -> dict:
    """What the student sees of a draft file: id, name, size, when uploaded, when the reaper removes it (expires_at,
    the same for the whole draft). The content goes by draft_file only."""
    return {"id": row.id, "name": row.name, "size": row.size, "created_at": row.created_at, "expires_at": expires_at}


# the moment of a row's last change: its upload, or its delete
_CHANGED = func.greatest(WorkDraftFile.created_at, func.coalesce(WorkDraftFile.deleted_at, WorkDraftFile.created_at))


def last_change(db: Session, item_id: int, user_id: int) -> Optional[datetime]:
    """The last upload or delete among the pair's rows (deleted ones included); None: no rows."""
    return db.execute(select(func.max(_CHANGED)).where(WorkDraftFile.item_id == item_id,
                                                       WorkDraftFile.user_id == user_id)).scalar_one()


def expiry(item: FeedItem, extensions, changed: Optional[datetime], ttl_days: int) -> Optional[datetime]:
    """When the draft of the pair expires: ttl_days after the later of the effective due moment (deadline_rules,
    with the student's extensions) and the last change; without a due moment, after the last change. None: no
    rows. The later of the two, so a file uploaded after the due moment (a late hand-in allowed) never expires at
    once."""
    if changed is None:
        return None
    due = deadline_rules.effective_due(item, extensions)
    return (changed if due is None else max(due, changed)) + timedelta(days=ttl_days)


def expires_at(db: Session, user: User, item: FeedItem, ttl_days: int = rules.DRAFT_TTL_DAYS) -> Optional[datetime]:
    return expiry(item, feed.due_moments(db, item, user), last_change(db, item.id, user.id), ttl_days)


def _visible(db: Session, user: User, item: FeedItem, now: datetime) -> None:
    works._student(user)
    if not feed.student_sees(db, user, item, now):
        raise FeedError(f"no item {item.id} here", "item_not_found")


def list_draft(db: Session, user: User, item: FeedItem, now: datetime) -> list[WorkDraftFile]:
    """The live files of the student's draft of the item, in the order of upload."""
    _visible(db, user, item, now)
    return _live(db, item, user)


def draft_view(db: Session, user: User, item: FeedItem, now: datetime,
               ttl_days: int = rules.DRAFT_TTL_DAYS) -> list[dict]:
    """The student's draft as they see it: describe() of every live file, each with the draft's expires_at."""
    rows = list_draft(db, user, item, now)
    when = expires_at(db, user, item, ttl_days) if rows else None
    return [describe(r, when) for r in rows]


def draft_file(db: Session, user: User, item: FeedItem, file_id: int, now: datetime) -> WorkDraftFile:
    """One live file of the student's own draft of this item, to be served by content_files.file_response after this
    check (always as content_files decides: a picture inline only when bytes and name agree, anything else as an
    attachment). draft_file_not_found: another student's, another item's, deleted, missing."""
    _visible(db, user, item, now)
    row = db.execute(select(WorkDraftFile).where(WorkDraftFile.id == file_id, WorkDraftFile.item_id == item.id,
                                                 WorkDraftFile.user_id == user.id,
                                                 WorkDraftFile.deleted_at.is_(None))).scalar_one_or_none()
    if row is None:
        raise FeedError(f"no file {file_id} in your draft", "draft_file_not_found")
    return row


# ----------------------------------------------------------------------------- adding
def _decide(db: Session, user: User, item: FeedItem, now: datetime) -> None:
    """Whether a hand-in of one file would be accepted now (its codes): the draft takes what the hand-in would. The
    text is the one the answer kind needs (a stand-in for answer_kind text), so a kind that takes no files says
    files_not_expected rather than answer_required."""
    _visible(db, user, item, now)
    current = works.work_of(db, item, user)
    text = "x" if getattr(item.answer_kind, "value", item.answer_kind) == "text" else None
    d = access.can_submit_work(user, item, now, in_audience=feed.in_audience(db, item, user),
                               extensions=feed.due_moments(db, item, user),
                               work_status=current.status.value if current is not None else None, text=text,
                               attachments=1)
    if not d:
        raise FeedError(d.reason, d.reason)


def _quota(db: Session, user: User, adding: int, quota: Optional[int]) -> None:
    used, _owned = works.quota_used(db, user)
    if not rules.quota_ok(used, adding, quota):
        raise FeedError("there is no more space for your files: ask the teacher", "quota_exceeded", used=used,
                        adding=adding, quota=quota)


def precheck(db: Session, user: User, item: FeedItem, name: str, now: datetime, *,
             limits: rules.Limits = rules.Limits(), quota: Optional[int] = rules.STUDENT_QUOTA_BYTES,
             declared: Optional[int] = None) -> str:
    """The cheap refusals before the body of an upload, without locks or writes (the caller rolls back): the item,
    can_submit_work, the draft's count, the name and its type, a repeated name, and the quota with `declared` (the
    Content-Length, when sent) as if every byte were new - the content is unknown yet. Returns the cleaned name.
    add_file decides all of it again after the body."""
    _decide(db, user, item, now)
    cleaned = clean_name(name)
    live = _live(db, item, user)
    files_service.refuse(rules.check_draft_name([r.name for r in live], len(live), cleaned, limits))
    if declared:
        _quota(db, user, declared, quota)
    return cleaned


def add_file(db: Session, user: User, item: FeedItem, upload: Incoming, now: datetime, *,
             limits: rules.Limits = rules.Limits(),
             quota: Optional[int] = rules.STUDENT_QUOTA_BYTES) -> WorkDraftFile:
    """One more file of the student's draft (a finished or finishing Incoming), decided under the student's lock:
    the item and can_submit_work again, attachment_rules.check_draft_file (count, the file, a repeated name, the sum),
    the quota (only the bytes the student has nowhere among their own hand-ins and drafts) - then the file goes into
    the store and the row is written. The caller closes the Incoming either way."""
    upload.finish()
    view = files_service.file_view(upload)
    locks.take_pair(db, locks.STUDENT_FILES, user.id)
    _decide(db, user, item, now)
    live = _live(db, item, user)
    files_service.refuse(rules.check_draft_file([r.name for r in live], len(live), sum(r.size for r in live), view,
                                                limits))
    used, owned = works.quota_used(db, user, [upload.sha256])
    adding = 0 if upload.sha256 in owned else view.size
    if not rules.quota_ok(used, adding, quota):
        raise FeedError("there is no more space for your files: ask the teacher", "quota_exceeded", used=used,
                        adding=adding, quota=quota)
    sha = files_service.adopt(db, upload)
    row = WorkDraftFile(item_id=item.id, user_id=user.id, name=view.name, blob_sha256=sha, size=view.size,
                        created_at=now)
    db.add(row)
    db.flush()
    return row


def from_last(db: Session, user: User, item: FeedItem, now: datetime, *,
              limits: rules.Limits = rules.Limits()) -> list[WorkDraftFile]:
    """The files of the student's last hand-in of the item copied into the draft (after a return, a phone need not
    upload ten photos again): the same blobs, so the quota does not change. A name the draft already has is
    skipped. Decided like an upload: the item and can_submit_work, then the draft with the copies must still fit one
    hand-in (too_many_files, submission_too_large). Returns the new rows (none: nothing to copy)."""
    locks.take_pair(db, locks.STUDENT_FILES, user.id)
    _decide(db, user, item, now)
    work = works.work_of(db, item, user)
    last = works.last_submission(db, work) if work is not None else None
    if last is None:
        return []
    live = _live(db, item, user)
    names = [r.name for r in live]
    copies = []
    for f in db.execute(select(AssignmentSubmissionFile)
                        .where(AssignmentSubmissionFile.submission_id == last.id,
                               AssignmentSubmissionFile.kind == SubmissionFileKind.file)
                        .order_by(AssignmentSubmissionFile.position, AssignmentSubmissionFile.id)).scalars():
        if any(rules.same_name(f.path, n) for n in names):
            continue
        names.append(f.path)
        copies.append(f)
    if len(live) + len(copies) > limits.files_max:
        raise FeedError("too many files for one hand-in", "too_many_files")
    total = sum(r.size for r in live) + sum(f.size for f in copies)
    if limits.total_max_bytes is not None and total > limits.total_max_bytes:
        raise FeedError("the files are too large for one hand-in", "submission_too_large")
    rows = [WorkDraftFile(item_id=item.id, user_id=user.id, name=f.path, blob_sha256=f.blob_sha256, size=f.size,
                          created_at=now) for f in copies]
    db.add_all(rows)
    db.flush()
    return rows


# ----------------------------------------------------------------------------- removing
def remove(db: Session, user: User, item: FeedItem, file_id: int, now: datetime) -> WorkDraftFile:
    """The student deletes a live file of their own draft: deleted_at = now. The row and its blob stay in the quota
    until purge (an upload and a delete cannot get round the quota); the name is free again at once."""
    locks.take_pair(db, locks.STUDENT_FILES, user.id)
    row = draft_file(db, user, item, file_id, now)
    row.deleted_at = max(now, row.created_at)
    db.flush()
    return row


def purge(db: Session, now: datetime, ttl_days: int = rules.DRAFT_TTL_DAYS) -> int:
    """The reaper: every draft whose expiry (see expiry) is before now goes as a whole, deleted files included; the
    number of rows removed. Candidates are the pairs not changed for ttl_days; each is decided again under the
    student's lock (an upload or a delete at that moment wins), with the due moment and the extensions as they are
    now. Blobs stay (the collector of module 6)."""
    cutoff = now - timedelta(days=ttl_days)
    pairs = db.execute(select(WorkDraftFile.item_id, WorkDraftFile.user_id)
                       .group_by(WorkDraftFile.item_id, WorkDraftFile.user_id)
                       .having(func.max(_CHANGED) < cutoff)
                       .order_by(WorkDraftFile.user_id, WorkDraftFile.item_id)).all()
    removed = 0
    for item_id, user_id in pairs:
        locks.take_pair(db, locks.STUDENT_FILES, user_id)
        item, user = db.get(FeedItem, item_id), db.get(User, user_id)
        if item is None or user is None:
            continue
        when = expiry(item, feed.due_moments(db, item, user), last_change(db, item_id, user_id), ttl_days)
        if when is None or when >= now:
            continue
        removed += db.execute(delete(WorkDraftFile).where(WorkDraftFile.item_id == item_id,
                                                          WorkDraftFile.user_id == user_id)).rowcount
    return removed
