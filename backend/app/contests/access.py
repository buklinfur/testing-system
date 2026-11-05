"""Access rules of contests (R24, R25, D-020..D-023): pure functions, no database, no clock of their own.

They read attributes of a user, a contest, a contest problem and a participation (ORM rows or anything with the
same attributes) and the moment `now`. What needs the database is passed in by the caller (the service layer):
`in_group` (the student is now in a group not archived that the contest is attached to) and `extensions` (the due
moments of the student's own extension and of the extensions of their current groups for this problem). The
endpoints of 4b and of the admin API call only these, and tests/test_contest_access_units.py checks them case by
case (docs/CONTESTS.md, section "Access").

Admins may do everything; for students:
* draft contests do not exist; archived ones are read-only for their participants who still have access;
* access by mode: open - everyone; code - a participation that came in with the code; groups - in_group;
  code_or_groups - either. A participation alone gives access only when it came in with the code;
* a student always sees their own submissions and scores (can_see_own_results), whatever the access;
* the window is [starts_at, ends_at) for fixed timing, and [started_at, min(started_at + duration, ends_at)) after
  the personal start; a personal contest without ends_at is a timed practice;
* after the window a submission is still accepted with allow_upsolving, but it does not count; deadlines of
  problems do not touch upsolving;
* a problem does not exist before its available_from (inclusive); a submission inside the window is on time while
  now <= the effective due moment (the latest of due_at and the extensions), after it after_due_submit decides;
* a hidden contest problem (removed from contest.yaml while it has submissions) is invisible and closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

ADMIN, STUDENT = "admin", "student"
CODE_MODES = ("code", "code_or_groups")
GROUP_MODES = ("groups", "code_or_groups")


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
    late: bool = False  # can_submit: counted, but after the effective due moment (after_due_submit = late)
    joined_via: Optional[str] = None  # can_enter: how the participation comes in (code / group / open)

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


def has_access(user, contest, participation, in_group: bool = False) -> bool:
    """Statements and submitting, by the access mode alone (state and time are checked by the callers)."""
    if is_admin(user):
        return True
    mode = _v(contest.access_mode)
    if mode == "open":
        return True
    by_code = participation is not None and _v(participation.joined_via) == "code"
    return (mode in CODE_MODES and by_code) or (mode in GROUP_MODES and in_group)


def can_view_contest(user, contest, participation, now: datetime, in_group: bool = False) -> bool:
    """The contest page. A published contest with a code is shown to anyone (to type the code in); one only for
    groups is not shown at all to students outside them."""
    if is_admin(user):
        return True
    state = _v(contest.state)
    if state == "published":
        return _v(contest.access_mode) != "groups" or has_access(user, contest, participation, in_group)
    return state == "archived" and participation is not None and has_access(user, contest, participation, in_group)


def can_see_own_results(user, contest, participation) -> bool:
    """A student's own submissions and scores: always once they took part, even without access any more, after a
    deadline with the statement hidden, or in an archived contest."""
    return is_admin(user) or (participation is not None and _v(contest.state) != "draft")


def normalize_code(code: Optional[str]) -> str:
    return "".join((code or "").split()).replace("-", "").upper()


def _code_ok(contest, code: Optional[str]) -> bool:
    return bool(code) and normalize_code(code) == normalize_code(contest.invite_code)


def can_enter(user, contest, participation, now: datetime, code: Optional[str] = None,
              in_group: bool = False) -> Decision:
    """The first entry creates the participation (R23, D-021); `joined_via` tells how. A student of an attached
    group comes in without a code (group), even when they type one. Already a participant without access through
    a group: a right code turns a participation that did not come in with the code into a code one
    ("rejoin_with_code"), so a student taken out of the group, or let in while the contest was open, gets access
    back with the code."""
    if is_admin(user):
        return Decision(False, "admins_do_not_join")
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    mode = _v(contest.access_mode)
    if participation is not None:
        through_group = mode in GROUP_MODES and in_group
        if mode in CODE_MODES and _v(participation.joined_via) != "code" and not through_group \
                and _code_ok(contest, code):
            return Decision(True, "rejoin_with_code", joined_via="code")
        return Decision(False, "already_joined")
    if contest_over(contest, now) and not contest.allow_upsolving:
        return Decision(False, "contest_over")
    if mode == "open":
        return Decision(True, joined_via="open")
    if mode in GROUP_MODES and in_group:
        return Decision(True, joined_via="group")
    if mode in CODE_MODES:
        return Decision(True, joined_via="code") if _code_ok(contest, code) else Decision(False, "wrong_code")
    return Decision(False, "no_access")


def can_start(user, contest, participation, now: datetime, in_group: bool = False) -> Decision:
    """The personal start ("Начать"): once, inside [starts_at, ends_at), only with access."""
    if not _personal(contest):
        return Decision(False, "not_personal")
    if participation is None:
        return Decision(False, "not_participant")
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    if not has_access(user, contest, participation, in_group):
        return Decision(False, "no_access")
    if participation.started_at is not None:
        return Decision(False, "already_started")
    if now < contest.starts_at:
        return Decision(False, "not_started_yet")
    if contest_over(contest, now):
        return Decision(False, "contest_over")
    return Decision(True)


def problem_listed(user, contest_problem, now: datetime) -> bool:
    """Whether the problem exists for the user at all (its label and title): not hidden, and from available_from
    on (inclusive)."""
    if is_admin(user):
        return True
    return not contest_problem.hidden and (contest_problem.available_from is None
                                           or now >= contest_problem.available_from)


def effective_due(contest_problem, extensions: Iterable[datetime] = ()) -> Optional[datetime]:
    """The latest of due_at and the extensions; None without due_at (an extension of nothing extends nothing)."""
    if contest_problem.due_at is None:
        return None
    return max([contest_problem.due_at, *extensions])


def past_due(contest_problem, now: datetime, extensions: Iterable[datetime] = ()) -> bool:
    """Late from the first microsecond after the effective due moment: now == due is still on time."""
    due = effective_due(contest_problem, extensions)
    return due is not None and now > due


def can_view_problem(user, contest, contest_problem, participation, now: datetime, in_group: bool = False,
                     extensions: Iterable[datetime] = ()) -> bool:
    """Statements are shown once the participant's window has begun (and after the contest, for upsolving), from
    available_from on, and after the effective due moment only with after_due_statement = visible."""
    if is_admin(user):
        return True
    if participation is None or not problem_listed(user, contest_problem, now) \
            or not can_view_contest(user, contest, participation, now, in_group) \
            or not has_access(user, contest, participation, in_group):
        return False
    window = contest_window_for(contest, participation)
    if window is not None:
        started = now >= window.start
    else:
        started = contest_over(contest, now) and contest.allow_upsolving  # personal, never started
    if not started:
        return False
    return not (past_due(contest_problem, now, extensions) and _v(contest_problem.after_due_statement) == "hidden")


def can_submit(user, contest, contest_problem, participation, now: datetime, *, language: Optional[str] = None,
               last_submission_at: Optional[datetime] = None, counted_submissions: int = 0, in_group: bool = False,
               extensions: Iterable[datetime] = ()) -> Decision:
    """Whether a submission is accepted now, whether it counts and whether it is late. `now` is the moment the
    request was received. `counted_submissions`: those of this user for this problem that counted (the limit
    applies only to them; upsolving is not limited)."""
    if is_admin(user):
        return Decision(True, "admin", counted=False)
    if _v(contest.state) != "published":
        return Decision(False, "not_published")
    if participation is None:
        return Decision(False, "not_participant")
    if not has_access(user, contest, participation, in_group):
        return Decision(False, "no_access")
    if contest_problem.hidden:
        return Decision(False, "problem_hidden")
    if language is not None and contest.languages is not None and language not in contest.languages:
        return Decision(False, "language_not_allowed")

    # the window and upsolving come before the deadline of the problem
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
    if not problem_listed(user, contest_problem, now):
        return Decision(False, "not_available_yet")

    late = False
    if counted and past_due(contest_problem, now, extensions):  # upsolving is not touched by deadlines
        policy = _v(contest_problem.after_due_submit)
        if policy == "no":
            return Decision(False, "deadline_passed")
        late = policy == "late"

    if counted and contest.max_submissions_per_problem is not None \
            and counted_submissions >= contest.max_submissions_per_problem:
        return Decision(False, "submission_limit")
    if last_submission_at is not None and contest.submit_cooldown_sec:
        wait = last_submission_at + timedelta(seconds=contest.submit_cooldown_sec) - now
        if wait > timedelta(0):
            return Decision(False, "cooldown", retry_after_s=max(1, int(wait.total_seconds() + 0.999)))
    return Decision(True, ("late" if late else "ok") if counted else "upsolving", counted=counted, late=late)


def can_see_leaderboard(user, contest, participation, now: datetime, in_group: bool = False) -> bool:
    if is_admin(user):
        return True
    if participation is None or not can_view_contest(user, contest, participation, now, in_group) \
            or not has_access(user, contest, participation, in_group):
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
