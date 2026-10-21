"""Backend on top of `isolate` (https://github.com/ioi/isolate, version 2.x, cgroup v2).

Assumptions that must be verified on the real machine with `python -m runner.cli sandbox-check`:
  * stdin/stdout/stderr of the isolate process are inherited by the sandboxed program;
  * files copied into the box by the invoking user are readable/executable inside the sandbox;
  * files created by the sandboxed program in the box (compiler output) are readable by us.
"""
from __future__ import annotations

import os
import signal as signal_module
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

from ..common import Limits, RunResult, SandboxError, Verdict
from .base import PipeCapture, RunningProcess, Sandbox

DEFAULT_ENV = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
SIGKILL = int(signal_module.SIGKILL)
SIGXFSZ = int(signal_module.SIGXFSZ)
SIGSEGV = int(signal_module.SIGSEGV)


def parse_meta(text: str) -> dict:
    meta = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


def classify_isolate(meta: dict, limits: Limits, stdout_overflow: bool = False) -> RunResult:
    """Translate isolate's meta file into a RunResult (pure function, unit-tested)."""
    time_ms = int(round(float(meta.get("time", "0")) * 1000))
    wall_ms = int(round(float(meta.get("time-wall", "0")) * 1000))
    mem_kb = None
    for key in ("cg-mem", "max-rss"):
        if key in meta:
            mem_kb = int(meta[key])
            break
    status = meta.get("status")
    message = meta.get("message", "")
    exit_code = int(meta["exitcode"]) if "exitcode" in meta else None
    sig = int(meta["exitsig"]) if "exitsig" in meta else None
    base = dict(exit_code=exit_code, signal=sig, time_ms=time_ms, wall_ms=wall_ms, memory_kb=mem_kb, message=message)

    if status == "XX":
        return RunResult(Verdict.IE, **base)
    if stdout_overflow:
        return RunResult(Verdict.OL, **base)
    if status == "TO":
        return RunResult(Verdict.TL, **base)
    if meta.get("cg-oom-killed") == "1":
        return RunResult(Verdict.ML, **base)
    near_memory_limit = bool(limits.memory_mb and mem_kb and mem_kb >= 0.9 * limits.memory_mb * 1024)
    if status == "SG":
        if sig == SIGXFSZ:
            return RunResult(Verdict.OL, **base)
        if near_memory_limit and sig in (SIGKILL, SIGSEGV, int(signal_module.SIGABRT)):
            return RunResult(Verdict.ML, **base)
        return RunResult(Verdict.RE, **base)
    if status == "RE":
        return RunResult(Verdict.ML if near_memory_limit else Verdict.RE, **base)
    if limits.time_ms and time_ms > limits.time_ms:
        return RunResult(Verdict.TL, **base)
    return RunResult(Verdict.OK, **base)


class _IsolateRunning(RunningProcess):
    def __init__(self, proc, meta_path, limits, out_cap, err_cap) -> None:
        self.proc = proc
        self.meta_path = meta_path
        self.limits = limits
        self.out_cap = out_cap
        self.err_cap = err_cap

    def wait(self) -> RunResult:
        rc = self.proc.wait()
        ended = time.monotonic()
        stdout = self.out_cap.join() if self.out_cap else b""
        stderr = self.err_cap.join() if self.err_cap else b""
        try:
            meta_text = Path(self.meta_path).read_text(errors="replace")
        finally:
            try:
                os.unlink(self.meta_path)
            except OSError:
                pass
        if not meta_text.strip():
            return RunResult(
                Verdict.IE,
                message=f"isolate returned {rc} without a meta file: {stderr[:300].decode(errors='replace')}",
                stderr=stderr,
                ended_at=ended,
            )
        result = classify_isolate(parse_meta(meta_text), self.limits, bool(self.out_cap and self.out_cap.overflow))
        result.stdout, result.stderr = stdout, stderr
        result.stdout_overflow = bool(self.out_cap and self.out_cap.overflow)
        result.ended_at = ended
        return result


class IsolateSandbox(Sandbox):
    inside_path = "/box"

    def __init__(self, box_id: int, isolate_bin: str = "isolate") -> None:
        self.box_id = box_id
        self.bin = isolate_bin
        self.box_dir = Path()

    def _isolate(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [self.bin, "--cg", "-b", str(self.box_id), *args], capture_output=True, text=True, timeout=60
        )
        if check and proc.returncode != 0:
            raise SandboxError(f"isolate {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc

    def init(self) -> None:
        self._isolate("--cleanup", check=False)  # leftovers of a crashed run
        root = Path(self._isolate("--init").stdout.strip())
        self.box_dir = root / "box"
        try:
            os.chmod(self.box_dir, 0o777)  # the sandbox user must be able to create files here
        except OSError:
            pass

    def cleanup(self) -> None:
        self._isolate("--cleanup", check=False)

    def start(
        self,
        argv,
        limits,
        *,
        stdin_file=None,
        stdin_fd=None,
        stdout_fd=None,
        stdout_cap=0,
        stderr_cap=65536,
        env: Optional[Mapping[str, str]] = None,
    ):
        fd, meta_path = tempfile.mkstemp(prefix="isolate-meta-")
        os.close(fd)
        cmd = [self.bin, "--cg", "-b", str(self.box_id), "--silent", f"--meta={meta_path}"]
        if limits.time_ms:
            cmd.append(f"--time={limits.time_ms / 1000:.3f}")
            cmd.append("--extra-time=0.3")
        if limits.wall_ms:
            cmd.append(f"--wall-time={limits.wall_ms / 1000:.3f}")
        if limits.memory_mb:
            cmd.append(f"--cg-mem={limits.memory_mb * 1024}")
        stack_kb = limits.stack_kb or (limits.memory_mb * 1024 if limits.memory_mb else None)
        if stack_kb:
            cmd.append(f"--stack={stack_kb}")
        cmd.append(f"--processes={limits.processes}")
        if limits.fsize_kb:
            cmd.append(f"--fsize={limits.fsize_kb}")
        if stdin_file:
            cmd.append(f"--stdin={stdin_file}")
        for key, value in {**DEFAULT_ENV, **(env or {})}.items():
            cmd.append(f"--env={key}={value}")
        cmd += ["--run", "--", *argv]

        proc = subprocess.Popen(
            cmd,
            stdin=stdin_fd if stdin_fd is not None else subprocess.DEVNULL,
            stdout=stdout_fd if stdout_fd is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        out_cap = PipeCapture(proc.stdout, stdout_cap, True) if stdout_fd is None else None
        err_cap = PipeCapture(proc.stderr, stderr_cap, False)
        return _IsolateRunning(proc, meta_path, limits, out_cap, err_cap)
