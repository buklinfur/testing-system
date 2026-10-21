from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from .common import Verdict

CENT = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def group_score(scoring: str, points: Decimal, verdicts: Sequence[Verdict]) -> Decimal:
    """Score of one group. SKIPPED tests count as not passed.

    sum            points are split evenly between the tests, a share per passed test
    all_or_nothing full points only if every test passed
    min            reserved for partial-score checkers; behaves as all_or_nothing in format v1
    """
    total = len(verdicts)
    if total == 0:
        return Decimal(0)
    passed = sum(1 for v in verdicts if v == Verdict.OK)
    if scoring == "sum":
        return _q(points * passed / total)
    if scoring in ("all_or_nothing", "min"):
        return _q(points) if passed == total else Decimal(0)
    raise ValueError(f"unknown scoring: {scoring!r}")


def failed_verdicts(verdicts: Sequence[Verdict]) -> list:
    return [v for v in verdicts if v not in (Verdict.OK, Verdict.SKIPPED)]


def first_fail_verdict(verdicts: Sequence[Verdict]) -> Verdict:
    """Verdict of the first failed test, OK if nothing failed (used by `solutions[].expect`)."""
    failed = failed_verdicts(verdicts)
    return failed[0] if failed else Verdict.OK


def submission_verdict(verdicts: Sequence[Verdict], score: Decimal) -> Verdict:
    """OK: every test passed. PARTIAL: some points but not everything. Otherwise the first failure."""
    failed = failed_verdicts(verdicts)
    if not failed:
        return Verdict.OK
    return Verdict.PARTIAL if score > 0 else failed[0]
