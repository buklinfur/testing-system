"""Access rules of contests (R24, R25): pure functions, no database, no clock of their own.

They read attributes of a user, a contest, a contest problem and a participation (ORM rows or anything with the
same attributes) and the moment `now`. The endpoints of 4b and of the admin API call only these, and
tests/test_contest_access_units.py checks them case by case (docs/CONTESTS.md, section "Access").

Admins may do everything; for students:
* draft contests do not exist; archived ones are read-only for their participants;
* the window is [starts_at, ends_at) for fixed timing, and [started_at, min(started_at + duration, ends_at)) after
  the personal start; a personal contest without ends_at is a timed practice;
* after the window a submission is still accepted with allow_upsolving, but it does not count;
* a hidden contest problem (removed from contest.yaml while it has submissions) is invisible and closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

ADMIN, STUDENT = "admin", "student"


def _v(value) -> str:
    return getattr(value, "value", value)


def is_admin(user) -> bool:
    return _v(user.role) == ADMIN


@dataclass(frozen=True)
class Window:
    start: datetime
    end: Optional[datetime]  # None: no end

    def contains(self, now: datetime) -> bool:
        return self.start <= now and (self.end is None or now < self.end)

    def over(self, now: datetime) -> bool:
        return self.end is not None and now >= self.end


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = "ok"  # a stable code for the API and the UI
    counted: bool = False  # can_submit: the submission counts in the contest (not upsolving)
    retry_after_s: Optional[int] = None  # cooldown

    def __bool__(self) -> bool:
        return self.allowed


def _personal(contest) -> bool:
    return _v(contest.timing_mode) == "personal"


def contest_window_for(contest, participation) -> Optional[Window]:
    """The time a participant may solve in; None while a personal contest has not been started."""
    if not _personal(contest):
        return Window(contest.starts_at, contest.ends_at)
    if participation is None or participation.started_at is None:
        return None
    end = participation.started_at + contest.personal_duration
    if contest.ends_at is not None:
        end = min(end, contest.ends_at)
    return Window(participation.started_at, end)


def contest_over(contest, now: datetime) -> bool:
    return contest.ends_at is not None and now >= contest.ends_at


def can_view_contest(user, contest, participation, now: datetime) -> bool:
    if is_admin(user):
        return True
    state = _v(contest.state)
    return state == "published" or (state == "archived" and participation is not None)


def normalize_code(code: Optional[str]) -> str:
    return "".join((code or "").split()).replace("-", "").upper()


def can_join(user, contest, participation, now: datetime, code: Optional[str] = None) -> Decision:
    if is_admin(user):
        return Decision(False, "admins_do_not_join")
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    if participation is not None:
        return Decision(False, "already_joined")
    if contest_over(contest, now) and not contest.allow_upsolving:
        return Decision(False, "contest_over")
    if _v(contest.join_policy) == "code" and normalize_code(code) != normalize_code(contest.invite_code):
        return Decision(False, "wrong_code")
    return Decision(True)


def can_start(user, contest, participation, now: datetime) -> Decision:
    """The personal start ("Начать"): once, inside [starts_at, ends_at)."""
    if not _personal(contest):
        return Decision(False, "not_personal")
    if participation is None:
        return Decision(False, "not_participant")
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    if participation.started_at is not None:
        return Decision(False, "already_started")
    if now < contest.starts_at:
        return Decision(False, "not_started_yet")
    if contest_over(contest, now):
        return Decision(False, "contest_over")
    return Decision(True)


def can_view_problem(user, contest, contest_problem, participation, now: datetime) -> bool:
    """Statements are shown once the participant's window has begun (and after the contest, for upsolving)."""
    if is_admin(user):
        return True
    if contest_problem.hidden or participation is None or not can_view_contest(user, contest, participation, now):
        return False
    window = contest_window_for(contest, participation)
    if window is not None:
        return now >= window.start
    return contest_over(contest, now) and contest.allow_upsolving  # personal, never started


def can_submit(user, contest, contest_problem, participation, now: datetime, *, language: Optional[str] = None,
               last_submission_at: Optional[datetime] = None, counted_submissions: int = 0) -> Decision:
    """Whether a submission is accepted now and whether it counts. `counted_submissions`: those of this user for
    this problem that counted (the limit applies only to them; upsolving is not limited)."""
    if is_admin(user):
        return Decision(True, "admin", counted=False)
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    if participation is None:
        return Decision(False, "not_participant")
    if contest_problem.hidden:
        return Decision(False, "problem_hidden")
    if language is not None and contest.languages is not None and language not in contest.languages:
        return Decision(False, "language_not_allowed")

    window = contest_window_for(contest, participation)
    if window is None:  # personal, not started
        if contest_over(contest, now):
            counted = False
        elif now < contest.starts_at:
            return Decision(False, "not_started_yet")
        else:
            return Decision(False, "press_start")
    elif now < window.start:
        return Decision(False, "not_started_yet")
    else:
        counted = window.contains(now)
    if not counted and not contest.allow_upsolving:
        return Decision(False, "contest_over")

    if counted and contest.max_submissions_per_problem is not None \
            and counted_submissions >= contest.max_submissions_per_problem:
        return Decision(False, "submission_limit")
    if last_submission_at is not None and contest.submit_cooldown_sec:
        wait = last_submission_at + timedelta(seconds=contest.submit_cooldown_sec) - now
        if wait > timedelta(0):
            return Decision(False, "cooldown", retry_after_s=max(1, int(wait.total_seconds() + 0.999)))
    return Decision(True, "ok" if counted else "upsolving", counted=counted)


def can_see_leaderboard(user, contest, participation, now: datetime) -> bool:
    if is_admin(user):
        return True
    if participation is None or not can_view_contest(user, contest, participation, now):
        return False
    mode = _v(contest.leaderboard)
    if mode == "visible":
        return True
    if mode == "after_end":
        return contest_over(contest, now)
    return False


def leaderboard_cutoff(user, contest, now: datetime) -> Optional[datetime]:
    """The moment the leaderboard a student sees is frozen at, or None for the live one. Admins always see it live;
    the freeze lasts from freeze_at until the end of the contest."""
    if is_admin(user) or contest.freeze_at is None:
        return None
    if contest.freeze_at <= now and not contest_over(contest, now):
        return contest.freeze_at
    return None


def visible_tests(user, contest, now: datetime, groups: Iterable) -> set:
    """Names of the test groups whose content (input, answer) the user may see. `groups`: (name, visibility).
    Hidden tests never reach a student, except with tests_after_end once the contest is over."""
    groups = list(groups)
    if is_admin(user) or (contest.tests_after_end and contest_over(contest, now)):
        return {name for name, _visibility in groups}
    return {name for name, visibility in groups if _v(visibility) == "open"}
