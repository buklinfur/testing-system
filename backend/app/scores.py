"""Points of a judgement: the one place that turns the score of a problem version into points of a contest (D-090).

A judgement keeps its score in the points of its problem version (`score` of `max_score`: the sum of the test groups,
or of the quiz questions). A contest problem may give the problem other points (`contest_problems.points`, e.g. 100
for a problem whose groups are worth 60); then the score is scaled. Everything that shows points - the admin views of
submissions, judgectl, the student's view and the leaderboard of 4b - calls contest_score / contest_max, never
divides by itself.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import ProblemType, ProblemVersion, TestGroup

CENT = Decimal("0.01")
QUIZ_ITEMS = {ProblemType.choice: "questions", ProblemType.text_answer: "answers"}


def contest_score(score: Optional[Decimal], max_score: Optional[Decimal], points: Optional[Decimal]) -> Decimal:
    """The points of a judgement in a contest. `points` of the contest problem absent: the score as it is. Otherwise
    score * points / max_score, rounded half up to cents; a judgement without a maximum (not finished: null, or a
    version worth nothing: 0) gives 0, never a division by zero."""
    score = Decimal(score or 0)
    if points is None:
        return score.quantize(CENT, ROUND_HALF_UP)
    if not max_score:
        return Decimal(0).quantize(CENT)
    return (score * Decimal(points) / Decimal(max_score)).quantize(CENT, ROUND_HALF_UP)


def contest_max(max_score: Optional[Decimal], points: Optional[Decimal]) -> Optional[Decimal]:
    """The most a problem gives in a contest: its points there, or the maximum of the version (null if unknown)."""
    if points is not None:
        return Decimal(points).quantize(CENT)
    return None if max_score is None else Decimal(max_score).quantize(CENT)


def shown(score: Optional[Decimal], max_score: Optional[Decimal], points: Optional[Decimal]) -> dict:
    """The fields every view of a judgement carries next to its raw score: max_score (of the version) and the points
    in the contest, both through the functions above."""
    return {"max_score": max_score, "contest_score": contest_score(score, max_score, points),
            "contest_max": contest_max(max_score, points)}


def quiz_max(version_type: ProblemType, config: dict) -> Decimal:
    """The maximum of a code-less version: the points of its questions (1 when not given), as runner/quiz.py adds them
    up. The migration of D-090 holds a copy of this rule in SQL; tests/pg/test_migrations_pg.py keeps them equal."""
    items = (config or {}).get(QUIZ_ITEMS[version_type]) or []
    return sum((Decimal(str(item.get("points", 1))) for item in items), Decimal(0)).quantize(CENT)


def version_max_score(db: Session, version_id: int) -> Decimal:
    """The maximum of a problem version: its quiz questions, or the sum of the points of its test groups."""
    version_type, config = db.execute(select(ProblemVersion.type, ProblemVersion.config)
                                      .where(ProblemVersion.id == version_id)).one()
    if version_type in QUIZ_ITEMS:
        return quiz_max(version_type, config)
    total = db.execute(select(func.coalesce(func.sum(TestGroup.points), 0))
                       .where(TestGroup.version_id == version_id)).scalar_one()
    return Decimal(total).quantize(CENT)
