"""Answers with files people uploaded (problem package files, submission sources): never a page a browser runs.

MODULE_4A 5.7, required for the admin API and for statement pictures in 4b:
* always `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox; default-src 'none'`;
* only pictures (png, jpeg, webp: D-026, one rule for statements and attachments) are shown inline, the type
  taken from the file name, never sniffed; a GIF is an attachment like anything else;
* tests and sources are `text/plain; charset=utf-8` as an attachment; anything else, SVG and HTML included, is
  `application/octet-stream` as an attachment.
The caller finds the file by its name in the stored list (version `files`), never by a path on disk.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi.responses import StreamingResponse

from .api_errors import ApiError
from .blobstore import BlobStore

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
TEXT_SUFFIXES = frozenset({".in", ".out", ".ans", ".txt", ".md", ".py", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
                           ".hh", ".inc", ".yaml", ".yml", ".csv", ".json"})
SAFETY = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"}


def kind_of(name: str, text: bool = False) -> tuple[str, str]:
    """(media type, disposition) for a file name; text=True forces text/plain (a submission's source)."""
    suffix = PurePosixPath(name).suffix.lower()
    if text:
        return "text/plain; charset=utf-8", "attachment"
    if suffix in IMAGE_TYPES:
        return IMAGE_TYPES[suffix], "inline"
    if suffix in TEXT_SUFFIXES:
        return "text/plain; charset=utf-8", "attachment"
    return "application/octet-stream", "attachment"


def disposition(kind: str, name: str) -> str:
    base = PurePosixPath(name).name or "file"
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', "_", base) or "file"
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(base, safe='')}"


def file_response(blobs: BlobStore, sha256: str, name: str, text: bool = False,
                  media_type: str | None = None, kind: str | None = None) -> StreamingResponse:
    """Stream a stored blob under the rules above (media_type/kind override them only for archives: a zip)."""
    if not blobs.exists(sha256):
        raise ApiError(404, "file_missing", "the file is recorded but missing from the blob store "
                                            "(judgectl status --details)")
    auto_type, auto_kind = kind_of(name, text)
    return StreamingResponse(blobs.iter_chunks(sha256), media_type=media_type or auto_type,
                             headers={**SAFETY, "Content-Disposition": disposition(kind or auto_kind, name)})
