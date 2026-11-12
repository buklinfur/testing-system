"""Statuses, grades and what a student sees of them (D-059, docs/CLASSROOM.md §4-§5): pure functions.

No database, no clock of their own. The service passes the rows it read (reviews and submissions of one work, in
the order they were added) and `now`; the date rules (the effective due moment for «не сдано») are
app/deadline_rules.py, through classroom/access.py.

* next_status: the table of transitions of a work; every (status, event) pair not in it is refused with a code;
* parse_points: a score or max_points as given by a person (a string or a JSON number) -> Decimal, or None;
* check_grade: a grade fits the scale of the assignment (points / pass_fail / none);
* release_target: which graded row a release publishes (grades_release = manual);
* student_view: the status, the grade and the flags a student sees.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional, Sequence

from ..deadline_rules import Decision
from . import access

NONE = None  # no work yet: «назначено» (not stored)
SUBMITTED, GRADED, RETURNED = "submitted", "graded", "returned"
STATUSES = (NONE, SUBMITTED, GRADED, RETURNED)
EVENTS = ("submit", "grade", "return", "release")

# (status, event) -> the new status. A release publishes the current grade and does not change the status.
TRANSITIONS = {
    (NONE, "submit"): SUBMITTED,
    (NONE, "grade"): GRADED,  # graded without a hand-in: work in a notebook, a zero for nothing handed in
    (SUBMITTED, "submit"): SUBMITTED,  # the latest hand-in counts, the others are history
    (SUBMITTED, "grade"): GRADED,
    (SUBMITTED, "return"): RETURNED,
    (GRADED, "grade"): GRADED,  # a regrade: the history keeps the old grade
    (GRADED, "return"): RETURNED,
    (GRADED, "release"): GRADED,
    (RETURNED, "submit"): SUBMITTED,
    (RETURNED, "grade"): GRADED,
}
# why the other pairs are refused
REFUSALS = {
    (NONE, "return"): "nothing_to_return",
    (NONE, "release"): "nothing_to_release",
    (SUBMITTED, "release"): "nothing_to_release",
    (GRADED, "submit"): "already_graded",  # until a teacher returns the work
    (RETURNED, "return"): "already_returned",
    (RETURNED, "release"): "nothing_to_release",
}


def _v(value):
    return getattr(value, "value", value)


@dataclass(frozen=True)
class Transition:
    allowed: bool
    reason: str = "ok"
    status: Optional[str] = None  # the new status when allowed

    def __bool__(self) -> bool:
        return self.allowed


def next_status(status: Optional[str], event: str) -> Transition:
    """The status of a work after an event, or a refusal. `status` None: the student has no work yet."""
    key = (_v(status), event)
    if key in TRANSITIONS:
        return Transition(True, status=TRANSITIONS[key])
    if key in REFUSALS:
        return Transition(False, REFUSALS[key])
    raise ValueError(f"unknown status or event: {key}")


# plain decimal notation, at most two decimals (the columns are Numeric(8, 2)): no exponent, NaN or Infinity
POINTS_RE = re.compile(r"-?[0-9]{1,9}(?:\.[0-9]{1,2})?")


def parse_points(value) -> Optional[Decimal]:
    """A number of points as a person gave it - a string ("4.5"), a JSON number (4.5) or a Decimal - or None when it
    is not one: more than two decimals, an exponent ("1e1"), NaN, Infinity, a bool. A float goes through str(), never
    through binary arithmetic. The range (0..max_points, > 0) is checked by the caller."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = value.strip() if isinstance(value, str) else str(value)
    if not POINTS_RE.fullmatch(text):
        return None
    return Decimal(text)


def check_grade(item, score: Optional[Decimal], passed: Optional[bool]) -> Decision:
    """A grade on the scale of the assignment: points - a score in 0..max_points and no passed; pass_fail - passed
    and no score; none - neither ("принято")."""
    grading = _v(item.grading)
    if grading == "none":
        return Decision(True) if score is None and passed is None else Decision(False, "grade_not_expected")
    if grading == "points":
        if passed is not None:
            return Decision(False, "grade_kind_mismatch")
        if score is None:
            return Decision(False, "score_required")
        if not Decimal(0) <= Decimal(score) <= Decimal(item.max_points):
            return Decision(False, "score_out_of_range")
        return Decision(True)
    if grading == "pass_fail":
        if score is not None:
            return Decision(False, "grade_kind_mismatch")
        return Decision(True) if passed is not None else Decision(False, "passed_required")
    raise ValueError(f"not an assignment scale: {grading!r}")


def seen_at_once(item) -> bool:
    """Grades a student sees without a release: grades_release = immediate, and always with grading = none - there
    «graded» only means «принято», there is nothing to hold back."""
    return _v(item.grades_release) == "immediate" or _v(item.grading) == "none"


def _released_ids(reviews: Sequence) -> set:
    return {r.release_of for r in reviews if _v(r.action) == "released"}


def _last(reviews: Sequence, action: str):
    return next((r for r in reversed(reviews) if _v(r.action) == action), None)


def release_target(item, reviews: Sequence) -> tuple:
    """(the graded row a release publishes, None) or (None, the refusal code): the current grade (the last graded
    row) of an assignment with grades_release = manual, once; with grading = none there is nothing to release."""
    if seen_at_once(item):
        return None, "release_not_needed"
    current = _last(reviews, "graded")
    if current is None:
        return None, "nothing_to_release"
    if current.id in _released_ids(reviews):
        return None, "already_released"
    return current, None


def visible_grade(item, reviews: Sequence):
    """The graded row the student sees: the last one (immediate, or grading = none), or the last one that was
    released (manual)."""
    if seen_at_once(item):
        return _last(reviews, "graded")
    released = _released_ids(reviews)
    return next((r for r in reversed(reviews) if _v(r.action) == "graded" and r.id in released), None)


def _seen(item, reviews: Sequence) -> set:
    """Ids of the graded rows a student may see: all (seen_at_once) or the released ones (manual)."""
    graded = {r.id for r in reviews if _v(r.action) == "graded"}
    return graded if seen_at_once(item) else graded & _released_ids(reviews)


def student_view(item, now: datetime, events: Sequence, extensions: Iterable[datetime] = ()) -> dict:
    """What a student sees of their work. `events`: the work's hand-ins and reviews in the order they happened, as
    pairs ("submission", row) / ("review", row) - the service reads them in that order; nothing here compares dates
    except through deadline_rules (missing).

    * status - that of the last event the student may see: a hand-in (submitted), a return (returned, seen at once)
      or a grade they may see (graded); a grade not released yet (manual) and a release itself are skipped, so a
      regrade after the release changes nothing until it is released;
    * grade - the last graded row they may see (visible_grade), with its comment;
    * returned_comment - while the work is returned;
    * late (of the latest hand-in) and missing («не сдано») - computed flags, not statuses."""
    reviews = [row for kind, row in events if kind == "review"]
    submissions = [row for kind, row in events if kind == "submission"]
    seen = _seen(item, reviews)
    status, returned = NONE, None
    for kind, row in events:
        action = "submit" if kind == "submission" else _v(row.action)
        if action == "submit":
            status = SUBMITTED
        elif action == "returned":
            status, returned = RETURNED, row
        elif action == "graded" and row.id in seen:
            status = GRADED
    grade = visible_grade(item, reviews)
    return {
        "status": status,
        "grade": None if grade is None else {"score": grade.score, "passed": grade.passed, "comment": grade.comment,
                                             "at": grade.at},
        "returned_comment": returned.comment if status == RETURNED else None,
        "submissions": len(submissions),
        "late": bool(submissions) and bool(submissions[-1].late),
        "missing": access.missing(item, now, bool(submissions), extensions),
    }
