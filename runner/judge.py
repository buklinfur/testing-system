"""Orchestration: compile a submission, run it on every test, check, score."""
from __future__ import annotations

import os
import threading
from decimal import Decimal
from typing import Optional

from .archive import SubmissionError, extract_submission
from .checkers import run_builtin
from .common import Limits, RunResult, SandboxError, Verdict
from .compile import Program, compile_program
from .config import RunnerConfig
from .jurycache import JuryCache, MemoryJuryCache, jury_key
from .languages import EXECUTABLE_NAME, get_profile, guess_language
from .package import Problem, TestFile
from .report import GroupReport, JudgeReport, TestReport
from .sandbox import make_factory
from .scoring import first_fail_verdict, group_score, submission_verdict

PROTOCOL_VERDICT = {0: Verdict.OK, 1: Verdict.WA, 2: Verdict.PE}


class JudgeError(Exception):
    """Internal failure (jury program broken, sandbox problem). Becomes verdict IE."""


class JudgeCancelled(Exception):
    """The caller asked to stop (lease lost, shutdown). Not a verdict: no report is produced."""


def _decode(data: bytes, cap: int) -> str:
    return data[:cap].decode("utf-8", errors="replace")


class Judge:
    def __init__(self, config: Optional[RunnerConfig] = None, factory=None,
                 jury_cache: Optional[JuryCache] = None) -> None:
        self.config = config or RunnerConfig()
        self.factory = factory or make_factory(self.config)
        self._jury_cache: JuryCache = jury_cache if jury_cache is not None else MemoryJuryCache()
        self._jury_local: dict = {}  # per-process layer: a disk cache is read once per binary, not once per test

    # ------------------------------------------------------------------ public
    def judge_submission(self, problem: Problem, language: str, data: bytes, is_archive: bool = False,
                         entry: Optional[str] = None, cancel=None, progress=None) -> JudgeReport:
        """cancel() -> bool is polled between steps (raises JudgeCancelled);
        progress(phase, done, total) reports "compiling" / "running" and finished tests."""
        profile = get_profile(language)
        try:
            files = extract_submission(data, is_archive, profile, self.config)
        except SubmissionError as exc:
            return JudgeReport(Verdict.CE, max_score=problem.max_score(), first_fail_verdict=Verdict.CE,
                               compile_log=str(exc))
        return self.judge(problem, language, files, entry, cancel=cancel, progress=progress)

    def judge(self, problem: Problem, language: str, files: dict, entry: Optional[str] = None,
              cancel=None, progress=None) -> JudgeReport:
        if problem.type not in ("batch", "interactive"):
            raise NotImplementedError(f"problem type {problem.type!r} is not supported by the runner yet")
        profile = get_profile(language)
        if problem.languages and language not in problem.languages:
            raise ValueError(f"language {language!r} is not allowed for this problem")
        max_score = problem.max_score()
        total = sum(len(g.tests) for g in problem.groups)
        self._tick(cancel, progress, "compiling", 0, total)
        try:
            outcome = compile_program(self.factory, profile, files, entry or problem.submission.get("entry"), self.config)
            if outcome.verdict == Verdict.IE:
                raise JudgeError(outcome.log)
            if not outcome.ok:
                return JudgeReport(Verdict.CE, max_score=max_score, first_fail_verdict=Verdict.CE, compile_log=outcome.log)
            self._tick(cancel, progress, "running", 0, total)
            groups = self._run_groups(problem, outcome.program, self._limits(problem, profile), language,
                                      cancel=cancel, progress=progress, total=total)
        except (JudgeError, SandboxError) as exc:
            return JudgeReport(Verdict.IE, max_score=max_score, first_fail_verdict=Verdict.IE, error=str(exc))
        verdicts = [t.verdict for g in groups for t in g.tests]
        score = sum((g.score for g in groups), Decimal(0))
        return JudgeReport(
            verdict=submission_verdict(verdicts, score),
            score=score,
            max_score=max_score,
            first_fail_verdict=first_fail_verdict(verdicts),
            groups=groups,
        )

    def selfcheck(self, problem: Problem) -> list:
        """Run the problem's reference solutions and compare with the expected verdicts."""
        results = []
        for item in problem.solutions:
            path = problem.root / item["path"]
            language = item.get("language") or guess_language(item["path"])
            report = self.judge(problem, language, {get_profile(language).default_source: path.read_bytes()})
            got = report.verdict if report.verdict in (Verdict.CE, Verdict.IE) else report.first_fail_verdict
            results.append({
                "path": item["path"], "expected": item["expect"], "got": got.value,
                "ok": got.value == item["expect"], "score": float(report.score), "error": report.error,
            })
        return results

    # ------------------------------------------------------------------ limits
    def _limits(self, problem: Problem, profile) -> Limits:
        c = self.config
        extra = problem.per_language.get(profile.key, {})
        t_mult = profile.time_multiplier * extra.get("time_multiplier", 1.0)
        m_mult = profile.memory_multiplier * extra.get("memory_multiplier", 1.0)
        if problem.time_ms is None:
            time_ms, wall_ms = c.time_ms_cap, c.wall_ms_cap
        else:
            time_ms = min(c.time_ms_cap, int(problem.time_ms * t_mult))
            wall_ms = min(c.wall_ms_cap, time_ms * 2 + 1000)
        memory = c.memory_mb_cap if problem.memory_mb is None else min(c.memory_mb_cap, int(problem.memory_mb * m_mult))
        out_kb = c.output_kb_cap if problem.output_kb is None else min(c.output_kb_cap, problem.output_kb)
        return Limits(time_ms=time_ms, wall_ms=wall_ms, memory_mb=memory, processes=c.processes, fsize_kb=out_kb)

    # ------------------------------------------------------------------ groups
    @staticmethod
    def _tick(cancel, progress, phase: str, done: int, total: int) -> None:
        if cancel is not None and cancel():
            raise JudgeCancelled()
        if progress is not None:
            progress(phase, done, total)

    def _run_groups(self, problem: Problem, program: Program, limits: Limits, language: str,
                    cancel=None, progress=None, total: int = 0) -> list:
        reports, passed, index = [], set(), 0
        for group in problem.groups:
            tests, verdicts = [], []
            stop = any(dep not in passed for dep in group.depends_on)
            for test in group.tests:
                index += 1
                if stop:
                    report = TestReport(group.name, test.name, index, Verdict.SKIPPED)
                else:
                    if cancel is not None and cancel():
                        raise JudgeCancelled()
                    report = self._run_test(problem, program, test, limits, index)
                    if report.verdict == Verdict.IE:
                        raise JudgeError(report.message or "internal error")
                    if (report.verdict != Verdict.OK and group.scoring in ("all_or_nothing", "min")
                            and not self.config.run_all_tests):
                        stop = True
                tests.append(report)
                verdicts.append(report.verdict)
                if progress is not None:
                    progress("running", index, total)
            if verdicts and all(v == Verdict.OK for v in verdicts):
                passed.add(group.name)
            reports.append(GroupReport(
                name=group.name, points=group.points, score=group_score(group.scoring, group.points, verdicts),
                visibility=group.visibility, feedback=problem.group_feedback(group), scoring=group.scoring, tests=tests,
            ))
        return reports

    def _run_test(self, problem, program, test, limits, index) -> TestReport:
        if problem.type == "interactive":
            return self._run_interactive(problem, program, test, limits, index)
        return self._run_batch(problem, program, test, limits, index)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _load(box, program: Program) -> None:
        for name, data in program.files.items():
            box.put_file(name, data, executable=name in program.executables)

    def _base_report(self, test: TestFile, index: int, run: RunResult) -> TestReport:
        return TestReport(
            group=test.group, name=test.name, index=index, verdict=run.verdict,
            time_ms=run.time_ms, memory_kb=run.memory_kb, exit_code=run.exit_code, signal=run.signal,
            message=run.message if run.verdict == Verdict.IE else "",
            stderr_excerpt=_decode(run.stderr, 2048),
        )

    def _jury_exe(self, path, language: str) -> bytes:
        source = path.read_bytes()
        profile = get_profile(language)
        if profile.compile is None:
            raise JudgeError("jury programs must be written in a compiled language (c or cpp)")
        key = jury_key(source, language, profile.compile, self.config.toolchain_id)
        exe = self._jury_local.get(key)
        if exe is not None:
            return exe
        exe = self._jury_cache.get(key)
        if exe is None:
            outcome = compile_program(self.factory, profile, {profile.default_source: source}, None, self.config, slot=1)
            if not outcome.ok:
                raise JudgeError(f"cannot compile {path.name}: {outcome.log}")
            exe = outcome.program.files[EXECUTABLE_NAME]
            self._jury_cache.put(key, exe)
        self._jury_local[key] = exe
        return exe

    # ------------------------------------------------------------------ batch
    def _run_batch(self, problem, program, test, limits, index) -> TestReport:
        cap = (limits.fsize_kb or self.config.output_kb_cap) * 1024
        data = test.input_path.read_bytes()
        with self.factory(0) as box:
            self._load(box, program)
            stdin_file = None
            if problem.io_mode == "files":
                box.put_file(problem.io_input, data)
            else:
                box.put_file("__input", data)
                stdin_file = "__input"
            run = box.run(program.argv, limits, stdin_file=stdin_file, env=program.env,
                          stdout_cap=cap, stderr_cap=self.config.stderr_cap_bytes)
            output = run.stdout
            missing_output = False
            if problem.io_mode == "files" and run.verdict == Verdict.OK:
                produced = box.read_file(problem.io_output, cap)
                missing_output = produced is None
                output = produced or b""
        report = self._base_report(test, index, run)
        if run.verdict != Verdict.OK:
            report.output_excerpt = _decode(output, self.config.excerpt_cap_bytes)
            return report
        report.output_excerpt = _decode(output, self.config.excerpt_cap_bytes)
        if missing_output:
            report.verdict, report.message = Verdict.WA, f"файл {problem.io_output} не создан"
            return report
        report.verdict, report.message = self._check(problem, test, data, output)
        return report

    def _check(self, problem, test, input_bytes: bytes, output: bytes):
        kind = problem.checker_type
        answer = test.answer_path.read_bytes() if test.answer_path else b""
        if kind in ("exact", "tokens", "float"):
            if test.answer_path is None:
                raise JudgeError(f"нет файла ответа для теста {test.name}")
            result = run_builtin(kind, output, answer, problem.checker_eps)
            return result.verdict, result.message
        if kind == "custom":
            return self._run_custom_checker(problem, input_bytes, output, answer)
        raise JudgeError(f"unsupported checker type: {kind!r}")

    def _run_custom_checker(self, problem, input_bytes, output, answer):
        exe = self._jury_exe(problem.checker_path, problem.checker_language)
        c = self.config
        limits = Limits(time_ms=c.jury_time_ms, wall_ms=c.jury_time_ms * 3, memory_mb=c.jury_memory_mb,
                        processes=8, fsize_kb=1024)
        with self.factory(1) as box:
            box.put_file("checker", exe, executable=True)
            box.put_file("input.txt", input_bytes)
            box.put_file("output.txt", output)
            box.put_file("answer.txt", answer)
            run = box.run(["./checker", "input.txt", "output.txt", "answer.txt"], limits,
                          stdout_cap=4096, stderr_cap=4096)
        message = _decode(run.stderr, c.message_cap_bytes).strip()
        if run.verdict in (Verdict.OK, Verdict.RE) and run.signal is None and run.exit_code in PROTOCOL_VERDICT:
            return PROTOCOL_VERDICT[run.exit_code], message
        raise JudgeError(f"checker failed ({run.verdict.value}, code {run.exit_code}): {message}")

    # ------------------------------------------------------------------ interactive
    def _run_interactive(self, problem, program, test, limits, index) -> TestReport:
        c = self.config
        iexe = self._jury_exe(problem.interactor_path, problem.interactor_language)
        ilimits = Limits(time_ms=c.jury_time_ms, wall_ms=(limits.wall_ms or c.wall_ms_cap) + 5000,
                         memory_mb=c.jury_memory_mb, processes=8, fsize_kb=1024)
        with self.factory(0) as pbox, self.factory(1) as ibox:
            self._load(pbox, program)
            ibox.put_file("interactor", iexe, executable=True)
            ibox.put_file("input.txt", test.input_path.read_bytes())
            args = ["./interactor", "input.txt"]
            if test.answer_path:
                ibox.put_file("answer.txt", test.answer_path.read_bytes())
                args.append("answer.txt")
            p_read, i_write = os.pipe()  # interactor stdout -> solution stdin
            i_read, p_write = os.pipe()  # solution stdout -> interactor stdin
            try:
                p_handle = pbox.start(program.argv, limits, stdin_fd=p_read, stdout_fd=p_write,
                                      env=program.env, stderr_cap=c.stderr_cap_bytes)
                i_handle = ibox.start(args, ilimits, stdin_fd=i_read, stdout_fd=i_write, stderr_cap=4096)
            finally:
                for fd in (p_read, i_write, i_read, p_write):
                    os.close(fd)
            results: dict = {}
            threads = [threading.Thread(target=lambda k=k, h=h: results.__setitem__(k, h.wait()))
                       for k, h in (("p", p_handle), ("i", i_handle))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        p, i = results["p"], results["i"]
        report = self._base_report(test, index, p)
        report.verdict, report.message = self._resolve_interactive(p, i)
        return report

    def _resolve_interactive(self, p: RunResult, i: RunResult):
        """Combine the solution's and the interactor's outcomes into one verdict."""
        message = _decode(i.stderr, self.config.message_cap_bytes).strip()
        if p.verdict in (Verdict.TL, Verdict.ML, Verdict.OL, Verdict.IE):
            return p.verdict, ""
        protocol = i.verdict in (Verdict.OK, Verdict.RE) and i.signal is None and i.exit_code in PROTOCOL_VERDICT
        if not protocol:
            raise JudgeError(f"interactor failed ({i.verdict.value}, code {i.exit_code}): {message}")
        if i.exit_code in (1, 2):
            # whoever failed first is the cause; the other one only reacts to the closed pipe
            if p.verdict == Verdict.RE and p.ended_at < i.ended_at:
                return Verdict.RE, ""
            return PROTOCOL_VERDICT[i.exit_code], message
        if p.verdict == Verdict.RE:
            return Verdict.RE, ""
        return Verdict.OK, message
