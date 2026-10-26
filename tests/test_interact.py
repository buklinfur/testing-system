"""The interactive relay (runner/interact.py) and the interactive verdict policy (docs/RUNNER.md).

No test here depends on timing: every order of events is forced by causality (a program can only
react to the other one through the relay), and bounds are checked as upper bounds.
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JUDGE_ALLOW_UNSAFE_LOCAL"] = "1"

from runner.common import Verdict as V  # noqa: E402
from runner.interact import BUFFER_BYTES, INTERACTOR, SOLUTION, Relay  # noqa: E402
from tests.test_e2e_local import EXAMPLES, copy_problem, judge  # noqa: E402

PY = sys.executable


def spawn(code: str, fds) -> subprocess.Popen:
    stdin, stdout = fds
    return subprocess.Popen([PY, "-c", code], stdin=stdin, stdout=stdout, close_fds=True)


class RelayTests(unittest.TestCase):
    def run_pair(self, solution: str, interactor: str, timeout=30):
        relay = Relay()
        sol = spawn(solution, relay.solution_fds)
        inter = spawn(interactor, relay.interactor_fds)
        relay.close_program_ends()
        relay.start()
        codes = sol.wait(timeout), inter.wait(timeout)
        relay.stop()
        self.assertIsNone(relay.error)
        return relay, codes

    def test_bytes_go_both_ways(self):
        solution = "import sys\nfor line in sys.stdin:\n    print(int(line) * 2, flush=True)\n"
        interactor = ("import sys\nfor i in range(1000):\n    print(i, flush=True)\n"
                      "    assert int(sys.stdin.readline()) == 2 * i\nsys.stdout.close()\n")
        relay, codes = self.run_pair(solution, interactor)
        self.assertEqual(codes, (0, 0))
        self.assertEqual(relay.closed, [INTERACTOR, SOLUTION], "the solution ends only after the interactor closed")
        self.assertEqual(relay.first_closed(), INTERACTOR)

    def test_solution_closing_first_is_seen_first(self):
        # the interactor blocks on reading until the relay passes the solution's EOF on
        relay, codes = self.run_pair("raise SystemExit(7)", "import sys\nsys.stdin.read()\n")
        self.assertEqual(codes, (7, 0))
        self.assertEqual(relay.first_closed(), SOLUTION)

    def test_reader_gone_means_broken_pipe_for_the_writer(self):
        # the interactor exits without reading; a solution writing forever must get EPIPE, not hang
        writer = "import sys\ntry:\n    while True:\n        sys.stdout.write('x' * 65536)\nexcept BrokenPipeError:\n    raise SystemExit(5)\n"
        relay, codes = self.run_pair(writer, "pass")
        self.assertEqual(codes, (5, 0))

    def test_memory_is_bounded_and_the_writer_is_held_back(self):
        # the interactor starts reading only after a second: meanwhile the relay buffer must stay bounded
        relay = Relay()
        size = 32 << 20
        sol = spawn(f"import sys\nsys.stdout.buffer.write(b'x' * {size})\n", relay.solution_fds)
        inter = spawn("import sys, time\ntime.sleep(1.0)\nsys.stdin.buffer.read()\n", relay.interactor_fds)
        relay.close_program_ends()
        relay.start()
        while inter.poll() is None:
            self.assertLessEqual(max(len(d.buf) for d in relay._dirs), BUFFER_BYTES)
            time.sleep(0.005)
        sol.wait(30)
        relay.stop()
        self.assertIsNone(relay.error)
        self.assertEqual(relay.relayed[SOLUTION], size, "everything is delivered in the end")

    def test_buffered_bytes_before_the_reader_starts_are_bounded(self):
        # nobody reads the interactor side at all: the relay may hold one buffer plus one pipe, no more
        relay = Relay()
        r_block, w_block = os.pipe()  # the interactor waits on this, never on its stdin
        sol = subprocess.Popen([PY, "-c", "import sys\nsys.stdout.buffer.write(b'y' * (16 << 20))\n"],
                               stdin=relay.solution_fds[0], stdout=relay.solution_fds[1],
                               stderr=subprocess.DEVNULL)  # it ends with an expected BrokenPipeError
        inter = subprocess.Popen([PY, "-c", f"import os\nos.read({r_block}, 1)\n"],
                                 stdin=relay.interactor_fds[0], stdout=relay.interactor_fds[1], pass_fds=(r_block,))
        relay.close_program_ends()
        relay.start()
        deadline = time.monotonic() + 5
        last = -1
        while time.monotonic() < deadline:  # wait until the relay stops reading (steady state)
            now = relay.relayed[SOLUTION]
            if now == last and now > 0:
                break
            last = now
            time.sleep(0.2)
        pipe_capacity = 1 << 20  # upper bound for a Linux pipe (default 64 KiB, max 1 MiB)
        self.assertLessEqual(relay.relayed[SOLUTION], BUFFER_BYTES + pipe_capacity)
        self.assertIsNone(sol.poll(), "the solution is blocked on its write, not finished")
        os.write(w_block, b"x")
        os.close(w_block)
        os.close(r_block)
        inter.wait(30)
        sol.wait(30)  # the reader is gone: the solution gets a broken pipe and ends
        relay.stop()
        self.assertIsNone(relay.error)

    def test_both_closed_before_any_round_is_a_tie(self):
        relay = Relay()
        relay.close_program_ends()  # nobody will ever write: both outputs are at EOF in the first round
        relay.start()
        relay.stop()
        self.assertTrue(relay.tie)
        self.assertIsNone(relay.first_closed())

    def test_abort_before_start_closes_everything(self):
        relay = Relay()
        fds = (*relay.solution_fds, *relay.interactor_fds)
        relay.abort()
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)


class InteractiveVerdictTests(unittest.TestCase):
    """Both orders of failure, through the whole judge (local backend), deterministic by causality."""

    def problem(self):
        def edit(d):
            d["limits"] = {"time_ms": 2000, "memory_mb": 256}
        return copy_problem("guess-number", tempfile.mkdtemp(), edit)

    def test_solution_fails_first_is_re(self):
        # the interactor waits for a query; it can only see EOF after the relay saw the solution's
        report = judge().judge_submission(self.problem(), "python", b"input()\nraise SystemExit(7)\n")
        self.assertEqual(report.first_fail_verdict, V.RE)

    def test_interactor_says_wa_then_solution_fails_is_wa(self):
        # query 0 is out of range: the interactor exits with WA on its own; only then the solution's
        # next input() hits EOF and it crashes (EOFError, exit code 1)
        source = b"input()\nprint(0, flush=True)\ninput()\n"
        report = judge().judge_submission(self.problem(), "python", source)
        self.assertEqual(report.first_fail_verdict, V.WA)
        failed = [t for g in report.groups for t in g.tests if t.verdict != V.OK][0]
        self.assertIn("out of range", failed.message)

    def test_solution_ok_but_interactor_wa_is_wa(self):
        source = (EXAMPLES / "guess-number/solutions/wa_linear.py").read_bytes()
        self.assertEqual(judge().judge_submission(self.problem(), "python", source).first_fail_verdict, V.WA)

    def test_relay_failure_is_internal_error(self):
        started = time.monotonic()
        with mock.patch.object(Relay, "_write", side_effect=RuntimeError("relay broke")):
            report = judge().judge_submission(self.problem(), "python",
                                              (EXAMPLES / "guess-number/solutions/ok.py").read_bytes())
        self.assertEqual(report.verdict, V.IE)
        self.assertIn("relay", report.error or "")
        # the relay closed every pipe: both programs ended quickly instead of waiting for the wall limit
        self.assertLess(time.monotonic() - started, 20)


if __name__ == "__main__":
    unittest.main()
