"""Deadline extensions of any deadline holder (D-023, D-038; shared by contest problems and, from 4v.1, class feed
items, D-059): a later due moment for one student or for one group.

A *target* says what is extended: the column of deadline_extensions that points at it, its id, its due_at and
available_from, and how to name it in messages and in the audit. Contest problems use it through
contests/deadlines.py, which keeps its own signatures, messages and audit actions (contest.extension_*).

Rules: only later than due_at (a holder without due_at has nothing to extend) and later than available_from; only
for a student and for a group not archived; a second extension for the same student or group replaces the due
moment (the old one goes to the audit). Accepted submissions are never recomputed: `late` is fixed at receipt.

Functions flush but never commit; every change is written to audit_log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import audit
from .models import DeadlineExtension, Group, GroupMember, Role, User
from .timeutil import iso_utc

REASON_MAX = 200


class ExtensionError(ValueError):
    """A refusal with a stable `code` (the same codes as ContestError of contests: no_deadline, ...)."""

    def __init__(self, message: str, code: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class Target:
    column: Any  # DeadlineExtension.<column> that points at the holder, e.g. DeadlineExtension.contest_problem_id
    id: int
    due_at: Optional[datetime]
    available_from: Optional[datetime]
    name: str  # starts the messages: "A" (a contest label)
    no_deadline: str  # the whole message when the holder has no due_at
    audit_prefix: str  # "contest" -> contest.extension_add / contest.extension_remove
    audit_details: dict = field(default_factory=dict)  # what names the holder in the audit, first in details


def subject(user: Optional[User], group: Optional[Group]) -> dict:
    return {"user": user.login} if user is not None else {"group": group.name}


def clean_reason(reason: Optional[str]) -> Optional[str]:
    return " ".join((reason or "").split())[:REASON_MAX] or None


def add(db: Session, actor: audit.Actor, target: Target, due_at: datetime, now: datetime, *,
        user: Optional[User] = None, group: Optional[Group] = None,
        reason: Optional[str] = None) -> tuple[DeadlineExtension, Optional[datetime]]:
    """A later due moment for one student or one group; returns (the extension, the due moment it replaced or
    None). The holder row should be locked or fixed by the caller if its due_at may change concurrently."""
    if (user is None) == (group is None):
        raise ExtensionError("an extension is for one student or for one group", "extension_subject")
    if target.due_at is None:
        raise ExtensionError(target.no_deadline, "no_deadline")
    if due_at <= target.due_at:
        raise ExtensionError(f"{target.name}: an extension must be later than due_at ({iso_utc(target.due_at)})",
                             "extension_not_later")
    if target.available_from is not None and due_at <= target.available_from:
        # due_at is later than available_from (a CHECK), so this holds already; kept for holders whose
        # available_from may move after their due_at was set
        raise ExtensionError(f"{target.name}: an extension must be later than available_from "
                             f"({iso_utc(target.available_from)})", "extension_before_available")
    if user is not None and user.role != Role.student:
        raise ExtensionError(f"{user.login} is not a student", "not_a_student")
    if group is not None and group.archived_at is not None:
        raise ExtensionError(f"group {group.name!r} is archived", "group_archived")
    reason = clean_reason(reason)
    column, value = (DeadlineExtension.user_id, user.id) if user is not None else (DeadlineExtension.group_id, group.id)
    row = db.execute(select(DeadlineExtension).where(target.column == target.id, column == value)
                     .with_for_update()).scalar_one_or_none()
    old = None
    if row is None:
        row = DeadlineExtension(user_id=user.id if user else None, group_id=group.id if group else None,
                                due_at=due_at, reason=reason, created_at=now, created_by=actor.user_id)
        setattr(row, target.column.key, target.id)
        db.add(row)
    else:
        old = row.due_at
        row.due_at, row.reason, row.created_at, row.created_by = due_at, reason, now, actor.user_id
    db.flush()
    audit.record(db, actor, f"{target.audit_prefix}.extension_add", now, object_type="deadline_extension",
                 object_id=row.id, details={**target.audit_details, **subject(user, group), "due_at": iso_utc(due_at),
                                            "replaced": iso_utc(old), "reason": reason})
    return row, old


def remove(db: Session, actor: audit.Actor, row: DeadlineExtension, now: datetime, audit_prefix: str,
           audit_details: dict) -> dict:
    """Delete one extension; returns (and audits) the holder's audit_details, the subject and the due moment."""
    details = {**audit_details,
               **subject(db.get(User, row.user_id) if row.user_id else None,
                         db.get(Group, row.group_id) if row.group_id else None),
               "due_at": iso_utc(row.due_at)}
    extension_id = row.id
    db.delete(row)
    db.flush()
    audit.record(db, actor, f"{audit_prefix}.extension_remove", now, object_type="deadline_extension",
                 object_id=extension_id, details=details)
    return details


def due_moments(db: Session, column, target_id: int, user: User) -> list[datetime]:
    """The `extensions` of deadline_rules for this student and holder: their own extension and those of the groups
    (not archived) they are in now."""
    own = select(DeadlineExtension.due_at).where(column == target_id, DeadlineExtension.user_id == user.id)
    by_group = (select(DeadlineExtension.due_at)
                .join(GroupMember, GroupMember.group_id == DeadlineExtension.group_id)
                .join(Group, Group.id == DeadlineExtension.group_id)
                .where(column == target_id, GroupMember.user_id == user.id,
                       GroupMember.removed_at.is_(None), Group.archived_at.is_(None)))
    return list(db.execute(own.union_all(by_group)).scalars())
