"""The leaderboard of a contest (4b.1, D-091): a pure function over rows the caller reads, no database, no clock.

Who is in it: the participants the caller passes (students with a participation; with a group filter, its current
members, D-043). What counts: the counted submissions of problems not hidden (late ones count, upsolving does not).
Each row shows the name the student chose (display_name) and nothing else of them: no login, no id; `me` marks the
row of the reader. Places are shared by equal results (1, 1, 3).

* points: per problem the best result in the contest's points (scores.contest_score, D-090); the total is their
  sum; between equal totals, whoever reached their total first (the moment of the last improvement) is higher;
* icpc: a problem is solved by its first OK; its time is the minutes from the participant's start (the personal
  start with timing personal) to that submission, plus icpc_penalty_minutes for every judged attempt before it
  that was neither CE nor IE (a fault of the system is not the student's); more solved first, then less penalty;
* freeze (`cutoff`, access.leaderboard_cutoff): submissions made at or after it change nothing; a cell only shows
  how many are waiting (`pending`), as do submissions not judged yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from .. import scores

NO_PENALTY = ("CE", "IE")
DONE = ("finished", "internal_error")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _v(value):
    return getattr(value, "value", value)


@dataclass(frozen=True)
class Problem:
    id: int  # contest_problems.id
    label: str
    points: Optional[Decimal] = None  # contest_problems.points
    max_score: Optional[Decimal] = None  # the maximum of the pinned version, shown when points is None
    hidden: bool = False


@dataclass(frozen=True)
class Participant:
    user_id: int
    name: str  # display_name
    start: Optional[datetime]  # the contest's start, or the personal start (None: not started)


@dataclass(frozen=True)
class Attempt:
    user_id: int
    problem_id: int
    at: datetime  # submissions.created_at
    counted: bool
    status: str  # of the current judgement
    verdict: Optional[str]
    score: Optional[Decimal]
    max_score: Optional[Decimal]


def standings(mode, problems: Iterable[Problem], participants: Iterable[Participant], attempts: Iterable[Attempt], *,
              me: Optional[int] = None, cutoff: Optional[datetime] = None, penalty_minutes: int = 20) -> dict:
    mode = _v(mode)
    shown = [p for p in problems if not p.hidden]
    by_id = {p.id: p for p in shown}
    people = {p.user_id: p for p in participants}
    mine: dict = {}
    for a in sorted(attempts, key=lambda a: (a.at, a.problem_id)):
        if a.counted and a.problem_id in by_id and a.user_id in people:
            mine.setdefault(a.user_id, {}).setdefault(a.problem_id, []).append(a)

    rows = []
    for person in people.values():
        cells, per_problem = {}, mine.get(person.user_id, {})
        for problem in shown:
            got = per_problem.get(problem.id)
            if got:
                cells[problem.label] = (_points_cell if mode == "points" else _icpc_cell)(
                    problem, got, person, cutoff, penalty_minutes)
        rows.append(_row(mode, person, cells, me))
    key = (lambda r: (-r["score"], r["_last"] is None, r["_last"] or EPOCH)) if mode == "points" else \
        (lambda r: (-r["solved"], r["penalty"]))
    rows.sort(key=lambda r: (key(r), r["name"]))
    place, previous = 0, None
    for i, row in enumerate(rows, 1):
        if key(row) != previous:
            place, previous = i, key(row)
        row["place"] = place
        row.pop("_last", None)
    return {"mode": mode, "frozen": cutoff is not None,
            "problems": [{"label": p.label, "max_score": scores.contest_max(p.max_score, p.points)} for p in shown],
            "rows": rows}


def _split(attempts: list, cutoff: Optional[datetime]) -> tuple:
    """(judged attempts that count now, number waiting: not judged yet or made after the freeze)."""
    judged, pending = [], 0
    for a in attempts:
        if (cutoff is not None and a.at >= cutoff) or _v(a.status) not in DONE:
            pending += 1
        else:
            judged.append(a)
    return judged, pending


def _points_cell(problem: Problem, attempts: list, person, cutoff, _penalty) -> dict:
    judged, pending = _split(attempts, cutoff)
    best, reached = Decimal(0), None
    for a in judged:
        got = scores.contest_score(a.score, a.max_score, problem.points)
        if got > best:
            best, reached = got, a.at
    return {"score": best, "attempts": len(judged), "pending": pending, "_reached": reached}


def _icpc_cell(problem: Problem, attempts: list, person, cutoff, penalty_minutes: int) -> dict:
    judged, pending = _split(attempts, cutoff)
    fails = 0
    for a in judged:
        verdict = _v(a.verdict)
        if verdict == "OK":
            start = person.start or a.at
            minutes = max(0, int((a.at - start).total_seconds() // 60))
            return {"solved": True, "attempts": fails + 1, "minutes": minutes, "pending": pending,
                    "_penalty": minutes + penalty_minutes * fails}
        if verdict not in NO_PENALTY:
            fails += 1
    return {"solved": False, "attempts": fails, "pending": pending, "_penalty": 0}


def _row(mode: str, person: Participant, cells: dict, me: Optional[int]) -> dict:
    row = {"name": person.name, "me": person.user_id == me}
    if mode == "points":
        row["score"] = sum((c["score"] for c in cells.values()), Decimal(0)).quantize(scores.CENT)
        reached = [c["_reached"] for c in cells.values() if c["_reached"] is not None]
        row["_last"] = max(reached) if reached else None
        for c in cells.values():
            c.pop("_reached")
    else:
        row["solved"] = sum(1 for c in cells.values() if c["solved"])
        row["penalty"] = sum(c["_penalty"] for c in cells.values())
        for c in cells.values():
            c.pop("_penalty")
    row["cells"] = cells
    return row
