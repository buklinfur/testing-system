"""Deadline rules shared by contest problems (4a.3) and class feed items (4v.1, D-059): pure functions, no database,
no clock of their own, no imports from the rest of the backend.

They read attributes of a deadline holder - anything with `available_from`, `due_at`, `after_due_statement` and
`after_due_submit` (an ORM row of contest_problems or feed_items, or a stand-in in tests) - and the moment `now`
(server time, UTC, taken when the request was received). `extensions` are the due moments of the extensions that
apply to the student (their own and those of the groups they are in now); the service layer collects them.

Boundaries (D-032, docs/CONTESTS.md "Границы времени"):
* available_from is inclusive: at that very moment the thing already exists;
* the due moment is inclusive: a submission is on time while now <= the effective due moment;
* the effective due moment is the latest of due_at and the extensions; without due_at there is no deadline and an
  extension extends nothing.
After it, after_due_statement (visible / hidden) decides about the statement and after_due_submit (no / late / yes)
about submitting.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


def _v(value) -> str:
    return getattr(value, "value", value)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = "ok"  # a stable code for the API and the UI
    counted: bool = False  # can_submit: the submission counts in the contest (not upsolving)
    retry_after_s: Optional[int] = None  # cooldown
    late: bool = False  # accepted after the effective due moment (after_due_submit = late)
    joined_via: Optional[str] = None  # can_enter: how the participation comes in (code / group / open)

    def __bool__(self) -> bool:
        return self.allowed


def available(holder, now: datetime) -> bool:
    """From available_from on, inclusive; always without it."""
    return holder.available_from is None or now >= holder.available_from


def effective_due(holder, extensions: Iterable[datetime] = ()) -> Optional[datetime]:
    """The latest of due_at and the extensions; None without due_at (an extension of nothing extends nothing)."""
    if holder.due_at is None:
        return None
    return max([holder.due_at, *extensions])


def past_due(holder, now: datetime, extensions: Iterable[datetime] = ()) -> bool:
    """Late from the first microsecond after the effective due moment: now == due is still on time."""
    due = effective_due(holder, extensions)
    return due is not None and now > due


def statement_hidden_after_due(holder, now: datetime, extensions: Iterable[datetime] = ()) -> bool:
    """The statement is closed: past the effective due moment with after_due_statement = hidden."""
    return past_due(holder, now, extensions) and _v(holder.after_due_statement) == "hidden"


def after_due_submit(holder, now: datetime, extensions: Iterable[datetime] = ()) -> Decision:
    """What the deadline says about a submission received at `now`: on time - allowed; past the effective due
    moment - after_due_submit decides: `no` refuses (deadline_passed), `late` accepts it marked late, `yes` accepts
    it as usual. Only the deadline: everything else (state, access, window, limits) is the caller's."""
    if not past_due(holder, now, extensions):
        return Decision(True)
    policy = _v(holder.after_due_submit)
    if policy == "no":
        return Decision(False, "deadline_passed")
    if policy == "late":
        return Decision(True, "late", late=True)
    return Decision(True)
