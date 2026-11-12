"""End-to-end tests on the UNSAFE local backend (needs g++/gcc/python3). They exercise the whole judging
logic; the isolate backend is covered by `python -m runner.cli sandbox-check` and `redteam` on the real machine."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runner.common import Verdict as V  # noqa: E402
from runner.config import RunnerConfig  # noqa: E402
from runner.explain import student_view  # noqa: E402
from runner.judge import Judge  # noqa: E402
from runner.package import Problem  # noqa: E402
from runner.quiz import grade_choice, grade_text  # noqa: E402
from tests.unsafe_local import child_env, enable_for_module  # noqa: E402


def setUpModule():
    enable_for_module()  # JUDGE_ALLOW_UNSAFE_LOCAL / JUDGE_DEV for this module's tests only (tests/unsafe_local.py)

EXAMPLES = ROOT / "problems-examples"


def judge(**overrides) -> Judge:
    return Judge(RunnerConfig(backend="local", **overrides))


def copy_problem(name: str, tmp: str, edit=None) -> Problem:
    dst = Path(tmp) / name
    shutil.copytree(EXAMPLES / name, dst)
    if edit:
        data = yaml.safe_load((dst / "problem.yaml").read_text(encoding="utf-8"))
        edit(data)
        (dst / "problem.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return Problem.load(dst)


def codes(report):
    return [(g.name, g.score, [t.verdict.value for t in g.tests]) for g in report.groups]


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.problem = Problem.load(EXAMPLES / "sum-of-two")
        self.judge = judge()

    def run_py(self, source, **kw):
        return self.judge.judge_submission(self.problem, "python", source.encode(), **kw)

    def test_correct_solution_gets_full_score(self):
        report = self.run_py("a, b = map(int, input().split())\nprint(a + b)\n")
        self.assertEqual((report.verdict, report.score, report.max_score), (V.OK, Decimal(100), Decimal(100)))

    def test_failed_dependency_skips_the_dependent_group(self):
        report = self.run_py("print(0)\n")
        groups = {g.name: g for g in report.groups}
        self.assertTrue(all(t.verdict == V.SKIPPED for t in groups["large"].tests))
        self.assertEqual(groups["large"].score, Decimal(0))
        self.assertEqual(report.first_fail_verdict, V.WA)

    def test_all_or_nothing_group_stops_at_first_failure(self):
        # passes the small tests, fails the large ones
        source = "a, b = map(int, input().split())\nprint(a + b if abs(a) < 10**4 else 0)\n"
        report = self.run_py(source)
        large = [g for g in report.groups if g.name == "large"][0]
        self.assertEqual([t.verdict for t in large.tests], [V.WA] + [V.SKIPPED] * 4)
        self.assertEqual(report.verdict, V.PARTIAL)
        self.assertEqual(report.score, Decimal("40.00"))

    def test_run_all_tests_option_runs_every_test(self):
        source = "a, b = map(int, input().split())\nprint(a + b if abs(a) < 10**4 else 0)\n"
        report = judge(run_all_tests=True).judge_submission(self.problem, "python", source.encode())
        large = [g for g in report.groups if g.name == "large"][0]
        self.assertNotIn(V.SKIPPED, [t.verdict for t in large.tests])

    def test_compile_error_is_sanitised(self):
        report = self.judge.judge_submission(self.problem, "cpp", b"int main( { return 0 }\n")
        self.assertEqual(report.verdict, V.CE)
        self.assertIn("main.cpp", report.compile_log)
        self.assertNotIn("localbox-", report.compile_log)

    def test_python_syntax_error_is_compile_error(self):
        report = self.run_py("print(1\n")
        self.assertEqual(report.verdict, V.CE)
        self.assertIn("SyntaxError", report.compile_log)

    def test_runtime_error_and_hint(self):
        report = self.run_py("print(1 // 0)\n")
        first = report.tests[0]
        self.assertEqual(first.verdict, V.RE)
        view = student_view(report, self.problem, "python")
        self.assertIn("ZeroDivisionError", view["tests"][0].get("runtime_hint", "") + str(view))

    def test_time_limit(self):
        problem = copy_problem("sum-of-two", tempfile.mkdtemp(), lambda d: d["limits"].update(time_ms=200, per_language={}))
        report = judge().judge_submission(problem, "python", b"while True:\n    pass\n")
        self.assertEqual(report.first_fail_verdict, V.TL)

    def test_language_not_allowed(self):
        problem = copy_problem("sum-of-two", tempfile.mkdtemp(), lambda d: d.update(languages=["python"]))
        with self.assertRaises(ValueError):
            judge().judge_submission(problem, "cpp", b"int main(){}")

    def test_multi_file_cpp_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("main.cpp", '#include <iostream>\n#include "add.h"\nint main(){ long long a,b; std::cin>>a>>b; std::cout<<add(a,b); }\n')
            z.writestr("add.h", "long long add(long long a, long long b);\n")
            z.writestr("add.cpp", '#include "add.h"\nlong long add(long long a, long long b){ return a + b; }\n')
        report = self.judge.judge_submission(self.problem, "cpp", buf.getvalue(), is_archive=True)
        self.assertEqual((report.verdict, report.score), (V.OK, Decimal(100)))

    def test_bad_archive_is_compile_error(self):
        report = self.judge.judge_submission(self.problem, "cpp", b"garbage", is_archive=True)
        self.assertEqual(report.verdict, V.CE)

    def test_file_io_mode(self):
        def edit(d):
            d["io"] = {"mode": "files", "input": "input.txt", "output": "output.txt"}
        problem = copy_problem("sum-of-two", tempfile.mkdtemp(), edit)
        source = "a, b = map(int, open('input.txt').read().split())\nopen('output.txt', 'w').write(str(a + b))\n"
        self.assertEqual(judge().judge_submission(problem, "python", source.encode()).verdict, V.OK)
        # a program that never creates the output file gets WA, not an internal error
        report = judge().judge_submission(problem, "python", b"pass\n")
        self.assertEqual(report.first_fail_verdict, V.WA)


class StudentViewTests(unittest.TestCase):
    def test_hidden_tests_never_leak_content(self):
        problem = Problem.load(EXAMPLES / "sum-of-two")
        source = b"a, b = map(int, input().split())\nprint(a + b if abs(a) < 10**4 else 0)\n"
        report = judge().judge_submission(problem, "python", source)
        view = student_view(report, problem, "python")
        self.assertEqual(view["verdict"], "PARTIAL")
        samples = [t for t in view["tests"] if t["group"] == "samples"]
        self.assertTrue(all("input" in t and "answer" in t for t in samples))  # open group
        hidden = [t for t in view["tests"] if t["group"] != "samples"]
        self.assertEqual(len(hidden), 1)  # first_fail: just the first failing test
        for t in hidden:
            self.assertNotIn("input", t)
            self.assertNotIn("answer", t)
            self.assertNotIn("output", t)
            self.assertNotIn("checker_message", t)
        self.assertNotIn("groups", {k for k in view if k == "groups" and False})

    def test_verdict_only_hides_everything_hidden(self):
        problem = copy_problem("sum-of-two", tempfile.mkdtemp(), lambda d: d.update(feedback="verdict_only"))
        report = judge().judge_submission(problem, "python", b"print(0)\n")
        view = student_view(report, problem, "python")
        self.assertEqual([t["group"] for t in view["tests"]], ["samples", "samples"])

    def test_compile_error_view(self):
        problem = Problem.load(EXAMPLES / "sum-of-two")
        report = judge().judge_submission(problem, "cpp", b"oops")
        view = student_view(report, problem, "cpp")
        self.assertEqual((view["verdict"], "compile_log" in view), ("CE", True))


class CustomCheckerTests(unittest.TestCase):
    def test_any_valid_answer_is_accepted(self):
        problem = Problem.load(EXAMPLES / "pair-with-sum")
        j = judge()
        good = j.judge_submission(problem, "python", b"n = int(input())\nprint(n - 5, 5)\n")
        self.assertEqual((good.verdict, good.score), (V.OK, Decimal(100)))
        bad = j.judge_submission(problem, "python", b"n = int(input())\nprint(n, 1)\n")
        self.assertEqual(bad.first_fail_verdict, V.WA)

    def test_presentation_error_from_checker(self):
        problem = Problem.load(EXAMPLES / "pair-with-sum")
        report = judge().judge_submission(problem, "python", b"print('hello')\n")
        self.assertEqual(report.first_fail_verdict, V.PE)


class InteractiveTests(unittest.TestCase):
    def problem(self):
        def edit(d):
            d["limits"] = {"time_ms": 300, "memory_mb": 256}
        return copy_problem("guess-number", tempfile.mkdtemp(), edit)

    def test_binary_search_passes(self):
        report = judge().judge_submission(self.problem(), "python", (EXAMPLES / "guess-number/solutions/ok.py").read_bytes())
        self.assertEqual((report.verdict, report.score), (V.OK, Decimal(100)))

    def test_too_many_queries_is_wa(self):
        source = (EXAMPLES / "guess-number/solutions/wa_linear.py").read_bytes()
        self.assertEqual(judge().judge_submission(self.problem(), "python", source).first_fail_verdict, V.WA)

    def test_crashing_solution_is_re(self):
        report = judge().judge_submission(self.problem(), "python", b"input()\nraise SystemExit(7)\n")
        self.assertEqual(report.first_fail_verdict, V.RE)

    def test_missing_flush_deadlocks_into_tl(self):
        # input() flushes stdout by itself, so use readline(): without a flush both sides wait forever
        source = b"import sys\nn = int(sys.stdin.readline())\nsys.stdout.write('1\\n')\nsys.stdin.readline()\n"
        report = judge().judge_submission(self.problem(), "python", source)
        self.assertEqual(report.first_fail_verdict, V.TL)

    def test_quitting_early_is_wa(self):
        report = judge().judge_submission(self.problem(), "python", b"n = int(input())\n")
        self.assertEqual(report.first_fail_verdict, V.WA)


class QuizTests(unittest.TestCase):
    def test_choice_needs_exact_set(self):
        problem = Problem.load(EXAMPLES / "quiz-python-basics")
        result = grade_choice(problem, {"q1": ["b"], "q2": ["a"], "q3": ["b"]})
        self.assertEqual((result["score"], result["max_score"]), (2.0, 4.0))
        self.assertEqual(grade_choice(problem, {"q1": ["b"], "q2": ["b", "a"], "q3": ["b"]})["score"], 4.0)

    def test_text_answers(self):
        problem = Problem.load(EXAMPLES / "what-is-the-output")
        self.assertEqual(grade_text(problem, {"a1": " 10.0 ", "a2": "LEN"})["score"], 2.0)
        self.assertEqual(grade_text(problem, {"a1": "11", "a2": "length"})["score"], 0.0)


class SelfcheckTests(unittest.TestCase):
    """Checks of problem versions (4a.4): compile_jury and selfcheck with sources given by the caller."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    @staticmethod
    def sol(name, source, expect, language="python"):
        return {"name": name, "language": language, "source": source.encode() if isinstance(source, str) else source,
                "expect": expect}

    def test_compile_jury_reports_each_program(self):
        j = judge()
        self.assertEqual(j.compile_jury(Problem.load(EXAMPLES / "sum-of-two")), [], "built-in checker: nothing to build")
        self.assertEqual(j.compile_jury(Problem.load(EXAMPLES / "pair-with-sum")),
                         [{"role": "checker", "name": "check.cpp", "ok": True, "log": ""}])
        self.assertEqual(j.compile_jury(Problem.load(EXAMPLES / "guess-number")),
                         [{"role": "interactor", "name": "interactor.cpp", "ok": True, "log": ""}])

    def test_a_checker_that_does_not_compile_is_a_fact_about_the_problem(self):
        problem = copy_problem("pair-with-sum", self.tmp)
        (problem.root / "checker/check.cpp").write_text("int main( { syntax error\n")
        rows = judge().compile_jury(problem)
        self.assertEqual([(r["role"], r["name"], r["ok"]) for r in rows], [("checker", "check.cpp", False)])
        self.assertIn("cannot compile check.cpp", rows[0]["log"])
        self.assertIn("error", rows[0]["log"])

    def test_selfcheck_with_given_sources(self):
        problem = Problem.load(EXAMPLES / "sum-of-two")
        progress = []
        rows = judge().selfcheck(problem, solutions=[
            self.sol("ok.py", "a, b = map(int, input().split())\nprint(a + b)\n", "OK"),
            self.sol("wa.py", "print(0)\n", "WA"),
            self.sol("re.py", "raise SystemExit(3)\n", "RE"),
            self.sol("ce.cpp", "int main( {", "CE", language="cpp"),
            self.sol("liar.py", "print(0)\n", "OK"),
        ], progress=lambda done, total: progress.append((done, total)))
        self.assertEqual([(r["name"], r["got"], r["ok"]) for r in rows],
                         [("ok.py", "OK", True), ("wa.py", "WA", True), ("re.py", "RE", True), ("ce.cpp", "CE", True),
                          ("liar.py", "WA", False)])
        self.assertEqual(progress, [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)])
        self.assertIsNone(rows[0]["first_fail"])
        self.assertEqual(rows[4]["first_fail"]["verdict"], "WA")
        self.assertEqual(rows[4]["first_fail"]["group"], "samples")
        self.assertEqual(rows[4]["expected"], "OK")
        self.assertIn("error", rows[3]["message"].lower(), "the compile log of a CE solution")
        self.assertNotIn("path", rows[0], "a worker's rows name the solution, there is no package path")

    def test_a_cancel_carries_the_finished_rows(self):
        from runner.judge import JudgeCancelled
        problem = Problem.load(EXAMPLES / "sum-of-two")
        done = []

        def cancel():  # nothing to stop during the first solution, stop as soon as the second one starts
            return len(done) >= 1
        with self.assertRaises(JudgeCancelled) as caught:
            judge().selfcheck(problem, solutions=[
                self.sol("a.py", "a, b = map(int, input().split())\nprint(a + b)\n", "OK"),
                self.sol("b.py", "print(0)\n", "WA")], cancel=cancel, progress=lambda d, t: done.append(d))
        self.assertEqual([(r["name"], r["ok"]) for r in caught.exception.partial], [("a.py", True)])

    def test_selfcheck_without_solutions_reads_the_package_as_before(self):
        """runner.cli selfcheck <package>: problem.yaml solutions, rows keyed by path, exit code 0."""
        for name in ("sum-of-two", "pair-with-sum", "guess-number"):
            with self.subTest(problem=name):
                rows = judge().selfcheck(Problem.load(EXAMPLES / name))
                data = yaml.safe_load((EXAMPLES / name / "problem.yaml").read_text(encoding="utf-8"))
                self.assertEqual([(r["path"], r["expected"]) for r in rows],
                                 [(s["path"], s["expect"]) for s in data["solutions"]])
                self.assertTrue(all(r["ok"] for r in rows), rows)
                self.assertTrue(all(isinstance(r["score"], float) for r in rows))

    def test_the_cli_selfcheck_prints_as_before(self):
        import subprocess
        out = subprocess.run([sys.executable, "-m", "runner.cli", "--backend", "local", "selfcheck",
                              str(EXAMPLES / "sum-of-two")], cwd=ROOT, capture_output=True, text=True, timeout=120,
                             env=child_env())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), [
            "ok   solutions/ok.py: expected OK, got OK, score 100.0 ",
            "ok   solutions/wa_int_overflow.cpp: expected WA, got WA, score 40.0 "])  # byte for byte as before 4a.4


if __name__ == "__main__":
    unittest.main()
