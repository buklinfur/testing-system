"""UNSAFE development backend: plain subprocess + rlimits. NOT a sandbox.

It exists so the judging logic can be tested on machines without isolate (CI, a laptop).
Never use it for code written by students. Enabled only if JUDGE_ALLOW_UNSAFE_LOCAL=1.
"""
from __future__ import annotations

import math
import os
import resource
import shutil
import signal as signal_module
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from ..common import Limits, RunResult, SandboxError, Verdict
from .base import PipeCapture, RunningProcess, Sandbox

DEFAULT_ENV = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
MEMORY_HINTS = (b"MemoryError", b"std::bad_alloc", b"Cannot allocate memory", b"out of memory")


class _LocalRunning(RunningProcess):
    def __init__(self, proc, limits, out_cap, err_cap, timer, fired) -> None:
        self.proc, self.limits, self.out_cap, self.err_cap, self.timer, self.fired = proc, limits, out_cap, err_cap, timer, fired
        self.started = time.monotonic()

    def wait(self) -> RunResult:
        _, status, usage = os.wait4(self.proc.pid, 0)
        ended = time.monotonic()
        self.proc.returncode = 0  # reaped here; keep Popen from waiting again
        self.timer.cancel()
        try:
            os.killpg(self.proc.pid, signal_module.SIGKILL)  # stray children
        except OSError:
            pass
        stdout = self.out_cap.join() if self.out_cap else b""
        stderr = self.err_cap.join()
        sig = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
        code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
        cpu_ms = int((usage.ru_utime + usage.ru_stime) * 1000)
        mem_kb = int(usage.ru_maxrss)
        lim = self.limits
        base = dict(exit_code=code, signal=sig, time_ms=cpu_ms, wall_ms=int((ended - self.started) * 1000),
                    memory_kb=mem_kb, stdout=stdout, stderr=stderr, ended_at=ended)
        overflow = bool(self.out_cap and self.out_cap.overflow)
        if overflow:
            return RunResult(Verdict.OL, stdout_overflow=True, **base)
        if sig == int(signal_module.SIGXCPU) or (lim.time_ms and cpu_ms > lim.time_ms and (sig or code)):
            return RunResult(Verdict.TL, tl_kind="cpu", **base)
        if self.fired.is_set():  # the wall-clock timer killed it
            return RunResult(Verdict.TL, tl_kind="wall", **base)
        if sig == int(signal_module.SIGXFSZ):
            return RunResult(Verdict.OL, **base)
        failed = bool(sig) or bool(code)
        if failed and lim.memory_mb:
            if any(h in stderr for h in MEMORY_HINTS) or mem_kb >= 0.8 * lim.memory_mb * 1024:
                return RunResult(Verdict.ML, **base)
        if failed:
            return RunResult(Verdict.RE, **base)
        return RunResult(Verdict.OK, **base)


class LocalSandbox(Sandbox):
    inside_path = ""

    def __init__(self) -> None:
        if os.environ.get("JUDGE_ALLOW_UNSAFE_LOCAL") != "1":
            raise SandboxError("the local backend is UNSAFE; set JUDGE_ALLOW_UNSAFE_LOCAL=1 to use it for development")
        self._tmp: Optional[str] = None
        self.box_dir = Path()

    def init(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="localbox-")
        self.box_dir = Path(self._tmp) / "box"
        self.box_dir.mkdir()
        self.inside_path = str(self.box_dir)

    def cleanup(self) -> None:
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def start(self, argv, limits, *, stdin_file=None, stdin_fd=None, stdout_fd=None, stdout_cap=0,
              stderr_cap=65536, env: Optional[Mapping[str, str]] = None):
        def preexec() -> None:
            if limits.time_ms:
                soft = max(1, math.ceil(limits.time_ms / 1000))
                resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 1))
            if limits.memory_mb:
                cap = limits.memory_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
            if limits.fsize_kb:
                cap = limits.fsize_kb * 1024
                resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
            if limits.stack_kb:
                cap = limits.stack_kb * 1024
                resource.setrlimit(resource.RLIMIT_STACK, (cap, cap))

        stdin = subprocess.DEVNULL
        opened = None
        if stdin_file:
            opened = open(self.box_dir / stdin_file, "rb")
            stdin = opened
        elif stdin_fd is not None:
            stdin = stdin_fd
        proc = subprocess.Popen(
            argv, cwd=self.box_dir, stdin=stdin,
            stdout=stdout_fd if stdout_fd is not None else subprocess.PIPE,
            stderr=subprocess.PIPE, env={**DEFAULT_ENV, **(env or {})},
            preexec_fn=preexec, start_new_session=True, close_fds=True,
        )
        if opened:
            opened.close()
        out_cap = PipeCapture(proc.stdout, stdout_cap, True) if stdout_fd is None else None
        err_cap = PipeCapture(proc.stderr, stderr_cap, False)
        fired = threading.Event()

        def kill() -> None:
            fired.set()
            try:
                os.killpg(proc.pid, signal_module.SIGKILL)
            except OSError:
                pass

        timer = threading.Timer((limits.wall_ms or 600_000) / 1000, kill)
        timer.daemon = True
        timer.start()
        return _LocalRunning(proc, limits, out_cap, err_cap, timer, fired)
