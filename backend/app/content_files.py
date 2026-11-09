"""Answers with files people uploaded (problem package files, submission sources, later statement pictures and
attachments): never a page a browser runs. The rules are docs/FILES.md (D-070); D-017 and D-026 stand.

* The type comes from the content, not only the name: the first HEAD_BYTES of the blob are read (BlobStore.open,
  never the whole file) and sniffed. A picture is shown inline only when its bytes are PNG, JPEG or WebP **and** its
  name says a picture; the media type is the one of the bytes (a .jpg holding a PNG is image/png). Picture bytes under
  another name, a picture name over other bytes (HTML in .png), SVG, HTML, GIF - application/octet-stream as an
  attachment.
* Text (tests, sources, statements) is text/plain; charset=utf-8 as an attachment only if the head has no NUL and is
  valid UTF-8 (a sequence cut at the end of the head is fine); anything else - cp1251 included - is
  application/octet-stream as an attachment.
* An archive is application/zip only with the zip signature, else application/octet-stream.
* Every answer, 200 and 304 alike: X-Content-Type-Options: nosniff, Content-Security-Policy: sandbox;
  default-src 'none', Cache-Control: private, no-cache, ETag: "<sha256>", Content-Disposition (RFC 6266: the base
  name only, cleaned, an ASCII fallback in filename= and the full name in filename*=UTF-8'').
* If-None-Match (RFC 9110: a list of tags, weak comparison, *) gives 304. file_response is called only after the
  endpoint has found the file for this user, so a 304 never says anything to someone without access.
* The body is streamed in chunks (BlobStore.iter_chunks), never read whole.

The caller finds the file by its name in the stored list (version `files`), never by a path on disk.
"""
from __future__ import annotations

import codecs
import re
import unicodedata
from pathlib import PurePosixPath
from typing import Optional
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from .api_errors import ApiError
from .auth.logins import TRANSLIT
from .blobstore import BlobStore

HEAD_BYTES = 8192
NAME_MAX = 150
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
IMAGE_MEDIA = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
TEXT_SUFFIXES = frozenset({".in", ".out", ".ans", ".txt", ".md", ".py", ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
                           ".hh", ".inc", ".yaml", ".yml", ".csv", ".json"})
TEXT = ("text/plain; charset=utf-8", "attachment")
OCTET = ("application/octet-stream", "attachment")
ZIP = ("application/zip", "attachment")
SAFETY = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'",
          "Cache-Control": "private, no-cache"}
ENTITY_TAG = re.compile(r'(?:W/)?"([^"]*)"')


# ----------------------------------------------------------------------------- what the bytes are
def sniff(head: bytes) -> Optional[str]:
    """"png" / "jpeg" / "webp" / "zip" by the signature at the start of the file, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    return None


def looks_like_text(head: bytes, complete: bool = False) -> bool:
    """No NUL and valid UTF-8. `complete`: the head is the whole file; otherwise a multi-byte sequence cut by the end
    of the head is fine (the rest is in the next bytes)."""
    if b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=complete)
    except UnicodeDecodeError:
        return False
    return True


def decide(name: str, head: bytes, *, text: bool = False, archive: bool = False,
           complete: bool = False) -> tuple[str, str]:
    """(media type, disposition) of a file from its name and its first bytes - a pure function (docs/FILES.md).
    text=True: a submission's source (text whatever its name); archive=True: a submission's zip."""
    kind = sniff(head)
    if archive:
        return ZIP if kind == "zip" else OCTET
    suffix = PurePosixPath(name).suffix.lower()
    if kind in IMAGE_MEDIA:
        if suffix in IMAGE_SUFFIXES and not text:
            return IMAGE_MEDIA[kind], "inline"
        return OCTET  # picture bytes under another name: never shown, never text
    if text or suffix in TEXT_SUFFIXES:
        return TEXT if looks_like_text(head, complete) else OCTET
    return OCTET


def kind_of(name: str, text: bool = False) -> tuple[str, str]:
    """The rule by the name alone, before the bytes are known (the bytes can only make it stricter)."""
    suffix = PurePosixPath(name).suffix.lower()
    if text or suffix in TEXT_SUFFIXES:
        return TEXT
    if suffix in IMAGE_SUFFIXES:
        return {".png": "image/png", ".webp": "image/webp"}.get(suffix, "image/jpeg"), "inline"
    return OCTET


# ----------------------------------------------------------------------------- the name in Content-Disposition
def clean_name(name: str) -> str:
    """The base name a browser may save the file as: no path, no control or format characters (direction marks
    included), no quote, backslash or semicolon, at most NAME_MAX characters (the extension kept), never empty."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C" and ch not in '";')
    base = base.strip().strip(".")
    if not base:
        return "file"
    if len(base) > NAME_MAX:
        stem, dot, ext = base.rpartition(".")
        ext = dot + ext if dot and 0 < len(ext) <= 10 else ""
        base = (stem if ext else base)[:NAME_MAX - len(ext)].rstrip() + ext
    return base


def ascii_name(base: str) -> str:
    """The filename= fallback: Russian letters transliterated, other characters outside [A-Za-z0-9._-] as _."""
    out = []
    for ch in base:
        low = ch.lower()
        if low in TRANSLIT:
            part = TRANSLIT[low]
            out.append(part.capitalize() if ch != low and part else part)
        elif ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append("_")
    result = re.sub(r"_{2,}", "_", "".join(out)).strip("_")
    if not result.strip("."):
        return "file"
    return "file" + result if result.startswith(".") else result  # "🙂.png" -> file.png, not a hidden .png


def disposition(kind: str, name: str) -> str:
    base = clean_name(name)
    return f"{kind}; filename=\"{ascii_name(base)}\"; filename*=UTF-8''{quote(base, safe='')}"


# ----------------------------------------------------------------------------- the answer
def etag_matches(header: Optional[str], tag: str) -> bool:
    """If-None-Match (RFC 9110 13.1.2): * or a list of entity tags, compared weakly (W/ ignored)."""
    if not header:
        return False
    if header.strip() == "*":
        return True
    return tag in ENTITY_TAG.findall(header)


def file_response(blobs: BlobStore, sha256: str, name: str, text: bool = False, media_type: str | None = None,
                  kind: str | None = None, request: Optional[Request] = None) -> Response:
    """Stream a stored blob under the rules above. media_type="application/zip" asks for a submission's archive
    (confirmed by the signature); `kind` may only make the answer stricter (attachment). With `request`,
    If-None-Match is honoured (304 with the same headers). The caller has already checked access."""
    head_file = blobs.open(sha256)
    if head_file is None:
        raise ApiError(404, "file_missing", "the file is recorded but missing from the blob store "
                                            "(judgectl status --details)")
    with head_file:
        head = head_file.read(HEAD_BYTES)
        complete = len(head) < HEAD_BYTES or not head_file.read(1)
    media, disp = decide(name, head, text=text, archive=media_type == "application/zip", complete=complete)
    if kind == "attachment":
        disp = "attachment"
    headers = {**SAFETY, "ETag": f'"{sha256}"', "Content-Disposition": disposition(disp, name)}
    if request is not None and etag_matches(request.headers.get("if-none-match"), sha256):
        return Response(status_code=304, headers={**headers, "Content-Type": media})
    return StreamingResponse(blobs.iter_chunks(sha256), media_type=media, headers=headers)
