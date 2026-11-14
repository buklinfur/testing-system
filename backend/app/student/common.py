"""What every student service needs: the refusal, the contest as this student sees it, reading the head of a blob."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..blobstore import BlobStore
from ..contests import access
from ..contests import service as contests
from ..models import Contest, ContestProblem, ContestState, Participation, User


class StudentError(ValueError):
    """A refusal with a stable code (STUDENT_API.md; HTTP statuses: stage 6) and, for rates, retry_after_s."""

    def __init__(self, code: str, message: str = "", retry_after_s: Optional[int] = None):
        self.code, self.retry_after_s = code, retry_after_s
        super().__init__(message or code)


@dataclass
class Seen:
    """A contest as this student sees it now. full: its page (can_view_contest); otherwise only their own results
    (can_see_own_results: in the list in the mode «только мои результаты»)."""
    contest: Contest
    participation: Optional[Participation]
    in_group: bool
    full: bool

    def has_access(self, user: User) -> bool:
        """Statements and submitting by the access mode (a contest with a code before entering: no)."""
        return self.full and access.has_access(user, self.contest, self.participation, self.in_group)


def require_student(user: User) -> None:
    if getattr(user.role, "value", user.role) != "student":
        raise StudentError("student_required", "this is the student API")


def seen(db: Session, user: User, slug: str, now: datetime, lock: bool = False) -> Seen:
    """The contest `slug` as `user` sees it, or contest_not_found - one answer for a draft, a contest for groups the
    student is not in, and no contest at all."""
    require_student(user)
    contest = db.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none() \
        if isinstance(slug, str) else None
    if contest is None or contest.state == ContestState.draft:
        raise StudentError("contest_not_found", f"no contest {slug!r}")
    participation, in_group = contests.access_inputs(db, contest, user, lock=lock)
    full = access.can_view_contest(user, contest, participation, now, in_group)
    if not full and not access.can_see_own_results(user, contest, participation):
        raise StudentError("contest_not_found", f"no contest {slug!r}")
    return Seen(contest, participation, in_group, full)


def full_page(db: Session, user: User, slug: str, now: datetime) -> Seen:
    """The contest with its page: a contest seen only for one's own results refuses with no_access."""
    s = seen(db, user, slug, now)
    if not s.full:
        raise StudentError("no_access", "only your own results of this contest are open to you")
    return s


def phase(contest: Contest, now: datetime) -> str:
    if now < contest.starts_at:
        return "before"
    return "over" if access.contest_over(contest, now) else "running"


def started(contest: Contest, participation: Optional[Participation], now: datetime) -> bool:
    """The student's time in the contest has begun (as can_view_problem counts it): their window began, or - a
    personal contest never started - the contest is over and open for upsolving. Before it the problems are not even
    named (owner's amendment of stage 0, D-093)."""
    window = access.contest_window_for(contest, participation)
    if window is not None:
        return now >= window.start
    if contest.timing_mode.value == "personal":
        return access.contest_over(contest, now) and contest.allow_upsolving
    return False


def listed_problem(db: Session, user: User, s: Seen, label: str, now: datetime) -> ContestProblem:
    """A problem of the contest the student sees (access.problem_visible), or problem_not_found - one answer for a
    hidden problem, one before its available_from and a label that is not there."""
    cp = db.execute(select(ContestProblem).where(ContestProblem.contest_id == s.contest.id,
                                                 ContestProblem.label == label)).scalar_one_or_none() \
        if isinstance(label, str) else None
    if cp is None or not access.problem_visible(user, s.contest, cp, s.participation, now, s.in_group):
        raise StudentError("problem_not_found", f"no problem {label!r} in this contest")
    return cp


def head_text(blobs: BlobStore, sha256: Optional[str], limit: int) -> tuple:
    """(the first `limit` bytes of a blob as text - UTF-8, undecodable bytes replaced -, whether it was cut);
    (None, False) for a missing blob."""
    if not sha256:
        return None, False
    fh = blobs.open(sha256)
    if fh is None:
        return None, False
    with fh:
        data = fh.read(limit + 1)
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit
