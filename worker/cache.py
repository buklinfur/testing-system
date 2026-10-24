"""Disk caches shared by all worker processes of a machine.

Layout under cache_dir:
  blobs/ab/<sha256>        test inputs/answers and jury sources, content addressed (verified on download)
  problems/<manifest>/     materialized problem packages (hard links into blobs/)
  jury/<key>               compiled checkers / interactors (+ <key>.sum for integrity)
  locks/<name>.lock        flock files so that only one process downloads or builds a thing

Rules that make concurrent use by several processes safe:
  * every file is written to a temporary name and moved with os.replace (atomic);
  * downloads are verified against the sha256 before they become visible;
  * eviction only removes entries that were not used for `min_age_s` (users call touch()).
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

HEX = set("0123456789abcdef")


class IntegrityError(Exception):
    """A downloaded file does not match its sha256."""


def valid_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".tmp-{os.getpid()}-{secrets.token_hex(4)}-{dest.name}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


class Counters:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.values = {"blob_hits": 0, "blob_misses": 0, "problem_hits": 0, "problem_misses": 0,
                       "jury_hits": 0, "jury_misses": 0, "bytes_downloaded": 0}

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.values[key] += n

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.values)


class DiskCache:
    def __init__(self, root: Path, counters: Optional[Counters] = None) -> None:
        self.root = Path(root)
        self.counters = counters or Counters()
        for sub in ("blobs", "problems", "jury", "locks"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- blobs
    def blob_path(self, sha256: str) -> Path:
        if not valid_sha256(sha256):
            raise ValueError(f"bad sha256: {sha256!r}")
        return self.root / "blobs" / sha256[:2] / sha256

    def get_blob(self, sha256: str, fetch: Callable[[Callable[[bytes], None]], None]) -> Path:
        """Return the cached file, downloading it with fetch(write) first if needed.

        fetch receives a `write(chunk)` callable and must feed it the whole content.
        """
        path = self.blob_path(sha256)
        if path.is_file():
            self.counters.add("blob_hits")
            self.touch(path)
            return path
        with file_lock(self.root / "locks" / f"blob-{sha256[:16]}.lock"):
            if path.is_file():  # another process finished while we waited
                self.counters.add("blob_hits")
                self.touch(path)
                return path
            self.counters.add("blob_misses")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
            digest, size = hashlib.sha256(), 0
            try:
                with open(tmp, "wb") as out:
                    def write(chunk: bytes) -> None:
                        nonlocal size
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)

                    fetch(write)
                if digest.hexdigest() != sha256:
                    raise IntegrityError(f"downloaded content has sha256 {digest.hexdigest()}, expected {sha256}")
                os.replace(tmp, path)
                self.counters.add("bytes_downloaded", size)
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        return path

    # ---------------------------------------------------------------- problems
    def problem_dir(self, manifest_sha256: str) -> Path:
        if not valid_sha256(manifest_sha256):
            raise ValueError(f"bad manifest hash: {manifest_sha256!r}")
        return self.root / "problems" / manifest_sha256

    def new_problem_tmp(self) -> Path:
        tmp = self.root / "problems" / f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
        tmp.mkdir(parents=True)
        return tmp

    def publish_problem(self, tmp: Path, final: Path) -> None:
        try:
            os.rename(tmp, final)
        except OSError:
            # somebody else published the same directory first: theirs is identical (content addressed)
            shutil.rmtree(tmp, ignore_errors=True)
            if not final.is_dir():
                raise

    def lock(self, name: str):
        return file_lock(self.root / "locks" / f"{name}.lock")

    @staticmethod
    def touch(path: Path) -> None:
        with contextlib.suppress(OSError):
            os.utime(path, None)

    # ---------------------------------------------------------------- eviction
    def size_bytes(self) -> int:
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(base, name)).st_size
        return total

    def gc(self, max_bytes: int, min_age_s: float, now: Optional[float] = None) -> dict:
        """Free space down to max_bytes. Problem dirs go first (oldest use first), then jury binaries,
        then blobs no problem directory links to anymore. Anything used within min_age_s is kept."""
        now = time.time() if now is None else now
        removed = {"problems": 0, "jury": 0, "blobs": 0, "tmp": 0}
        # leftovers of crashed processes
        for tmp in list((self.root / "problems").glob(".tmp-*")) + list((self.root / "blobs").glob("*/.tmp-*")):
            with contextlib.suppress(OSError):
                if now - tmp.stat().st_mtime > 3600:
                    shutil.rmtree(tmp, ignore_errors=True) if tmp.is_dir() else tmp.unlink()
                    removed["tmp"] += 1
        if self.size_bytes() <= max_bytes:
            return removed

        def candidates(paths):
            items = []
            for p in paths:
                with contextlib.suppress(OSError):
                    mtime = p.stat().st_mtime
                    if now - mtime >= min_age_s:
                        items.append((mtime, p))
            return [p for _m, p in sorted(items)]

        for p in candidates(d for d in (self.root / "problems").iterdir() if d.is_dir() and not d.name.startswith(".")):
            shutil.rmtree(p, ignore_errors=True)
            removed["problems"] += 1
            if self.size_bytes() <= max_bytes:
                return removed
        for p in candidates(f for f in (self.root / "jury").iterdir() if f.is_file()):
            with contextlib.suppress(OSError):
                p.unlink()
            removed["jury"] += 1
            if self.size_bytes() <= max_bytes:
                return removed
        for p in candidates(f for sub in (self.root / "blobs").iterdir() if sub.is_dir() for f in sub.iterdir()
                            if f.is_file() and not f.name.startswith(".")):
            with contextlib.suppress(OSError):
                if p.stat().st_nlink > 1:
                    continue  # still linked from a problem directory that is in use
                p.unlink()
            removed["blobs"] += 1
            if self.size_bytes() <= max_bytes:
                return removed
        return removed


class DiskJuryCache:
    """Implements runner.jurycache.JuryCache on disk (compiled checkers and interactors)."""

    def __init__(self, cache: DiskCache) -> None:
        self.cache = cache
        self.dir = cache.root / "jury"

    def get(self, key: str) -> Optional[bytes]:
        if not valid_sha256(key):
            return None
        path, sum_path = self.dir / key, self.dir / f"{key}.sum"
        try:
            data = path.read_bytes()
            expected = sum_path.read_text().strip()
        except OSError:
            self.cache.counters.add("jury_misses")
            return None
        if hashlib.sha256(data).hexdigest() != expected:  # damaged file: drop it and rebuild
            for p in (path, sum_path):
                with contextlib.suppress(OSError):
                    p.unlink()
            self.cache.counters.add("jury_misses")
            return None
        self.cache.counters.add("jury_hits")
        DiskCache.touch(path)
        return data

    def put(self, key: str, data: bytes) -> None:
        if not valid_sha256(key):
            raise ValueError("bad jury cache key")
        _atomic_write(self.dir / f"{key}.sum", hashlib.sha256(data).hexdigest().encode())
        _atomic_write(self.dir / key, data)
        with contextlib.suppress(OSError):
            os.chmod(self.dir / key, 0o755)
