"""Overhead of the interactive plumbing: N short request/response exchanges between C++ programs.

    python tools/bench_interactive.py [--backend isolate|local] [--exchanges 100000] [--repeat 5]

Builds a throw-away interactive problem (the interactor sends i, the solution answers i + 1, N times,
each message flushed), judges a correct C++ solution `--repeat` times and prints the wall time of the
interactive test (both sandboxes, without compilation) and the solution's CPU time per run, plus medians. The local backend needs JUDGE_ALLOW_UNSAFE_LOCAL=1.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runner.config import RunnerConfig  # noqa: E402
from runner.judge import Judge  # noqa: E402
from runner.package import Problem  # noqa: E402

INTERACTOR = r"""
#include <cstdio>
int main(int argc, char** argv) {
    FILE* in = fopen(argv[1], "r");
    long n;
    if (!in || fscanf(in, "%ld", &n) != 1) { fprintf(stderr, "bad input\n"); return 3; }
    printf("%ld\n", n); fflush(stdout);
    for (long i = 0; i < n; ++i) {
        printf("%ld\n", i); fflush(stdout);
        long r;
        if (scanf("%ld", &r) != 1) { fprintf(stderr, "no answer at %ld\n", i); return 1; }
        if (r != i + 1) { fprintf(stderr, "wrong answer at %ld\n", i); return 1; }
    }
    return 0;
}
"""

SOLUTION = rb"""
#include <cstdio>
int main() {
    long n, x;
    if (scanf("%ld", &n) != 1) return 1;
    for (long i = 0; i < n; ++i) {
        if (scanf("%ld", &x) != 1) return 1;
        printf("%ld\n", x + 1); fflush(stdout);
    }
    return 0;
}
"""

PROBLEM_YAML = """format_version: 1
slug: bench-interactive
title: { ru: "bench" }
type: interactive
limits: { time_ms: 10000, memory_mb: 256 }
languages: [cpp]
interactor: { path: interactor/interactor.cpp }
feedback: verdict_only
test_groups:
  - { name: main, tests: "tests/t*.in", points: 100, visibility: hidden }
"""


def build(root: Path, exchanges: int) -> Problem:
    (root / "interactor").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "statement").mkdir()
    (root / "interactor/interactor.cpp").write_text(INTERACTOR)
    (root / "tests/t01.in").write_text(f"{exchanges}\n")
    (root / "statement/ru.md").write_text("bench\n")
    (root / "problem.yaml").write_text(PROBLEM_YAML)
    return Problem.load(root)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default="isolate", choices=["isolate", "local"])
    ap.add_argument("--exchanges", type=int, default=100_000)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--box-base", type=int, default=None)
    args = ap.parse_args()
    overrides = {} if args.box_base is None else {"box_id_base": args.box_base}
    judge = Judge(RunnerConfig(backend=args.backend, **overrides))
    test_seconds = []
    run_interactive = judge._run_interactive

    def timed(*a, **kw):  # wall time of the interactive test itself, without compilation
        t = time.monotonic()
        try:
            return run_interactive(*a, **kw)
        finally:
            test_seconds.append(time.monotonic() - t)

    judge._run_interactive = timed
    with tempfile.TemporaryDirectory() as tmp:
        problem = build(Path(tmp) / "bench", args.exchanges)
        walls, cpus = [], []
        for n in range(args.repeat):
            t0 = time.monotonic()
            report = judge.judge_submission(problem, "cpp", SOLUTION)
            total = time.monotonic() - t0
            test = report.groups[0].tests[0]
            if report.verdict.value != "OK":
                print(f"run {n}: verdict {report.verdict.value} ({test.message}); compile log: {report.compile_log}")
                return 1
            wall_ms = int(test_seconds[-1] * 1000)
            walls.append(wall_ms)
            cpus.append(test.time_ms)
            print(f"run {n}: test wall {wall_ms} ms, solution cpu {test.time_ms} ms, judge_submission {total:.2f} s")
        print(f"median: test wall {statistics.median(walls)} ms, solution cpu {statistics.median(cpus)} ms "
              f"({args.exchanges} exchanges, backend {args.backend})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
