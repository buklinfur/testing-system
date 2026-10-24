"""Supervisor: starts N worker processes, pins each to its CPUs, restarts crashed ones, stops gracefully.

    python -m worker run

Each worker is a separate OS process (`python -m worker run-one --index i`), so a crash or a hang of
one judging job never affects the others, and the CPU affinity is simply inherited by everything the
worker starts (isolate, compilers, the solution).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .cache import DiskCache
from .config import WorkerConfig
from .process import EXIT_FATAL, EXIT_SUPERSEDED
from .topology import WorkerSlot, format_cpu_list, parse_explicit_plan, plan_workers, read_cores

log = logging.getLogger("judge.supervisor")
GC_INTERVAL_S = 600.0


def make_slots(cfg: WorkerConfig) -> list:
    explicit = parse_explicit_plan(cfg.cpu_plan) if cfg.cpu_plan else None
    cores = read_cores() if (cfg.pin_cpus and explicit is None) else []
    slots = plan_workers(cores, cfg.workers, cfg.reserve_cores, cfg.box_base, explicit=explicit, pin=cfg.pin_cpus)
    if explicit is None and len(slots) < cfg.workers:
        log.warning("only %d cores are free (asked for %d workers): starting %d", len(slots), cfg.workers, len(slots))
    return slots


def restart_delay(consecutive_failures: int, exit_code: Optional[int]) -> float:
    if exit_code in (EXIT_FATAL, EXIT_SUPERSEDED):
        return 60.0  # a bad token or a duplicate supervisor: do not hammer the server
    return min(1.0 * (2 ** max(0, consecutive_failures - 1)), 60.0)


@dataclass
class Child:
    slot: WorkerSlot
    proc: Optional[subprocess.Popen] = None
    started: float = 0.0
    failures: int = 0
    next_start: float = 0.0
    last_exit: Optional[int] = None


class Supervisor:
    def __init__(self, cfg: WorkerConfig, slots: list, spawn: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.children = [Child(s) for s in slots]
        self._spawn = spawn or self._default_spawn
        self._clock = clock
        self.stopping = threading.Event()
        self._term_sent = False

    def _default_spawn(self, slot: WorkerSlot) -> subprocess.Popen:
        cmd = [sys.executable, "-m", "worker", "run-one", "--index", str(slot.index)]
        env = dict(os.environ, JUDGE_SLOT_CPUS=format_cpu_list(slot.cpus), JUDGE_SLOT_BOX_BASE=str(slot.box_base))
        return subprocess.Popen(cmd, env=env)

    # ------------------------------------------------------------------ loop
    def tick(self) -> None:
        """One supervision step (separate from run() so it can be tested with a fake clock)."""
        now = self._clock()
        for child in self.children:
            if child.proc is not None:
                code = child.proc.poll()
                if code is None:
                    if now - child.started > 300:
                        child.failures = 0  # it has been healthy for a while
                    continue
                child.proc, child.last_exit = None, code
                if self.stopping.is_set():
                    continue
                child.failures = 0 if (now - child.started > 300 and code not in (EXIT_FATAL, EXIT_SUPERSEDED)) else child.failures + 1
                delay = restart_delay(child.failures, code)
                child.next_start = now + delay
                log.warning("worker %d exited with code %s; restart in %.0fs", child.slot.index, code, delay)
            if child.proc is None and not self.stopping.is_set() and now >= child.next_start:
                log.info("starting worker %d (cpus=%s boxes=%s)", child.slot.index,
                         format_cpu_list(child.slot.cpus) or "any", child.slot.boxes)
                child.proc = self._spawn(child.slot)
                child.started = now

    def request_stop(self) -> None:
        self.stopping.set()

    def _terminate_children(self, sig: int) -> None:
        for child in self.children:
            if child.proc is not None and child.proc.poll() is None:
                try:
                    child.proc.send_signal(sig)
                except OSError:
                    pass

    def shutdown(self) -> None:
        """SIGTERM to every worker (they finish their current job), wait, then SIGKILL stragglers."""
        log.info("stopping %d workers (drain timeout %.0fs)", sum(c.proc is not None for c in self.children),
                 self.cfg.drain_timeout_s)
        self._terminate_children(signal.SIGTERM)
        deadline = time.monotonic() + self.cfg.drain_timeout_s + 20
        while time.monotonic() < deadline and any(c.proc and c.proc.poll() is None for c in self.children):
            time.sleep(0.2)
        stuck = [c for c in self.children if c.proc and c.proc.poll() is None]
        for child in stuck:
            log.error("worker %d did not stop in time: killing it", child.slot.index)
            child.proc.kill()
        for child in stuck:
            child.proc.wait()

    def run(self) -> int:
        cache = DiskCache(self.cfg.cache_dir)
        last_gc = 0.0

        def on_signal(signum, _frame):
            if self.stopping.is_set():
                self._terminate_children(signal.SIGTERM)  # second signal: workers abort their jobs
            self.stopping.set()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        log.info("supervisor started: %d workers", len(self.children))
        while not self.stopping.is_set():
            self.tick()
            if self._clock() - last_gc > GC_INTERVAL_S:
                last_gc = self._clock()
                try:
                    removed = cache.gc(self.cfg.max_cache_bytes, self.cfg.cache_min_age_s)
                    if any(removed.values()):
                        log.info("cache gc removed %s", removed)
                except OSError as exc:
                    log.warning("cache gc failed: %s", exc)
            self.stopping.wait(1.0)
        self.shutdown()
        return 0
