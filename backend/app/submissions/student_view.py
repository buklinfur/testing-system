"""What a student sees of their own submission (4b.1, D-091): a pure function over the stored rows, no database, no
runner (the backend never imports it; the texts are protocol/verdicts.py).

What may be shown is decided now, at reading time, by effective_policy (D-094):
* which groups are open comes only from the version the judgement ran on - its own groups and tests, never matched
  by name with the groups of another version - and only while that version is still the one pinned to the contest
  problem: open there and open to the student now (contests/access.visible_tests: the open ones, every one after the
  end with tests_after_end). A judgement of an older version shows no group as open (a new version may have closed
  it, or renumbered the groups);
* the feedback level of a group is the stricter of its level in the judgement's version and the strictest level of
  the version pinned now (its problem level and every group override): a policy made stricter later
  (full -> verdict_only) hides the details of old submissions too.
test_results.visible_to_student, written by the worker when it judged, is not consulted.

* status: queued | judging (with progress) | done; a verdict, its text and hint only when done;
* points in the contest's points (scores.contest_score, D-090), also those of the groups;
* CE: the compile log (already stripped of server paths by the runner), cut to COMPILE_LOG_MAX;
* IE: the verdict and a general hint only - judgements.error never reaches a student (D-069);
* groups: shown when open (or open now) or with feedback full / groups_only;
* tests: of a group open now - every test with input, answer, the program's output and the checker's message; of a
  hidden group - with feedback full every test, with first_fail the first failed test of the submission, otherwise
  none, and never their content or checker message. Time and memory unless verdict_only; the hint of a runtime
  error only from the signal or the exit code (stderr is never shown: a traceback of a hidden test may print its
  data); the kind of a TL unless verdict_only;
* a problem without code: per question whether it was right, with the explanation only with feedback full, nothing
  with verdict_only.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Optional

from protocol.verdicts import SIGNAL_RU, TL_KIND_RU, VERDICT_HINT_RU, VERDICT_RU

from .. import scores

FEEDBACKS = ("full", "first_fail", "groups_only", "verdict_only")
DEFAULT_FEEDBACK = "first_fail"  # problem.yaml without `feedback` (runner/package.py)
COMPILE_LOG_MAX = 4000
EXCERPT_MAX = 4096
PASSED = ("OK", "SKIPPED")


def _v(value):
    return getattr(value, "value", value)


@dataclass(frozen=True)
class GroupPolicy:
    visibility: str  # open | hidden
    feedback: str  # one of FEEDBACKS


def policy_of(problem_feedback: Optional[str], groups: Iterable) -> dict:
    """{group name: GroupPolicy} of a problem version as written in it: `groups` are (name, visibility, feedback
    override or None)."""
    base = _v(problem_feedback) or DEFAULT_FEEDBACK
    return {name: GroupPolicy(_v(visibility), _v(feedback) or base) for name, visibility, feedback in groups}


def meet(a: str, b: str) -> str:
    """The stricter of two feedback levels: what both allow. full allows everything the other does; first_fail
    (one failed test, no hidden groups) and groups_only (hidden groups, no tests) share only what verdict_only shows."""
    if a == b or b == "full":
        return a
    if a == "full":
        return b
    return "verdict_only"


def strictest(levels: Iterable) -> str:
    out = "full"
    for level in levels:
        out = meet(out, _v(level) or DEFAULT_FEEDBACK)
    return out


def effective_feedback(judged_feedback: Optional[str], pinned_levels: Iterable) -> str:
    """The problem-level feedback to apply: the judgement's version's, capped by the version pinned now."""
    return meet(_v(judged_feedback) or DEFAULT_FEEDBACK, strictest(pinned_levels))


def effective_policy(judged_feedback: Optional[str], judged_groups: Iterable, pinned_levels: Iterable,
                     same_version: bool, open_now: Iterable[str]) -> dict:
    """{group name: GroupPolicy} for a judgement, by the rules of the module docstring.

    judged_feedback / judged_groups: the problem level and (name, visibility, override) of the groups of the version
    the judgement ran on; pinned_levels: the problem level and every group override of the version pinned now;
    same_version: the judgement ran on the version pinned now; open_now: access.visible_tests over judged_groups."""
    cap = strictest(pinned_levels)
    shown = set(open_now) if same_version else set()
    return {name: GroupPolicy("open" if name in shown else "hidden", meet(rule.feedback, cap))
            for name, rule in policy_of(judged_feedback, judged_groups).items()}


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _cut(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + "… (обрезано)"


def runtime_hint(exit_code: Optional[int], signal: Optional[int]) -> Optional[str]:
    if signal in SIGNAL_RU:
        return SIGNAL_RU[signal]
    if exit_code:
        return f"Программа завершилась с кодом {exit_code}. В C и C++ main должна возвращать 0."
    return None


def status_of(judgement) -> str:
    status = _v(_get(judgement, "status"))
    if status == "queued":
        return "queued"
    return "done" if status in ("finished", "internal_error") else "judging"


def student_view(judgement, *, results: Iterable = (), policy: Mapping = None, problem_feedback: Optional[str] = None,
                 contents: Mapping = None, points: Optional[Decimal] = None, counted: bool = True, late: bool = False,
                 rechecking: bool = False) -> dict:
    """The student's view of the current judgement of their submission.

    results: rows of test_results in order, each with `group` (name), `test_case_id`, `verdict`, `time_ms`,
    `memory_kb`, `exit_code`, `signal`, `tl_kind`, `checker_message`, `output_excerpt`; policy: effective_policy()
    (a group it does not know is hidden with problem_feedback); problem_feedback: effective_feedback(); contents:
    {test_case_id: {"input": str, "answer": str}} read by the caller for tests of open groups only (anything else in
    it is ignored); points: contest_problems.points."""
    policy = policy or {}
    base = _v(problem_feedback) or DEFAULT_FEEDBACK
    contents = contents or {}
    status = status_of(judgement)
    score, max_score = _get(judgement, "score"), _get(judgement, "max_score")
    view = {"status": status, "counted": counted, "late": late, "rechecking": rechecking,
            "score": scores.contest_score(score, max_score, points) if status == "done" else None,
            "max_score": scores.contest_max(max_score, points)}
    if status == "judging":
        view["progress"] = {"done": _get(judgement, "tests_done"), "total": _get(judgement, "tests_total")}
    if status != "done":
        return view
    verdict = _v(_get(judgement, "verdict")) or "IE"
    view.update(verdict=verdict, verdict_text=VERDICT_RU.get(verdict, verdict), hint=VERDICT_HINT_RU.get(verdict, ""))
    if verdict == "CE":
        view["compile_log"] = _cut(_get(judgement, "compile_log") or "", COMPILE_LOG_MAX)
        return view
    if verdict == "IE":
        return view

    summary = _get(judgement, "summary") or {}
    if "quiz" in summary:
        view["questions"] = _quiz(summary["quiz"], base)
        return view

    def rule(name) -> GroupPolicy:
        return policy.get(name) or GroupPolicy("hidden", base)

    groups = []
    for g in summary.get("groups") or []:
        p = rule(g.get("name"))
        if p.visibility == "open" or p.feedback in ("full", "groups_only"):
            groups.append({"name": g.get("name"),
                           "points": scores.contest_score(Decimal(str(g.get("points", 0))), max_score, points),
                           "score": scores.contest_score(Decimal(str(g.get("score", 0))), max_score, points)})
    view["groups"] = groups

    tests, failed_before = [], False
    for number, r in enumerate(results, 1):
        name, verdict_t = _get(r, "group"), _v(_get(r, "verdict"))
        p = rule(name)
        is_open = p.visibility == "open"
        show = is_open or p.feedback == "full" or (p.feedback == "first_fail" and verdict_t not in PASSED
                                                   and not failed_before)
        if verdict_t not in PASSED:
            failed_before = True
        if not show:
            continue
        entry = {"test_no": number, "group": name, "verdict": verdict_t}
        if p.feedback != "verdict_only":
            entry["time_ms"], entry["memory_kb"] = _get(r, "time_ms"), _get(r, "memory_kb")
            if verdict_t == "TL" and _get(r, "tl_kind") in TL_KIND_RU:
                entry["tl_kind"] = _get(r, "tl_kind")
                entry["time_limit_hint"] = TL_KIND_RU[entry["tl_kind"]]
        if verdict_t == "RE" and (is_open or p.feedback in ("full", "first_fail")):
            hint = runtime_hint(_get(r, "exit_code"), _get(r, "signal"))
            if hint:
                entry["runtime_hint"] = hint
        if is_open:
            content = contents.get(_get(r, "test_case_id")) or {}
            entry["input"] = _cut(content.get("input"), EXCERPT_MAX)
            entry["answer"] = _cut(content.get("answer"), EXCERPT_MAX)
            entry["output"] = _cut(_get(r, "output_excerpt"), EXCERPT_MAX)
            if _get(r, "checker_message"):
                entry["checker_message"] = _cut(_get(r, "checker_message"), EXCERPT_MAX)
        tests.append(entry)
    view["tests"] = tests
    return view


def _quiz(graded: dict, feedback: str) -> list:
    if feedback == "verdict_only":
        return []
    out = []
    for d in graded.get("details") or []:
        item = {"id": d.get("id"), "correct": bool(d.get("correct"))}
        if feedback == "full" and d.get("explanation"):
            item["explanation"] = d["explanation"]
        out.append(item)
    return out
