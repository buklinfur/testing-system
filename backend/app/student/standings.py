"""The leaderboard of a contest for a student: access.standings_open_for, then contests/standings.py over the rows."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from .. import groups as g
from .. import scores
from ..contests import access
from ..contests import standings as rules
from ..models import ContestProblem, GroupMember, Judgement, Participation, Role, Submission, User
from .common import StudentError, full_page


def standings(db: Session, user: User, slug: str, now: datetime, group_id: Optional[int] = None) -> dict:
    """The leaderboard as this student may see it now (frozen per leaderboard_cutoff). `group_id`: one of the
    contest's groups not archived (its current members, D-043), else group_not_found."""
    s = full_page(db, user, slug, now)
    c = s.contest
    if not access.standings_open_for(user, c, s.participation, now, s.in_group):
        raise StudentError("standings_closed", "the leaderboard is not open to you now")
    groups = [gr for gr in g.contest_groups(db, c) if gr.archived_at is None]
    if group_id is not None and group_id not in {gr.id for gr in groups}:
        raise StudentError("group_not_found", "no such group in this contest")

    people = select(Participation, User).join(User, User.id == Participation.user_id) \
        .where(Participation.contest_id == c.id, User.role == Role.student)
    if group_id is not None:
        people = people.where(User.id.in_(select(GroupMember.user_id).where(
            GroupMember.group_id == group_id, GroupMember.removed_at.is_(None))))
    parts = {}
    participants = []
    for part, person in db.execute(people).all():
        start = part.started_at if c.timing_mode.value == "personal" else c.starts_at
        parts[person.id] = part
        participants.append(rules.Participant(person.id, person.display_name, start))

    cps = list(db.execute(select(ContestProblem).where(ContestProblem.contest_id == c.id)
                          .order_by(ContestProblem.position, ContestProblem.id)).scalars())
    problems = [rules.Problem(cp.id, cp.label, cp.points, scores.version_max_score(db, cp.problem_version_id),
                              cp.hidden) for cp in cps]
    current = aliased(Judgement)
    rows = db.execute(select(Submission, current)
                      .outerjoin(current, and_(current.submission_id == Submission.id, current.is_current.is_(True)))
                      .where(Submission.contest_problem_id.in_([cp.id for cp in cps]),
                             Submission.user_id.in_(list(parts)))).all() if parts and cps else []
    attempts = []
    for sub, j in rows:
        window = access.contest_window_for(c, parts[sub.user_id])
        attempts.append(rules.Attempt(sub.user_id, sub.contest_problem_id, sub.created_at,
                                      window is not None and window.contains(sub.created_at),
                                      j.status.value if j is not None else "queued",
                                      j.verdict.value if j is not None and j.verdict else None,
                                      j.score if j is not None else None, j.max_score if j is not None else None))
    out = rules.standings(c.scoring_mode, problems, participants, attempts, me=user.id,
                          cutoff=access.leaderboard_cutoff(user, c, now), penalty_minutes=c.icpc_penalty_minutes)
    out.update({"group": group_id, "groups": [{"id": gr.id, "name": gr.name} for gr in groups],
                "server_time": now})
    return out
