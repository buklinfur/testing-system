"""Statuses, grades and what a student sees of them (D-059, docs/CLASSROOM.md §4-§5): pure functions.

No database, no clock of their own. The service passes the rows it read (reviews and submissions of one work, in
the order they were added) and `now`; the date rules (the effective due moment for «не сдано») are
app/deadline_rules.py, through classroom/access.py.

* next_status: the table of transitions of a work; every (status, event) pair not in it is refused with a code;
* parse_points: a score or max_points as given by a person (a string or a JSON number) -> Decimal, or None;
* check_grade: a grade fits the scale of the assignment (points / pass_fail / none);
* release_target: which graded row a release publishes (grades_release = manual);
* student_status / student_view: the status, the grade and the flags a student sees;
* superseded / resubmitted_after_grade: a grade the student never saw that a later hand-in took off the work, and
  the teacher's mark «пересдано после оценки, требует проверки» (D-102).
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
    (GRADED, "submit"): "already_graded",  # a grade the student sees, until a teacher returns the work (D-102)
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


def next_status(status: Optional[str], event: str, *, grade_seen: bool = True) -> Transition:
    """The status of a work after an event, or a refusal. `status` None: the student has no work yet.

    `grade_seen` matters only for a hand-in on a graded work (D-102): a grade the student sees (released, or seen at
    once) closes the hand-in (already_graded) until a return; a grade they have not seen (manual, not released yet)
    does not - the work goes back to submitted and that grade stays in the history, for the earlier hand-in
    (superseded). The default is the closed one; the service passes student_status(...) == GRADED."""
    key = (_v(status), event)
    if key == (GRADED, "submit") and not grade_seen:
        return Transition(True, status=SUBMITTED)
    if key in TRANSITIONS:
        return Transition(True, status=TRANSITIONS[key])
    if key in REFUSALS:
        return Transition(False, REFUSALS[key])
    raise ValueError(f"unknown status or event: {key}")


# plain decimal notation as the columns hold it - Numeric(8, 2): at most 6 digits before the point and two after it
# (999999.99); no exponent, NaN or Infinity. A longer number would reach the database and fail there (D-088).
POINTS_RE = re.compile(r"-?[0-9]{1,6}(?:\.[0-9]{1,2})?")


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


def release_target(item, events: Sequence) -> tuple:
    """(the graded row a release publishes, None) or (None, the refusal code): the current grade (the last graded
    row) of an assignment with grades_release = manual, once; with grading = none there is nothing to release. A
    grade given to an earlier hand-in (superseded: a hand-in came after it) is never released - nothing_to_release
    (D-102; the status check of the service refuses it first, this is the same rule in the pure function).
    `events`: the work's hand-ins and reviews in their order, as student_view takes them."""
    if seen_at_once(item):
        return None, "release_not_needed"
    reviews = _reviews(events)
    current = _last(reviews, "graded")
    if current is None or current.id in superseded(item, events):
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


def _reviews(events: Sequence) -> list:
    return [row for kind, row in events if kind == "review"]


def student_status(item, events: Sequence) -> Optional[str]:
    """The status of the last event the student may see: a hand-in (submitted), a return (returned, seen at once) or
    a grade they may see (graded); a grade not released yet (manual) and a release itself are skipped. None: no
    event they see. Every refusal a student gets is decided on this status, never on the cached one, so no answer
    tells whether an unreleased grade exists (D-102)."""
    seen = _seen(item, _reviews(events))
    status = NONE
    for kind, row in events:
        action = "submit" if kind == "submission" else _v(row.action)
        if action == "submit":
            status = SUBMITTED
        elif action == "returned":
            status = RETURNED
        elif action == "graded" and row.id in seen:
            status = GRADED
    return status


def superseded(item, events: Sequence) -> set:
    """Ids of the graded rows taken off the work by a later hand-in (D-102): a grade the student never saw (not
    released, manual) with a hand-in after it. It stays in the history «к прежней сдаче», is not the current grade
    and is never released. A grade the student saw is never superseded: it closed the hand-in (already_graded) until
    a return, and after a return it stays what the student sees until a new one is released."""
    seen = _seen(item, _reviews(events))
    out, pending = set(), []
    for kind, row in events:
        if kind == "submission":
            out.update(pending)
            pending = []
        elif _v(row.action) == "graded" and row.id not in seen:
            pending.append(row.id)
    return out


def current_grade(item, events: Sequence):
    """The teacher's current grade of a work: the last graded row that a later hand-in did not take off (superseded),
    or None."""
    gone = superseded(item, events)
    return next((r for r in reversed(_reviews(events)) if _v(r.action) == "graded" and r.id not in gone), None)


def resubmitted_after_grade(item, events: Sequence) -> bool:
    """The teacher's mark «пересдано после оценки, требует проверки»: the last grade or return of the work is a grade
    a later hand-in took off (superseded), so the latest hand-in waits for a new grade (D-102)."""
    last = next((row for kind, row in reversed(events) if kind == "review" and _v(row.action) in ("graded", "returned")),
                None)
    return last is not None and _v(last.action) == "graded" and last.id in superseded(item, events)


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
    reviews = _reviews(events)
    submissions = [row for kind, row in events if kind == "submission"]
    status = student_status(item, events)
    returned = _last(reviews, "returned") if status == RETURNED else None
    grade = visible_grade(item, reviews)
    return {
        "status": status,
        "grade": None if grade is None else {"score": grade.score, "passed": grade.passed, "comment": grade.comment,
                                             "at": grade.at},
        "returned_comment": returned.comment if returned is not None else None,
        "submissions": len(submissions),
        "late": bool(submissions) and bool(submissions[-1].late),
        "missing": access.missing(item, now, bool(submissions), extensions),
    }
