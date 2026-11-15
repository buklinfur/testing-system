"""Human-readable explanations of verdicts and the student-facing view of a report."""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from protocol.verdicts import SIGNAL_RU, TL_KIND_RU, VERDICT_HINT_RU, VERDICT_RU  # noqa: F401

from .common import Verdict
from .report import JudgeReport, TestReport

_PATHS = re.compile(r"(/box/|/tmp/[^\s:]*/|/usr/[^\s:]*/|\./)")


def sanitize_compile_log(text: str, limit: int = 4000) -> str:
    """Remove server paths from compiler output and cut it to a sensible size."""
    text = _PATHS.sub("", text.replace("\r", ""))
    if len(text) > limit:
        text = text[:limit] + "\n... (вывод обрезан)"
    return text.strip()


def explain_runtime_error(language: str, stderr: str, exit_code: Optional[int], signal: Optional[int]) -> str:
    if language == "python" and "Traceback" in stderr:
        lines = [ln for ln in stderr.strip().splitlines() if ln.strip()]
        error = lines[-1] if lines else ""
        line_no = None
        for ln in lines:
            match = re.search(r'File ".*?", line (\d+)', ln)
            if match:
                line_no = match.group(1)
        where = f"строка {line_no}: " if line_no else ""
        return f"{where}{error}"
    if signal in SIGNAL_RU:
        return SIGNAL_RU[signal]
    if exit_code:
        return f"Программа завершилась с кодом {exit_code}. В C и C++ main должна возвращать 0."
    return ""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "… (обрезано)"


def student_view(report: JudgeReport, problem, language: str = "", excerpt_cap: int = 4096) -> dict:
    """What a student may see. Teachers use the full report instead.

    Rules (see docs/PROBLEM_FORMAT.md, section 7):
      * open group      -> every test is shown completely (input, answer, own output);
      * hidden group    -> content is never shown; details depend on the feedback policy;
      * checker messages of hidden tests are never shown (they may reveal the answer).
    """
    verdict = report.verdict.value
    view = {
        "verdict": verdict,
        "verdict_text": VERDICT_RU[verdict],
        "hint": VERDICT_HINT_RU.get(verdict, ""),
        "score": float(report.score),
        "max_score": float(report.max_score),
    }
    if report.verdict == Verdict.CE:
        view["compile_log"] = report.compile_log
        return view
    if report.verdict == Verdict.IE:
        return view

    by_name = {g.name: g for g in problem.groups}
    groups_out, tests_out = [], []
    first_fail_done = False
    for group in report.groups:
        spec = by_name[group.name]
        feedback = group.feedback
        if group.visibility == "open" or feedback in ("full", "groups_only"):
            groups_out.append({"name": group.name, "points": float(group.points), "score": float(group.score)})
        by_file = {t.name: t for t in spec.tests}
        for test in group.tests:
            entry = {"test_no": test.index, "group": group.name, "verdict": test.verdict.value}
            show = False
            if group.visibility == "open":
                show = True
                src = by_file[test.name]
                entry["input"] = _cut(src.input_path.read_text(errors="replace"), excerpt_cap)
                if src.answer_path:
                    entry["answer"] = _cut(src.answer_path.read_text(errors="replace"), excerpt_cap)
                entry["output"] = _cut(test.output_excerpt, excerpt_cap)
                if test.message:
                    entry["checker_message"] = test.message
            elif feedback == "full":
                show = True
            elif feedback == "first_fail":
                if test.verdict not in (Verdict.OK, Verdict.SKIPPED) and not first_fail_done:
                    show = True
            if show:
                if feedback != "verdict_only":
                    entry["time_ms"], entry["memory_kb"] = test.time_ms, test.memory_kb
                allow_hint = group.visibility == "open" or feedback in ("full", "first_fail")
                if test.verdict == Verdict.RE and allow_hint:
                    hint = explain_runtime_error(language, test.stderr_excerpt, test.exit_code, test.signal)
                    if hint:
                        entry["runtime_hint"] = hint
                if test.verdict == Verdict.TL and test.tl_kind in TL_KIND_RU and feedback != "verdict_only":
                    entry["tl_kind"] = test.tl_kind
                    entry["time_limit_hint"] = TL_KIND_RU[test.tl_kind]
                tests_out.append(entry)
            if test.verdict not in (Verdict.OK, Verdict.SKIPPED):
                first_fail_done = True
    view["groups"] = groups_out
    view["tests"] = tests_out
    return view
