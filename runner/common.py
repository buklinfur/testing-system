from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional


class Verdict(str, enum.Enum):
    OK = "OK"
    WA = "WA"
    PE = "PE"
    TL = "TL"
    ML = "ML"
    RE = "RE"
    CE = "CE"
    OL = "OL"
    PARTIAL = "PARTIAL"
    SKIPPED = "SKIPPED"
    IE = "IE"


class SandboxError(Exception):
    """The sandbox itself misbehaved (not the submitted program)."""


@dataclass(frozen=True)
class Limits:
    time_ms: Optional[int]
    wall_ms: Optional[int]
    memory_mb: Optional[int]
    processes: int = 16
    fsize_kb: Optional[int] = None  # largest file the program may create
    stack_kb: Optional[int] = None


@dataclass
class RunResult:
    """Outcome of one sandboxed process.

    verdict OK means "the process finished normally with exit code 0".
    TL / ML / OL / RE / IE describe how it failed.
    """

    verdict: Verdict
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    time_ms: int = 0
    wall_ms: int = 0
    memory_kb: Optional[int] = None
    message: str = ""
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_overflow: bool = False
    ended_at: float = 0.0  # time.monotonic() when the process ended
