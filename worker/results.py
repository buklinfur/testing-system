"""Build the result payload that goes to the backend (docs/WORKER_PROTOCOL.md, "result")."""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from runner.common import Verdict
from runner.explain import student_view
from runner.quiz import grade_choice, grade_text
from runner.report import JudgeReport

log = logging.getLogger("judge.results")
MAX_ERROR_CHARS = 2000


def visible_test_indexes(report: JudgeReport, problem, language: str) -> set:
    """Tests the student may see details of, decided by the runner's own student_view (one source of truth)."""
    try:
        view = student_view(report, problem, language)
    except Exception:  # noqa: BLE001 - never lose a result because of the presentation layer
        log.exception("student_view failed; hiding all test details")
        return set()
    return {entry["test_no"] for entry in view.get("tests", [])}


def code_result(report: JudgeReport, problem, language: str, test_ids: list) -> dict:
    visible = visible_test_indexes(report, problem, language)
    tests = []
    for t in report.tests:
        tests.append({
            "test_case_id": test_ids[t.index - 1], "index": t.index, "verdict": t.verdict.value,
            "time_ms": t.time_ms, "memory_kb": t.memory_kb, "exit_code": t.exit_code, "signal": t.signal,
            "message": t.message, "stderr_excerpt": t.stderr_excerpt, "output_excerpt": t.output_excerpt,
            "visible_to_student": t.index in visible,
        })
    return {
        "verdict": report.verdict.value, "score": str(report.score), "max_score": str(report.max_score),
        "compile_log": report.compile_log or None, "error": (report.error or None) and report.error[:MAX_ERROR_CHARS],
        "summary": {
            "first_fail": report.first_fail_verdict.value,
            "groups": [{"name": g.name, "points": str(g.points), "score": str(g.score),
                        "scoring": g.scoring, "visibility": g.visibility} for g in report.groups],
        },
        "tests": tests,
    }


def quiz_result(problem, answers: Optional[dict]) -> dict:
    graded = grade_choice(problem, answers or {}) if problem.type == "choice" else grade_text(problem, answers or {})
    score, maximum = Decimal(str(graded["score"])), Decimal(str(graded["max_score"]))
    if maximum > 0 and score >= maximum:
        verdict = Verdict.OK
    else:
        verdict = Verdict.PARTIAL if score > 0 else Verdict.WA
    return {"verdict": verdict.value, "score": str(score), "max_score": str(maximum), "compile_log": None,
            "error": None, "summary": {"quiz": graded}, "tests": []}


def internal_error_result(message: str) -> dict:
    return {"verdict": Verdict.IE.value, "score": "0", "max_score": "0", "compile_log": None,
            "error": message[:MAX_ERROR_CHARS], "summary": {}, "tests": []}
