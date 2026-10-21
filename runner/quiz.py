"""Grading of problems without code: type choice and text_answer."""
from __future__ import annotations

import re
from decimal import Decimal

CENT = Decimal("0.01")


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def grade_choice(problem, selected: dict) -> dict:
    """selected: {question id: [option ids]}. A question counts only if the sets are equal."""
    score, total, details = Decimal(0), Decimal(0), []
    for q in problem.questions:
        points = Decimal(str(q.get("points", 1)))
        total += points
        got = set(selected.get(q["id"], []))
        ok = got == set(q["correct"])
        if ok:
            score += points
        details.append({"id": q["id"], "correct": ok, "explanation": q.get("explanation", "")})
    return {"score": float(score.quantize(CENT)), "max_score": float(total.quantize(CENT)), "details": details}


def _matches(answer: dict, given: str) -> bool:
    mode = answer.get("match", "normalized")
    accepted = answer["accept"]
    if mode == "exact":
        return given in accepted
    if mode == "normalized":
        return _normalize(given) in {_normalize(a) for a in accepted}
    if mode == "regex":
        return any(re.fullmatch(a, given.strip()) for a in accepted)
    if mode == "number":
        eps = float(answer.get("eps", 0))
        try:
            value = float(given.strip().replace(",", "."))
        except ValueError:
            return False
        return any(abs(value - float(a)) <= eps for a in accepted)
    raise ValueError(f"unknown match mode: {mode!r}")


def grade_text(problem, given: dict) -> dict:
    score, total, details = Decimal(0), Decimal(0), []
    for a in problem.answers:
        points = Decimal(str(a.get("points", 1)))
        total += points
        ok = _matches(a, given.get(a["id"], ""))
        if ok:
            score += points
        details.append({"id": a["id"], "correct": ok, "explanation": a.get("explanation", "")})
    return {"score": float(score.quantize(CENT)), "max_score": float(total.quantize(CENT)), "details": details}
