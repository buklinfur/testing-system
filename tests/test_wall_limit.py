"""limits.wall_ms in the problem format, and TL by CPU vs TL by wall clock (runner, explanation, worker payload)."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JUDGE_ALLOW_UNSAFE_LOCAL"] = "1"

from runner.common import Limits, Verdict as V  # noqa: E402
from runner.explain import TL_KIND_RU, student_view  # noqa: E402
from runner.languages import get_profile  # noqa: E402
from runner.sandbox.isolate import classify_isolate, parse_meta  # noqa: E402
from tests.test_e2e_local import EXAMPLES, copy_problem, judge  # noqa: E402
from worker.results import code_result  # noqa: E402


def sum_problem(**limits):
    def edit(d):
        d["limits"] = {"memory_mb": 256, **limits}
        d["feedback"] = "full"
    return copy_problem("sum-of-two", tempfile.mkdtemp(), edit)


class LimitsTests(unittest.TestCase):
    def limits(self, language="cpp", **problem_limits):
        j = judge()
        return j._limits(sum_problem(**problem_limits), get_profile(language))

    def test_default_is_twice_the_time_plus_a_second(self):
        lim = self.limits(time_ms=1000)
        self.assertEqual((lim.time_ms, lim.wall_ms), (1000, 3000))

    def test_explicit_wall_limit(self):
        lim = self.limits(time_ms=1000, wall_ms=8000)
        self.assertEqual((lim.time_ms, lim.wall_ms), (1000, 8000))

    def test_explicit_wall_limit_scales_with_the_language(self):
        profile = get_profile("python")
        lim = self.limits("python", time_ms=1000, wall_ms=4000)
        self.assertEqual(lim.wall_ms, int(4000 * profile.time_multiplier))

    def test_explicit_wall_limit_is_capped(self):
        j = judge()
        lim = j._limits(sum_problem(time_ms=1000, wall_ms=10_000_000), get_profile("cpp"))
        self.assertEqual(lim.wall_ms, j.config.wall_ms_cap)

    def test_wall_limit_without_time_limit(self):
        lim = self.limits(wall_ms=5000)
        self.assertEqual(lim.wall_ms, 5000)
        self.assertEqual(lim.time_ms, judge().config.time_ms_cap)


class ClassifyKindTests(unittest.TestCase):
    LIM = Limits(time_ms=1000, wall_ms=3000, memory_mb=256)

    def classify(self, text):
        return classify_isolate(parse_meta(text), self.LIM)

    def test_cpu_timeout(self):
        r = self.classify("status:TO\nmessage:Time limit exceeded\ntime:1.2\ntime-wall:1.3\n")
        self.assertEqual((r.verdict, r.tl_kind), (V.TL, "cpu"))

    def test_wall_timeout_by_message(self):
        r = self.classify("status:TO\nmessage:Time limit exceeded (wall clock)\ntime:0.01\ntime-wall:3.0\n")
        self.assertEqual((r.verdict, r.tl_kind), (V.TL, "wall"))

    def test_wall_timeout_by_numbers(self):
        r = self.classify("status:TO\ntime:0.01\ntime-wall:3.1\n")
        self.assertEqual((r.verdict, r.tl_kind), (V.TL, "wall"))

    def test_finished_over_the_cpu_limit(self):
        r = self.classify("time:1.5\ntime-wall:1.6\nexitcode:0\n")
        self.assertEqual((r.verdict, r.tl_kind), (V.TL, "cpu"))

    def test_other_verdicts_have_no_kind(self):
        self.assertIsNone(self.classify("time:0.01\nexitcode:0\n").tl_kind)
        self.assertIsNone(self.classify("status:RE\nexitcode:3\ntime:0.01\n").tl_kind)


class LocalRunKindTests(unittest.TestCase):
    """Through the whole judge on the local backend."""

    def first_failed(self, report):
        return [t for t in report.tests if t.verdict != V.OK][0]

    def test_busy_loop_is_cpu(self):
        report = judge().judge_submission(sum_problem(time_ms=300), "cpp", b"int main() { for (;;) {} }\n")
        test = self.first_failed(report)
        self.assertEqual((test.verdict, test.tl_kind), (V.TL, "cpu"))

    def test_sleeping_is_wall(self):
        source = b"#include <unistd.h>\nint main() { sleep(30); }\n"
        report = judge().judge_submission(sum_problem(time_ms=300, wall_ms=600), "cpp", source)
        test = self.first_failed(report)
        self.assertEqual((test.verdict, test.tl_kind), (V.TL, "wall"))

    def test_explanation_and_payload(self):
        source = b"#include <unistd.h>\nint main() { sleep(30); }\n"
        problem = sum_problem(time_ms=300, wall_ms=600)
        report = judge().judge_submission(problem, "cpp", source)
        view = student_view(report, problem, "cpp")
        shown = [t for t in view["tests"] if t["verdict"] == "TL"][0]
        self.assertEqual((shown["tl_kind"], shown["time_limit_hint"]), ("wall", TL_KIND_RU["wall"]))
        payload = code_result(report, problem, "cpp", list(range(1, len(report.tests) + 1)))
        self.assertEqual({t["tl_kind"] for t in payload["tests"] if t["verdict"] == "TL"}, {"wall"})
        self.assertEqual({t["tl_kind"] for t in payload["tests"] if t["verdict"] != "TL"}, {None})

    def test_verdict_only_hides_the_kind(self):
        def edit(d):
            d["limits"] = {"time_ms": 300, "memory_mb": 256}
            d["feedback"] = "verdict_only"
        problem = copy_problem("sum-of-two", tempfile.mkdtemp(), edit)
        report = judge().judge_submission(problem, "cpp", b"int main() { for (;;) {} }\n")
        view = student_view(report, problem, "cpp")
        self.assertFalse(any("tl_kind" in t or "time_limit_hint" in t for t in view.get("tests", [])))


class ValidatorTests(unittest.TestCase):
    def validate(self, **limits):
        problem = sum_problem(**limits)
        return subprocess.run([sys.executable, str(ROOT / "tools/validate_problem.py"), str(problem.root)],
                              capture_output=True, text=True)

    def test_wall_below_time_is_an_error(self):
        r = self.validate(time_ms=1000, wall_ms=500)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("wall_ms", r.stdout + r.stderr)

    def test_wall_limit_is_accepted_by_the_schema(self):
        r = self.validate(time_ms=1000, wall_ms=5000)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
