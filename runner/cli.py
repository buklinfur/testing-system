"""Command line interface.

    python -m runner.cli judge <package> <source-or-zip> --lang cpp
    python -m runner.cli selfcheck <package>
    python -m runner.cli sandbox-check
    python -m runner.cli redteam
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import RunnerConfig
from .judge import Judge
from .languages import guess_language
from .package import Problem
from .redteam import run_redteam
from .selftest import sandbox_check


def _config(args) -> RunnerConfig:
    config = RunnerConfig(backend=args.backend, box_id_base=args.box_base)
    if getattr(args, "all_tests", False):
        config.run_all_tests = True
    return config


def _print_report(report) -> None:
    print(f"verdict: {report.verdict.value}   score: {report.score}/{report.max_score}")
    if report.compile_log:
        print("compile log:\n" + report.compile_log)
    if report.error:
        print("internal error:", report.error)
    for group in report.groups:
        print(f"  group {group.name}: {group.score}/{group.points} ({group.scoring}, {group.visibility})")
        for t in group.tests:
            extra = f" {t.message}" if t.message else ""
            print(f"    #{t.index:<3} {t.name:<12} {t.verdict.value:<8} {t.time_ms:>5} ms {t.memory_kb or 0:>8} KB{extra}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="runner")
    parser.add_argument("--backend", default="isolate", choices=["isolate", "local"])
    parser.add_argument("--box-base", type=int, default=0, help="first isolate box id (a worker uses two)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("judge")
    p.add_argument("package", type=Path)
    p.add_argument("source", type=Path)
    p.add_argument("--lang")
    p.add_argument("--archive", action="store_true")
    p.add_argument("--all-tests", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("selfcheck")
    p.add_argument("package", type=Path)

    sub.add_parser("sandbox-check")

    p = sub.add_parser("redteam")
    p.add_argument("--local-safe", action="store_true", help="only cases that cannot hurt the host (local backend)")

    args = parser.parse_args(argv)
    config = _config(args)

    if args.command == "judge":
        problem = Problem.load(args.package)
        language = args.lang or guess_language(args.source.name)
        report = Judge(config).judge_submission(problem, language, args.source.read_bytes(), is_archive=args.archive)
        if args.json:
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        else:
            _print_report(report)
        return 0 if report.verdict.value == "OK" else 1

    if args.command == "selfcheck":
        problem = Problem.load(args.package)
        rows = Judge(config).selfcheck(problem)
        for r in rows:
            mark = "ok  " if r["ok"] else "FAIL"
            print(f"{mark} {r['path']}: expected {r['expected']}, got {r['got']}, score {r['score']} {r['error'] or ''}")
        return 0 if all(r["ok"] for r in rows) else 1

    if args.command == "sandbox-check":
        rows = sandbox_check(config)
    else:
        rows = run_redteam(config, only_local_safe=args.local_safe)
    for r in rows:
        mark = "ok  " if r["ok"] else "FAIL"
        print(f"{mark} {r['name']}: {r.get('verdict', '')} {r.get('detail', '')}")
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
