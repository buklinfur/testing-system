"""python -m worker <command>

  run        supervisor: start all workers of this machine (what systemd runs)
  run-one    a single worker process (started by the supervisor; handy for debugging)
  plan       print the CPU / isolate box plan and exit
  check      verify on the real machine: CPU pinning reaches sandboxed programs, API and token work
  gc         clean the disk cache now
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from .cache import DiskCache
from .config import WorkerConfig
from .logsetup import setup_logging
from .supervisor import Supervisor, make_slots
from .topology import format_cpu_list, read_cores


def _slot_for(cfg: WorkerConfig, index: int):
    slots = make_slots(cfg)
    for slot in slots:
        if slot.index == index:
            return slot
    raise SystemExit(f"worker index {index} is not in the plan ({len(slots)} workers)")


def cmd_plan(cfg: WorkerConfig) -> int:
    cores = read_cores()
    print("physical cores (logical CPUs):", ", ".join(f"[{format_cpu_list(c)}]" for c in cores))
    for slot in make_slots(cfg):
        print(f"worker {slot.index}: cpus={format_cpu_list(slot.cpus) or 'any'}  isolate boxes={slot.boxes[0]},{slot.boxes[1]}")
    used = {c for s in make_slots(cfg) for c in s.cpus}
    free = sorted({c for core in cores for c in core} - used)
    print("left for the system:", format_cpu_list(free) or "nothing")
    return 0


def cmd_check(cfg: WorkerConfig) -> int:
    """Run a tiny program in every worker's sandbox and compare the CPUs it may use with the plan."""
    from runner.common import Limits, Verdict
    from runner.compile import compile_program
    from runner.config import RunnerConfig
    from runner.languages import get_profile
    from runner.sandbox import make_factory

    from .topology import apply_affinity

    ok = True
    code = "import os\nprint(','.join(map(str, sorted(os.sched_getaffinity(0)))))\n"
    for slot in make_slots(cfg):
        rcfg = RunnerConfig(backend=cfg.backend, isolate_bin=cfg.isolate_bin, box_id_base=slot.box_base)
        factory = make_factory(rcfg)
        apply_affinity(slot.cpus)  # exactly what the worker process does before it starts boxes
        profile = get_profile("python")
        program = compile_program(factory, profile, {profile.default_source: code.encode()}, None, rcfg).program
        with factory(0) as box:
            for name, data in program.files.items():
                box.put_file(name, data)
            run = box.run(program.argv, Limits(time_ms=2000, wall_ms=5000, memory_mb=128, processes=8),
                          env=program.env, stdout_cap=1024)
        seen = run.stdout.decode().strip()
        want = format_cpu_list(slot.cpus) if slot.cpus else None
        got = format_cpu_list(int(x) for x in seen.split(",")) if seen else "?"
        good = run.verdict == Verdict.OK and (want is None or got == want)
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} worker {slot.index}: planned cpus={want or 'any'}, sandboxed program sees {got} ({run.verdict.value})")
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    one = sub.add_parser("run-one")
    one.add_argument("--index", type=int, required=True)
    for name in ("plan", "check", "gc"):
        sub.add_parser(name)
    args = parser.parse_args(argv)
    cfg = WorkerConfig.from_env()
    try:
        return _dispatch(args, cfg)
    except ValueError as exc:  # bad plan / bad configuration: a message, not a traceback
        print(f"error: {exc}", file=sys.stderr)
        return 78


def _dispatch(args, cfg: WorkerConfig) -> int:
    if args.command == "plan":
        return cmd_plan(cfg)
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "gc":
        removed = DiskCache(cfg.cache_dir).gc(cfg.max_cache_bytes, cfg.cache_min_age_s)
        print("removed:", removed)
        return 0

    setup_logging(cfg.log_level, "sup" if args.command == "run" else f"w{args.index}")
    try:
        cfg.validate()
    except ValueError as exc:
        logging.getLogger("judge").error("bad configuration: %s", exc)
        return 78
    if cfg.backend == "local" and os.environ.get("JUDGE_ALLOW_UNSAFE_LOCAL") != "1":
        logging.getLogger("judge").error("backend=local is UNSAFE and needs JUDGE_ALLOW_UNSAFE_LOCAL=1")
        return 78

    if args.command == "run":
        return Supervisor(cfg, make_slots(cfg)).run()

    from .process import WorkerProcess

    worker = WorkerProcess(cfg, _slot_for(cfg, args.index))
    worker.install_signal_handlers()
    return worker.run()


if __name__ == "__main__":
    sys.exit(main())
