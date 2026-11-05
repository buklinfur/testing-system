"""Deadline extensions of contest problems (D-023): for one student or for one group, only later than due_at.

They are operational actions (judgectl extension, the admin API of 4a.3b), not part of contest.yaml. A student's
effective due moment is the latest of due_at, their own extension and the extensions of the groups they are in
now (deadline_rules.effective_due; service.extensions_for collects them). An extension changes nothing already
received: `late` of a submission is fixed when it arrives.

The rules are in app/extensions.py, shared with class feed items (D-059); this module names a contest problem as
the target and keeps its own messages, ContestError codes and audit actions (contest.extension_add / _remove).

Functions flush but never commit; every change is written to audit_log.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import extensions as ext
from ..auth import audit
from ..extensions import REASON_MAX  # noqa: F401 - re-exported, the old place of the limit
from ..models import Contest, ContestProblem, DeadlineExtension, Group, User
from .service import ContestError, visible_row


def target(contest: Contest, cp: ContestProblem) -> ext.Target:
    """A contest problem as the target of extensions."""
    return ext.Target(column=DeadlineExtension.contest_problem_id, id=cp.id, due_at=cp.due_at,
                      available_from=cp.available_from, name=cp.label,
                      no_deadline=f"{cp.label}: the problem has no due_at in {contest.slug}: there is nothing to extend",
                      audit_prefix="contest", audit_details={"contest": contest.slug, "label": cp.label})


def add_extension(db: Session, actor: audit.Actor, contest: Contest, label: str, due_at: datetime, now: datetime, *,
                  user: Optional[User] = None, group: Optional[Group] = None,
                  reason: Optional[str] = None) -> tuple[DeadlineExtension, Optional[datetime]]:
    """A later due moment for one student or one group. A second one for the same student or group replaces the
    due moment (В7); returns (the extension, the due moment it replaced or None)."""
    if (user is None) == (group is None):
        raise ContestError("an extension is for one student or for one group", "extension_subject")
    cp = visible_row(db, contest, label)
    try:
        return ext.add(db, actor, target(contest, cp), due_at, now, user=user, group=group, reason=reason)
    except ext.ExtensionError as exc:
        raise ContestError(str(exc), exc.code) from exc


def list_extensions(db: Session, contest: Contest, label: Optional[str] = None) -> list[dict]:
    query = (select(DeadlineExtension, ContestProblem, User.login, Group.name)
             .join(ContestProblem, ContestProblem.id == DeadlineExtension.contest_problem_id)
             .outerjoin(User, User.id == DeadlineExtension.user_id)
             .outerjoin(Group, Group.id == DeadlineExtension.group_id)
             .where(ContestProblem.contest_id == contest.id))
    if label is not None:
        query = query.where(ContestProblem.id == visible_row(db, contest, label).id)
    rows = db.execute(query.order_by(ContestProblem.position, DeadlineExtension.id)).all()
    return [{"id": row.id, "label": cp.label, "user": login, "group": name, "due_at": row.due_at,
             "base_due_at": cp.due_at, "reason": row.reason, "created_at": row.created_at}
            for row, cp, login, name in rows]


def remove_extension(db: Session, actor: audit.Actor, extension_id: int, now: datetime) -> dict:
    row = db.get(DeadlineExtension, extension_id)
    if row is None or row.contest_problem_id is None:
        raise ContestError(f"no extension {extension_id} (see: judgectl extension list <contest>)",
                           "extension_not_found")
    cp = db.get(ContestProblem, row.contest_problem_id)
    return ext.remove(db, actor, row, now, "contest",
                      {"contest": db.get(Contest, cp.contest_id).slug, "label": cp.label})
