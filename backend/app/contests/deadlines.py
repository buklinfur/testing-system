"""Deadline extensions of contest problems (D-023): for one student or for one group, only later than due_at.

They are operational actions (judgectl extension, the admin API of 4a.3b), not part of contest.yaml. A student's
effective due moment is the latest of due_at, their own extension and the extensions of the groups they are in
now (access.effective_due; service.extensions_for collects them). An extension changes nothing already received: `late` of a submission is fixed when it
arrives.

Functions flush but never commit; every change is written to audit_log.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit
from ..models import Contest, ContestProblem, DeadlineExtension, Group, Role, User
from ..timeutil import iso_utc
from .service import ContestError, visible_row

REASON_MAX = 200


def _subject(user: Optional[User], group: Optional[Group]) -> dict:
    return {"user": user.login} if user is not None else {"group": group.name}


def add_extension(db: Session, actor: audit.Actor, contest: Contest, label: str, due_at: datetime, now: datetime, *,
                  user: Optional[User] = None, group: Optional[Group] = None,
                  reason: Optional[str] = None) -> tuple[DeadlineExtension, Optional[datetime]]:
    """A later due moment for one student or one group. A second one for the same student or group replaces the
    due moment (В7); returns (the extension, the due moment it replaced or None)."""
    if (user is None) == (group is None):
        raise ContestError("an extension is for one student or for one group", "extension_subject")
    cp = visible_row(db, contest, label)
    if cp.due_at is None:
        raise ContestError(f"{label}: the problem has no due_at in {contest.slug}: there is nothing to extend",
                           "no_deadline")
    if due_at <= cp.due_at:
        raise ContestError(f"{label}: an extension must be later than due_at ({iso_utc(cp.due_at)})",
                           "extension_not_later")
    if user is not None and user.role != Role.student:
        raise ContestError(f"{user.login} is not a student", "not_a_student")
    if group is not None and group.archived_at is not None:
        raise ContestError(f"group {group.name!r} is archived", "group_archived")
    reason = " ".join((reason or "").split())[:REASON_MAX] or None
    column, value = (DeadlineExtension.user_id, user.id) if user is not None else (DeadlineExtension.group_id, group.id)
    row = db.execute(select(DeadlineExtension).where(DeadlineExtension.contest_problem_id == cp.id, column == value)
                     .with_for_update()).scalar_one_or_none()
    old = None
    if row is None:
        row = DeadlineExtension(contest_problem_id=cp.id, user_id=user.id if user else None,
                                group_id=group.id if group else None, due_at=due_at, reason=reason, created_at=now,
                                created_by=actor.user_id)
        db.add(row)
    else:
        old = row.due_at
        row.due_at, row.reason, row.created_at, row.created_by = due_at, reason, now, actor.user_id
    db.flush()
    audit.record(db, actor, "contest.extension_add", now, object_type="deadline_extension", object_id=row.id,
                 details={"contest": contest.slug, "label": label, **_subject(user, group), "due_at": iso_utc(due_at),
                          "replaced": iso_utc(old), "reason": reason})
    return row, old


def list_extensions(db: Session, contest: Contest, label: Optional[str] = None) -> list[dict]:
    query = (select(DeadlineExtension, ContestProblem, User.login, Group.name)
             .join(ContestProblem, ContestProblem.id == DeadlineExtension.contest_problem_id)
             .outerjoin(User, User.id == DeadlineExtension.user_id)
             .outerjoin(Group, Group.id == DeadlineExtension.group_id)
             .where(ContestProblem.contest_id == contest.id))
    if label is not None:
        query = query.where(ContestProblem.id == visible_row(db, contest, label).id)
    rows = db.execute(query.order_by(ContestProblem.position, DeadlineExtension.id)).all()
    return [{"id": ext.id, "label": cp.label, "user": login, "group": name, "due_at": ext.due_at,
             "base_due_at": cp.due_at, "reason": ext.reason, "created_at": ext.created_at}
            for ext, cp, login, name in rows]


def remove_extension(db: Session, actor: audit.Actor, extension_id: int, now: datetime) -> dict:
    row = db.get(DeadlineExtension, extension_id)
    if row is None:
        raise ContestError(f"no extension {extension_id} (see: judgectl extension list <contest>)",
                           "extension_not_found")
    cp = db.get(ContestProblem, row.contest_problem_id)
    subject = _subject(db.get(User, row.user_id) if row.user_id else None,
                       db.get(Group, row.group_id) if row.group_id else None)
    details = {"contest": db.get(Contest, cp.contest_id).slug, "label": cp.label, **subject,
               "due_at": iso_utc(row.due_at)}
    db.delete(row)
    db.flush()
    audit.record(db, actor, "contest.extension_remove", now, object_type="deadline_extension", object_id=extension_id,
                 details=details)
    return details
