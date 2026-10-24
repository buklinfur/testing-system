"""Cache of compiled jury programs (checkers, interactors).

The runner only needs two operations, so the storage is pluggable:

* `MemoryJuryCache` (default): lives as long as the `Judge` object;
* `worker.cache.DiskJuryCache`: shared by all worker processes of a machine, survives restarts.

The key is derived from the *content* of the source, the language, the compile command and a
`toolchain_id` (for example a hash of `g++ --version`), so a changed compiler or changed flags never
reuse an old binary.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Optional, Protocol, Sequence


class JuryCache(Protocol):
    def get(self, key: str) -> Optional[bytes]: ...

    def put(self, key: str, data: bytes) -> None: ...


class MemoryJuryCache:
    def __init__(self) -> None:
        self._items: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            return self._items.get(key)

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._items[key] = data


def jury_key(source: bytes, language: str, compile_cmd: Optional[Sequence[str]], toolchain_id: str = "") -> str:
    digest = hashlib.sha256()
    for part in (language, repr(tuple(compile_cmd or ())), toolchain_id, hashlib.sha256(source).hexdigest()):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
