"""A teacher's files and links of a feed item, and the file pieces shared with hand-ins (D-078, D-080,
docs/CLASSROOM.md §10).

Locking. add_file, add_link, remove and reorder take the row of the item FOR UPDATE (items.lock_item) and read the
attachments after it: two additions at once go one after another, so the limit of attachments and the duplicate
name are decided on what the other one wrote. The rules (classroom/attachment_rules.py) are pure; this module
collects what they need.

Files arrive as blobstore.Incoming: written into <blob root>/tmp with sha256, size and the first bytes counted on
the way. A file is taken into the store (Incoming.adopt, one os.replace) only after every check passed; the caller
closes the Incoming in any case, so a refused or broken upload leaves nothing behind. One window stays: a database
failure after adopt leaves a blob without a row, which the blob collector of module 6 removes.

Visibility. A teacher sees and changes everything; a student sees the attachments of an item exactly when
attachment_rules.can_view_item_files says so: an assignment with its statement (hidden after the due moment with
after_due_statement = hidden), a contest item while it is listed. Whatever a student may not see is "not found",
never "forbidden": the answer does not tell that the attachment exists.

Functions flush but never commit; a refusal raises FeedError and the caller rolls back.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..auth import audit
from ..blobstore import Incoming
from ..content_files import clean_name
from ..models import Attachment, AttachmentKind, Blob, FeedItem
from ..settings import FileSettings
from . import access
from . import attachment_rules as rules
from . import items as feed
from .items import FeedError


@dataclass(frozen=True)
class FileView:
    """What the rules see of an Incoming: the cleaned name, the size and the first bytes."""

    name: str
    size: int
    head: bytes


def file_view(upload: Incoming) -> FileView:
    """The name a browser may have sent with a path or control characters becomes its clean base name first
    (content_files.clean_name, the one files are served with); the rules then check what is left."""
    return FileView(clean_name(upload.name), upload.size, upload.head)


def refuse(decision) -> None:
    """A Decision or a Refusal of the rules as FeedError (the index of the file or link in `data`)."""
    if not decision:
        code = getattr(decision, "code", None) or decision.reason
        index = getattr(decision, "index", None)
        raise FeedError(code if index is None else f"{code} (#{index + 1})", code,
                        **({} if index is None else {"index": index}))


def teacher_limits(files: FileSettings) -> rules.Limits:
    return rules.Limits(files.item_attachments_max, files.attachment_max_bytes, None)


def hand_in_limits(files: FileSettings) -> rules.Limits:
    return rules.Limits(files.submission_files_max, files.submission_file_max_bytes,
                        files.submission_total_max_bytes)


def ensure_blob(db: Session, sha256: str, size: int) -> None:
    """The row of a blob, written once whoever comes first (two transactions may adopt the same content)."""
    db.execute(pg_insert(Blob).values(sha256=sha256, size_bytes=size).on_conflict_do_nothing(index_elements=["sha256"]))


def adopt(db: Session, upload: Incoming) -> str:
    """The upload into the store and its blobs row: the last step, after every check."""
    sha = upload.adopt()
    ensure_blob(db, sha, upload.size)
    return sha


# ----------------------------------------------------------------------------- reading
def list_attachments(db: Session, item: FeedItem) -> list[Attachment]:
    return list(db.execute(select(Attachment).where(Attachment.item_id == item.id)
                           .order_by(Attachment.position, Attachment.id)).scalars())


def describe(row: Attachment) -> dict:
    """An attachment as the admin API and judgectl show it (and the audit records it): never the content."""
    out = {"id": row.id, "kind": row.kind.value, "position": row.position, "created_by": row.created_by,
           "created_at": row.created_at}
    if row.kind == AttachmentKind.file:
        out.update(name=row.name, size=row.size, sha256=row.blob_sha256)
    else:
        out.update(url=row.url, title=row.title)
    return out


def can_view(db: Session, viewer, item: FeedItem, now: datetime) -> bool:
    if access.is_admin(viewer):
        return True
    return rules.can_view_item_files(viewer, item, now, **feed.view_inputs(db, item, viewer))


def visible_attachments(db: Session, viewer, item: FeedItem, now: datetime) -> Optional[list[Attachment]]:
    """The attachments of the item the viewer sees now, in their order; None when the viewer does not see them at
    all (the student endpoints of 4b.2 answer not found then)."""
    return list_attachments(db, item) if can_view(db, viewer, item, now) else None


def item_file(db: Session, viewer, item: FeedItem, attachment_id: int, now: datetime) -> Attachment:
    """One file attachment of this item for the viewer, to be served by content_files.file_response after this
    check. attachment_not_found: no such attachment of this item (another item's included), a link, or not visible
    to the viewer now."""
    row = db.execute(select(Attachment).where(Attachment.id == attachment_id, Attachment.item_id == item.id)
                     ).scalar_one_or_none()
    if row is None or row.kind != AttachmentKind.file or not can_view(db, viewer, item, now):
        raise FeedError(f"no file attachment {attachment_id} of item {item.id}", "attachment_not_found")
    return row


# ----------------------------------------------------------------------------- changes (teachers)
def _locked(db: Session, item: FeedItem) -> tuple[FeedItem, list[Attachment]]:
    item = feed.lock_item(db, item)
    refuse(rules.can_change_attachments(item))
    return item, list_attachments(db, item)


def _next_position(rows: Sequence[Attachment]) -> int:
    return max((r.position for r in rows), default=-1) + 1


def precheck_file(db: Session, item: FeedItem, name: str, limits: rules.Limits = rules.TEACHER_LIMITS) -> str:
    """The cheap refusals of a file attachment before its body is received (the admin API calls it before
    save_body): the item is archived, already has its limit of attachments, the cleaned name is not allowed or
    repeats an attachment's. No lock and nothing written: add_file decides again under the lock of the item, after
    the body. Returns the cleaned name."""
    refuse(rules.can_change_attachments(item))
    rows = list_attachments(db, item)
    if len(rows) + 1 > limits.files_max:
        refuse(rules.Refusal("too_many_attachments"))
    clean = clean_name(name)
    refuse(rules.check_name(clean))
    if any(rules.same_name(clean, r.name) for r in rows if r.kind == AttachmentKind.file):
        refuse(rules.Refusal("duplicate_name", 0))
    return clean


def add_file(db: Session, actor: audit.Actor, item: FeedItem, upload: Incoming, now: datetime, *,
             limits: rules.Limits = rules.TEACHER_LIMITS) -> Attachment:
    """A file attachment, appended. The upload must be finished; it is taken into the store only when accepted
    (the caller closes it either way)."""
    upload.finish()
    item, rows = _locked(db, item)
    view = file_view(upload)
    refuse(rules.check_new_attachment([r.name for r in rows if r.kind == AttachmentKind.file], len(rows),
                                      file=view, limits=limits))
    sha = adopt(db, upload)
    row = Attachment(item_id=item.id, kind=AttachmentKind.file, name=view.name, blob_sha256=sha, size=view.size,
                     position=_next_position(rows), created_by=actor.user_id, created_at=now)
    db.add(row)
    db.flush()
    audit.record(db, actor, "item.attachment_add", now, object_type="feed_item", object_id=item.id,
                 details={"attachment": row.id, "kind": "file", "name": row.name, "sha256": sha, "size": row.size})
    return row


def add_link(db: Session, actor: audit.Actor, item: FeedItem, url: str, now: datetime, *,
             title: Optional[str] = None, limits: rules.Limits = rules.TEACHER_LIMITS) -> Attachment:
    """A link attachment, appended: stored as sent after strip (a blank title: none); the server never opens it."""
    item, rows = _locked(db, item)
    link = rules_link(url, title)
    refuse(rules.check_new_attachment([r.name for r in rows if r.kind == AttachmentKind.file], len(rows),
                                      link=link, limits=limits))
    row = Attachment(item_id=item.id, kind=AttachmentKind.link, url=link.url, title=link.title,
                     position=_next_position(rows), created_by=actor.user_id, created_at=now)
    db.add(row)
    db.flush()
    audit.record(db, actor, "item.attachment_add", now, object_type="feed_item", object_id=item.id,
                 details={"attachment": row.id, "kind": "link", "url": row.url, "title": row.title})
    return row


@dataclass(frozen=True)
class LinkIn:
    """A link as received: url and an optional title."""

    url: str
    title: Optional[str] = None


def rules_link(url, title=None) -> LinkIn:
    """A link as it is checked and stored: url and title stripped, a blank title none (anything that is not a
    string is left for the rules to refuse)."""
    url = url.strip() if isinstance(url, str) else url
    if isinstance(title, str):
        title = title.strip() or None
    return LinkIn(url, title)


def remove(db: Session, actor: audit.Actor, item: FeedItem, attachment_id: int, now: datetime) -> dict:
    """Delete the row (the blob stays: other rows may use it, the collector of module 6 decides); the audit keeps
    what it was - name, sha and size, or url and title - never the content."""
    item, rows = _locked(db, item)
    row = next((r for r in rows if r.id == attachment_id), None)
    if row is None:
        raise FeedError(f"no attachment {attachment_id} of item {item.id}", "attachment_not_found")
    details = describe(row)
    details.pop("created_at")
    details["attachment"] = details.pop("id")
    db.delete(row)
    db.flush()
    audit.record(db, actor, "item.attachment_remove", now, object_type="feed_item", object_id=item.id,
                 details=details)
    return details


def reorder(db: Session, actor: audit.Actor, item: FeedItem, ids: Sequence[int], now: datetime) -> list[Attachment]:
    """The new order: `ids` must be exactly the attachments of the item now, each once - nothing missing, nothing
    extra, no repeat (attachment_order_invalid otherwise, with what is wrong in `data`)."""
    item, rows = _locked(db, item)
    current = [r.id for r in rows]
    ids = list(ids)
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    missing = sorted(set(current) - set(ids))
    extra = sorted(set(ids) - set(current), key=str)
    if repeated or missing or extra:
        raise FeedError(f"the order must list every attachment of item {item.id} once: {current}",
                        "attachment_order_invalid", missing=missing, extra=extra, repeated=repeated)
    by_id = {r.id: r for r in rows}
    for position, attachment_id in enumerate(ids):
        by_id[attachment_id].position = position
    db.flush()
    if ids != current:
        audit.record(db, actor, "item.attachment_order", now, object_type="feed_item", object_id=item.id,
                     details={"before": current, "after": ids})
    return [by_id[i] for i in ids]


def count(db: Session, item: FeedItem) -> int:
    return db.execute(select(func.count()).select_from(Attachment).where(Attachment.item_id == item.id)).scalar_one()
