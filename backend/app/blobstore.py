"""Content-addressed file store on disk: <root>/ab/<sha256>. The database keeps only the hash.

A blob is exactly a file <root>/<2 hex>/<64 hex>; anything else under the root is not one: <root>/tmp holds files
being received (Incoming), and put()/put_file() write .tmp-* files before renaming them. The blob collector of
module 6 counts only the first kind (and may remove stale files in <root>/tmp, left by a crash).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import BinaryIO, Iterator, Optional

HEX = set("0123456789abcdef")
TMP_DIR = "tmp"  # <root>/tmp: uploads being received, on the same file system as the blobs
HEAD_BYTES = 8192  # the first bytes an Incoming keeps (classroom/attachment_rules.HEAD_BYTES, a test compares)


class TooLarge(ValueError):
    """More bytes than the limit of an Incoming; the temporary file is removed when the Incoming is closed."""

    def __init__(self, limit: int):
        self.limit = limit
        super().__init__(f"the file is larger than {limit} bytes")


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

    def staging(self) -> Path:
        """<root>/tmp, created on demand: the directory of files being received."""
        path = self.root / TMP_DIR
        path.mkdir(exist_ok=True)
        return path

    def incoming(self, name: str = "", limit: Optional[int] = None) -> "Incoming":
        """A new file being received (see Incoming); use it in a with block."""
        return Incoming(self, name, limit)

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


class Incoming:
    """A file being received: written in chunks into <root>/tmp (the same file system as the blobs, so taking it
    into the store is one os.replace), its sha256, size and first HEAD_BYTES bytes computed on the way. Nothing is
    in the store until adopt(), which the services call only after every check passed. close() - also on leaving
    the with block - removes the temporary file unless it was adopted: a broken connection, a body over the limit
    and a refusal of the checks leave neither a blob nor a temporary file.

    `name` is the name the file was sent with (the services clean and check it); `limit`: TooLarge as soon as more
    bytes are written (None: no limit here, the caller limits)."""

    def __init__(self, store: BlobStore, name: str = "", limit: Optional[int] = None):
        self.store, self.name, self.limit = store, name, limit
        fd, path = tempfile.mkstemp(dir=store.staging(), prefix="in-")
        self.path = Path(path)
        self._fh: Optional[BinaryIO] = os.fdopen(fd, "wb")
        self._hash = hashlib.sha256()
        self.size = 0
        self.head = b""
        self.sha256: Optional[str] = None
        self.adopted = False

    # file-like: http_body.save_body writes into it
    def write(self, chunk: bytes) -> int:
        if self._fh is None:
            raise ValueError("the incoming file is finished or closed")
        if self.limit is not None and self.size + len(chunk) > self.limit:
            raise TooLarge(self.limit)
        self._hash.update(chunk)
        if len(self.head) < HEAD_BYTES:
            self.head += chunk[:HEAD_BYTES - len(self.head)]
        self.size += len(chunk)
        self._fh.write(chunk)
        return len(chunk)

    def flush(self) -> None:
        if self._fh is not None:
            self._fh.flush()

    def copy_from(self, source: BinaryIO, block: int = 1 << 20) -> "Incoming":
        """Write a whole binary file object (judgectl, dev_seed) and finish."""
        while chunk := source.read(block):
            self.write(chunk)
        return self.finish()

    def finish(self) -> "Incoming":
        """Everything is written: the file is closed and sha256 known."""
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self.sha256 = self._hash.hexdigest()
        return self

    def adopt(self) -> str:
        """Into the store as <root>/ab/<sha256> (a blob that is there already stays, the copy is removed); the sha.
        The database row (blobs) is the caller's."""
        if self.sha256 is None:
            raise ValueError("finish() the incoming file first")
        if not self.adopted:
            dest = self.store.path(self.sha256)
            if dest.is_file():
                with contextlib.suppress(OSError):
                    self.path.unlink()
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(self.path, dest)
            self.adopted = True
        return self.sha256

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if not self.adopted:
            with contextlib.suppress(OSError):
                self.path.unlink()

    def __enter__(self) -> "Incoming":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
