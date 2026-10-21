"""Built-in checkers. They run in the judge process on untrusted text, so they are deliberately simple."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .common import Verdict


@dataclass
class CheckResult:
    verdict: Verdict
    message: str = ""


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def check_exact(output: bytes, answer: bytes) -> CheckResult:
    def lines(data: bytes) -> list:
        rows = [row.rstrip(" \t\r") for row in _text(data).split("\n")]
        while rows and rows[-1] == "":
            rows.pop()
        return rows

    got, want = lines(output), lines(answer)
    for number, (a, b) in enumerate(zip(got, want), 1):
        if a != b:
            return CheckResult(Verdict.WA, f"строки различаются, строка {number}")
    if len(got) != len(want):
        return CheckResult(Verdict.WA, f"разное число строк: {len(got)} вместо {len(want)}")
    return CheckResult(Verdict.OK)


def check_tokens(output: bytes, answer: bytes) -> CheckResult:
    got, want = _text(output).split(), _text(answer).split()
    for number, (a, b) in enumerate(zip(got, want), 1):
        if a != b:
            return CheckResult(Verdict.WA, f"токены различаются, токен {number}")
    if len(got) != len(want):
        return CheckResult(Verdict.WA, f"разное число токенов: {len(got)} вместо {len(want)}")
    return CheckResult(Verdict.OK)


def _close(a: str, b: str, eps: float) -> bool:
    try:
        x, y = float(a), float(b)
    except ValueError:
        return a == b
    if x != x or y != y:  # NaN never matches
        return False
    return abs(x - y) <= eps * max(1.0, abs(y))


def check_float(output: bytes, answer: bytes, eps: float) -> CheckResult:
    got, want = _text(output).split(), _text(answer).split()
    for number, (a, b) in enumerate(zip(got, want), 1):
        if not _close(a, b, eps):
            return CheckResult(Verdict.WA, f"числа различаются, токен {number}")
    if len(got) != len(want):
        return CheckResult(Verdict.WA, f"разное число токенов: {len(got)} вместо {len(want)}")
    return CheckResult(Verdict.OK)


def run_builtin(kind: str, output: bytes, answer: bytes, eps: Optional[float] = None) -> CheckResult:
    if kind == "exact":
        return check_exact(output, answer)
    if kind == "tokens":
        return check_tokens(output, answer)
    if kind == "float":
        return check_float(output, answer, eps or 1e-9)
    raise ValueError(f"not a built-in checker: {kind!r}")
