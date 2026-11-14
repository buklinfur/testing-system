"""Contests of a student: the list, the page of one, entering (with the rate of attempts kept in the database, D-093)
and the personal start."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from .. import groups as g
from .. import locks, rates, scores
from ..contests import access
from ..contests import service as contests
from ..contests.service import ContestError
from ..models import (Contest, ContestEntryAttempt, ContestGroup, ContestProblem, ContestState, Group, GroupMember,
                      Judgement, JudgementStatus, Participation, ProblemVersion, Submission, User)
from ..settings import SubmitSettings
from .common import Seen, StudentError, phase, require_student, seen, started


def _window(contest: Contest, participation: Optional[Participation]) -> Optional[dict]:
    w = access.contest_window_for(contest, participation)
    return None if w is None else {"start": w.start, "end": w.end}


def _row(user: User, s: Seen, now: datetime) -> dict:
    c, part = s.contest, s.participation
    return {"slug": c.slug, "title": c.title, "mode": "full" if s.full else "own_results", "phase": phase(c, now),
            "state": c.state, "starts_at": c.starts_at, "ends_at": c.ends_at, "timing_mode": c.timing_mode,
            "personal_duration": c.personal_duration, "access_mode": c.access_mode, "has_access": s.has_access(user),
            "joined": part is not None, "joined_via": part.joined_via if part else None,
            "started_at": part.started_at if part else None, "window": _window(c, part),
            "standings_open": bool(s.full and access.standings_open_for(user, c, part, now, s.in_group))}


def my_contests(db: Session, user: User, now: datetime) -> list:
    """Every contest the student sees (can_view_contest) or took part in (can_see_own_results, mode own_results),
    newest start first. Three queries whatever the number of contests."""
    require_student(user)
    parts = {p.contest_id: p for p in db.execute(select(Participation).where(Participation.user_id == user.id))
             .scalars()}
    in_groups = set(db.execute(
        select(ContestGroup.contest_id).join(GroupMember, GroupMember.group_id == ContestGroup.group_id)
        .join(Group, Group.id == ContestGroup.group_id)
        .where(GroupMember.user_id == user.id, GroupMember.removed_at.is_(None), Group.archived_at.is_(None))
    ).scalars())
    out = []
    for c in db.execute(select(Contest).where(Contest.state != ContestState.draft)
                        .order_by(Contest.starts_at.desc(), Contest.id.desc())).scalars():
        part, in_group = parts.get(c.id), c.id in in_groups
        full = access.can_view_contest(user, c, part, now, in_group)
        if full or access.can_see_own_results(user, c, part):
            out.append(_row(user, Seen(c, part, in_group, full), now))
    return out


def best_results(db: Session, user: User, contest: Contest) -> dict:
    """{contest problem id: (submissions, best points or None)} of the student, by the current judgements."""
    current = aliased(Judgement)
    rows = db.execute(select(Submission.contest_problem_id, current.status, current.score, current.max_score,
                             ContestProblem.points)
                      .join(ContestProblem, ContestProblem.id == Submission.contest_problem_id)
                      .outerjoin(current, and_(current.submission_id == Submission.id, current.is_current.is_(True)))
                      .where(Submission.user_id == user.id, ContestProblem.contest_id == contest.id)).all()
    out: dict = {}
    for cp_id, status, score, max_score, points in rows:
        count, best = out.get(cp_id, (0, None))
        if status in (JudgementStatus.finished, JudgementStatus.internal_error):
            got = scores.contest_score(score, max_score, points)
            best = got if best is None else max(best, got)
        out[cp_id] = (count + 1, best)
    return out


def contest_view(db: Session, user: User, slug: str, now: datetime) -> dict:
    """The page of a contest. Seen only for one's own results: the row of the list and nothing else. Before the
    student's time begins (started()), the problems are not named: an empty list and their number (D-093)."""
    s = seen(db, user, slug, now)
    out = _row(user, s, now)
    out["server_time"] = now
    if not s.full:
        return out
    c = s.contest
    out["description_md"] = c.description_md
    out["groups"] = [{"id": gr.id, "name": gr.name} for gr in g.contest_groups(db, c) if gr.archived_at is None]
    rows = [cp for cp in db.execute(select(ContestProblem).where(ContestProblem.contest_id == c.id)
                                    .order_by(ContestProblem.position, ContestProblem.id)).scalars()
            if access.problem_visible(user, c, cp, s.participation, now, s.in_group)]
    out["problem_count"] = len(rows)
    if not started(c, s.participation, now):
        out["problems"] = []
        return out
    mine = best_results(db, user, c)
    problems = []
    for cp in rows:
        problems.append(problem_row(db, user, s, cp, now, mine.get(cp.id, (0, None))))
    out["problems"] = problems
    return out


def problem_row(db: Session, user: User, s: Seen, cp: ContestProblem, now: datetime, mine: tuple) -> dict:
    version = db.get(ProblemVersion, cp.problem_version_id)
    extensions = contests.extensions_for(db, cp, user)
    count, best = mine
    return {"label": cp.label, "titles": version.titles,
            "max_score": scores.contest_max(scores.version_max_score(db, version.id), cp.points),
            "available_from": cp.available_from, "due_at": access.effective_due(cp, extensions),
            "after_due": {"statement": cp.after_due_statement, "submit": cp.after_due_submit},
            "statement_open": access.can_view_problem(user, s.contest, cp, s.participation, now, s.in_group,
                                                      extensions),
            "mine": {"submissions": count, "best": best}}


@dataclass
class EnterOutcome:
    """The answer to an attempt to enter; the attempt is recorded either way, so the caller commits either way."""
    ok: bool
    code: str  # "ok" | "rejoin_with_code" | a refusal of access.can_enter | "contest_not_found"
    participation: Optional[Participation] = None


def _record(db: Session, user: User, contest: Optional[Contest], outcome: str, now: datetime) -> None:
    db.add(ContestEntryAttempt(user_id=user.id, contest_id=contest.id if contest else None, outcome=outcome, at=now))
    db.flush()


def enter(db: Session, user: User, slug: str, now: datetime, settings: SubmitSettings, code: Optional[str] = None,
          ip: Optional[str] = None) -> EnterOutcome:
    """An attempt to enter a contest (with its code, or without one). At most settings.enter_rate attempts per
    10 minutes per student, right or wrong, counted from contest_entry_attempts under the lock ENTER_RATE of the
    student (one at a time; then the participation row, as everywhere): the limit holds across processes and
    restarts (D-093). Over the limit: StudentError rate_limited with retry_after_s, and nothing is recorded."""
    require_student(user)
    locks.take_pair(db, locks.ENTER_RATE, user.id)
    moments = db.execute(select(ContestEntryAttempt.at).where(
        ContestEntryAttempt.user_id == user.id, ContestEntryAttempt.at > now - rates.ENTER_WINDOW)).scalars().all()
    rate = rates.window_ok(moments, now, settings.enter_rate, rates.ENTER_WINDOW)
    if not rate:
        raise StudentError("rate_limited", "too many attempts to enter a contest", rate.retry_after_s)
    try:
        s = seen(db, user, slug, now)
    except StudentError as exc:
        _record(db, user, None, exc.code, now)
        return EnterOutcome(False, exc.code)
    try:
        participation = contests.enter(db, user, s.contest, now, code, ip=ip)
    except ContestError as exc:
        _record(db, user, s.contest, exc.code, now)
        return EnterOutcome(False, exc.code)
    outcome = "rejoin_with_code" if s.participation is not None else "ok"
    _record(db, user, s.contest, outcome, now)
    return EnterOutcome(True, outcome, participation)


def start(db: Session, user: User, slug: str, now: datetime) -> Participation:
    """The personal start ("Начать"): access.can_start's refusals as StudentError."""
    s = seen(db, user, slug, now)
    try:
        return contests.start(db, user, s.contest, now)
    except ContestError as exc:
        raise StudentError(exc.code, str(exc)) from exc
