from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from ..common import Limits, RunResult, SandboxError


class PipeCapture:
    """Reads a pipe in a thread, keeps at most `cap` bytes.

    stop_on_overflow=True closes the pipe once the cap is exceeded: the writer then gets
    SIGPIPE, which is how an output flood is stopped. Otherwise the pipe is drained and
    the extra bytes are discarded (used for stderr, so the program never blocks on it).
    """

    def __init__(self, stream, cap: int, stop_on_overflow: bool) -> None:
        self.stream = stream
        self.cap = cap
        self.stop_on_overflow = stop_on_overflow
        self.data = bytearray()
        self.overflow = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read1(65536)
                if not chunk:
                    break
                room = self.cap - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if len(chunk) > room:
                    self.overflow = True
                    if self.stop_on_overflow:
                        break
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    def join(self) -> bytes:
        self.thread.join(timeout=10)
        return bytes(self.data)


def safe_relpath(name: str) -> PurePosixPath:
    """Validate a file name that will be created inside a sandbox."""
    if not name or "\x00" in name or "\\" in name:
        raise SandboxError(f"bad file name: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.parts[0] == ".":
        raise SandboxError(f"bad file name: {name!r}")
    return path


class RunningProcess(ABC):
    @abstractmethod
    def wait(self) -> RunResult: ...


class Sandbox(ABC):
    """One isolated working directory plus the ability to run a program in it.

    Use as a context manager: the box is created on enter and destroyed on exit.
    Commands are executed with the box as the working directory, so argv uses
    relative paths such as "./sol".
    """

    box_dir: Path
    inside_path: str  # how the box directory looks from inside ("/box" for isolate)

    def __enter__(self) -> "Sandbox":
        self.init()
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()

    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def cleanup(self) -> None: ...

    @abstractmethod
    def start(
        self,
        argv: list,
        limits: Limits,
        *,
        stdin_file: Optional[str] = None,
        stdin_fd: Optional[int] = None,
        stdout_fd: Optional[int] = None,
        stdout_cap: int = 0,
        stderr_cap: int = 65536,
        env: Optional[Mapping[str, str]] = None,
    ) -> RunningProcess: ...

    def run(self, argv: list, limits: Limits, **kwargs) -> RunResult:
        return self.start(argv, limits, **kwargs).wait()

    # -- files ----------------------------------------------------------------
    def put_file(self, name: str, data: bytes, executable: bool = False) -> None:
        rel = safe_relpath(name)
        path = self.box_dir.joinpath(*rel.parts)
        parent = path.parent
        if parent != self.box_dir:
            os.makedirs(parent, mode=0o755, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, 0o755 if executable else 0o644)

    def read_file(self, name: str, max_bytes: int) -> Optional[bytes]:
        rel = safe_relpath(name)
        path = self.box_dir.joinpath(*rel.parts)
        try:
            if path.is_symlink() or not path.is_file():
                return None
            with open(path, "rb") as fh:
                data = fh.read(max_bytes + 1)
        except PermissionError as exc:
            raise SandboxError(f"cannot read {name} from the box (permissions): {exc}") from exc
        if len(data) > max_bytes:
            raise SandboxError(f"file {name} is larger than {max_bytes} bytes")
        return data
