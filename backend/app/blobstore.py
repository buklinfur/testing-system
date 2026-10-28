"""Content-addressed file store on disk: <root>/ab/<sha256>. The database keeps only the hash."""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

HEX = set("0123456789abcdef")


def valid_sha256(value: str) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


class BlobStore:
    def __init__(self, root) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str) -> Path:
        if not valid_sha256(sha256):
            raise ValueError(f"bad sha256: {sha256!r}")
        return self.root / sha256[:2] / sha256

    def exists(self, sha256: str) -> bool:
        return self.path(sha256).is_file()

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self.path(sha)
        if dest.is_file():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".tmp-{os.getpid()}-{secrets.token_hex(4)}")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return sha

    def put_file(self, src) -> tuple[str, int]:
        """(sha256, size) of a file (a path or a binary file object), copied in chunks: never held in memory."""
        tmp = self.root / f".tmp-{os.getpid()}-{secrets.token_hex(4)}"
        h, size = hashlib.sha256(), 0
        source = open(src, "rb") if isinstance(src, (str, os.PathLike)) else src
        try:
            with open(tmp, "wb") as out:
                while block := source.read(1 << 20):
                    h.update(block)
                    size += len(block)
                    out.write(block)
            sha = h.hexdigest()
            dest = self.path(sha)
            if not dest.is_file():
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, dest)
            return sha, size
        finally:
            if source is not src:
                source.close()
            with contextlib.suppress(OSError):
                tmp.unlink()

    def get(self, sha256: str) -> Optional[bytes]:
        try:
            return self.path(sha256).read_bytes()
        except FileNotFoundError:
            return None

    def open(self, sha256: str) -> Optional[BinaryIO]:
        try:
            return open(self.path(sha256), "rb")
        except FileNotFoundError:
            return None

    def iter_chunks(self, sha256: str, chunk: int = 1 << 20) -> Iterator[bytes]:
        with open(self.path(sha256), "rb") as fh:
            while True:
                block = fh.read(chunk)
                if not block:
                    return
                yield block
