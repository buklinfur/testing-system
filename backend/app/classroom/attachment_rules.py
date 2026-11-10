"""Files and links of the class feed (4v.3, docs/CLASSROOM.md §10): pure functions.

Two kinds of attachments:
* a student's hand-in on a manual assignment (answer_kind files / text_or_files) - files and links next to the
  text answer (answer.md);
* a teacher's attachments of a feed item (the statement of an assignment, materials of a contest item).

No database, no clock of their own, no reading of files: the service passes the name, the size and the first
HEAD_BYTES of every file (the head is enough for the signatures), the counts and sums it reads under its locks, and
`now`. Visibility of the item is decided by classroom/access.py, dates by app/deadline_rules.py.

Types are a whitelist by the last suffix of the name, lower case (ALLOWED). A name without a suffix is refused;
so is `run.py.exe` (.exe is not in the list), while `отчёт.v2.pdf` passes. The content is checked by signature:
* text and sources - nothing (a cp1251 source from Windows is fine; docs/FILES.md serves such a file as a binary
  attachment anyway);
* zip-based formats (zip, docx, xlsx, pptx, odt, ods, odp, sb3) - a local file header PK\\x03\\x04 (an empty zip,
  PK\\x05\\x06, is refused);
* pdf - %PDF-;
* pictures - the PNG / JPEG / WebP signature that matches the suffix (.png - PNG, .jpg/.jpeg - JPEG, .webp - WebP).
  Stricter than serving (D-070 serves a .jpg holding a PNG as image/png): at upload the student can still save it
  again. SVG, HTML, executables and everything else are not in the list.

Links are data: the server never opens them (no preview, no SSRF). Only http and https with a host, no user:pass@,
no whitespace or control characters, at most LINK_URL_MAX characters.

A refusal is a Decision with a stable code; when several conditions fail, the first in the documented order gives
it. check_hand_in and check_new_attachment also say which file or link (`Refusal.index`).
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional, Sequence
from urllib.parse import urlsplit

from ..deadline_rules import Decision
from . import access

HEAD_BYTES = 8192  # == content_files.HEAD_BYTES: the service reads this much of every file
NAME_MAX = 150  # == content_files.NAME_MAX: longer names would be cut in Content-Disposition
LINK_URL_MAX = 2000
LINK_TITLE_MAX = 200

MIB = 1024 * 1024
# the defaults of the settings (the service passes the configured values)
SUBMISSION_FILES_MAX = 10  # JUDGE_SUBMISSION_FILES_MAX: files and links of one hand-in (answer.md not counted)
SUBMISSION_FILE_MAX_BYTES = 20 * MIB  # JUDGE_SUBMISSION_FILE_MAX_MB
SUBMISSION_TOTAL_MAX_BYTES = 50 * MIB  # JUDGE_SUBMISSION_TOTAL_MAX_MB
STUDENT_QUOTA_BYTES = 200 * MIB  # JUDGE_STUDENT_QUOTA_MB: distinct blobs of all the student's hand-ins
ITEM_ATTACHMENTS_MAX = 20  # JUDGE_ITEM_ATTACHMENTS_MAX: files and links of one feed item
ATTACHMENT_MAX_BYTES = 50 * MIB  # JUDGE_ATTACHMENT_MAX_MB: one file of a teacher

TEXT, ZIP, PDF = "text", "zip", "pdf"
# the whitelist: suffix -> what its content must be ("png" / "jpeg" / "webp": that picture signature)
ALLOWED = {
    **{s: TEXT for s in (".py", ".c", ".cpp", ".h", ".hpp", ".txt", ".md", ".csv", ".json", ".ipynb")},
    **{s: ZIP for s in (".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".sb3")},
    ".pdf": PDF,
    ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".webp": "webp",
}
SEPARATORS = frozenset("/\\")
# a URL is ASCII-printable or letters of a non-ASCII host / path; none of these categories belongs in it
URL_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})
ZERO_WIDTH_JOINER = "\u200d"  # inside emoji sequences: the one format character a title may hold

OK = Decision(True)


def _no(code: str) -> Decision:
    return Decision(False, code)


@dataclass(frozen=True)
class Refusal:
    """A refused list: the code and the position of the file or link it is about (None: the list as a whole)."""
    code: str
    index: Optional[int] = None

    def __bool__(self) -> bool:
        return False


@dataclass(frozen=True)
class Limits:
    files_max: int = SUBMISSION_FILES_MAX  # files and links together
    file_max_bytes: int = SUBMISSION_FILE_MAX_BYTES
    total_max_bytes: Optional[int] = SUBMISSION_TOTAL_MAX_BYTES  # None: no sum limit (a teacher's attachments)


TEACHER_LIMITS = Limits(files_max=ITEM_ATTACHMENTS_MAX, file_max_bytes=ATTACHMENT_MAX_BYTES, total_max_bytes=None)


# --------------------------------------------------------------------------- what the bytes are
def signature(head: bytes) -> Optional[str]:
    """"png" / "jpeg" / "webp" / "zip" / "zip-empty" / "pdf" by the start of the file, or None. The first four agree
    with content_files.sniff (a unit test keeps them in step)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"PK\x05\x06"):
        return "zip-empty"
    if head.startswith(b"%PDF-"):
        return "pdf"
    return None


def suffix_of(name: str) -> str:
    """The last suffix, lower case, or "" (no dot, or a name like ".py" or "notes.")."""
    stem, dot, ext = name.rpartition(".")
    return "." + ext.lower() if dot and stem and ext else ""


# --------------------------------------------------------------------------- one file
def check_name(name) -> Decision:
    """The name a file is stored and served under (the service has already cut it to its base name with
    content_files.clean_name). file_name_invalid: empty, only dots or spaces, a path separator, a control or format
    character (NUL, line breaks, direction marks) or a lone surrogate, longer than NAME_MAX characters.
    file_type_not_allowed: no suffix, or a suffix not in ALLOWED. Cyrillic and other letters are fine."""
    if not isinstance(name, str) or not name.strip(" .") or len(name) > NAME_MAX:
        return _no("file_name_invalid")
    if any(ch in SEPARATORS or unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in name):
        return _no("file_name_invalid")
    if name != name.strip():
        return _no("file_name_invalid")
    return OK if suffix_of(name) in ALLOWED else _no("file_type_not_allowed")


def check_content(name: str, head: bytes) -> Decision:
    """The first bytes against the suffix (the name has passed check_name): file_content_mismatch otherwise."""
    expected = ALLOWED[suffix_of(name)]
    if expected == TEXT:
        return OK
    return OK if signature(head) == expected else _no("file_content_mismatch")


def check_file(name, size: int, head: bytes, max_bytes: int) -> Decision:
    """One file, in this order: file_name_invalid, file_type_not_allowed, file_too_large (more than max_bytes),
    file_content_mismatch."""
    named = check_name(name)
    if not named:
        return named
    if size > max_bytes:
        return _no("file_too_large")
    return check_content(name, head)


def same_name(a: str, b: str) -> bool:
    """Two names of one hand-in or one item may not differ only by case: they would overwrite each other when saved
    on Windows."""
    return a.casefold() == b.casefold()


# --------------------------------------------------------------------------- one link
def check_link(url, title=None) -> Decision:
    """A link as data (the server never opens it). `url` is taken after stripping the whitespace around it (the
    service stores it so). link_invalid: not http or https (any case), no host, user:pass@ in it, a backslash,
    whitespace or a control / format character inside, longer than LINK_URL_MAX. An @ in the path is fine
    (youtube.com/@channel); in the host part it is user:pass@ (https://good.ru@evil.example goes to evil.example). link_title_invalid: a title (it
    may be None or blank) longer than LINK_TITLE_MAX characters or with control characters, format characters other
    than the zero width joiner of emoji, or a lone surrogate."""
    if not isinstance(url, str):
        return _no("link_invalid")
    url = url.strip()
    if not url or len(url) > LINK_URL_MAX or "\\" in url:
        return _no("link_invalid")
    if any(unicodedata.category(ch) in URL_BAD_CATEGORIES for ch in url):
        return _no("link_invalid")
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a port that is not a number
    except ValueError:
        return _no("link_invalid")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or "@" in parts.netloc:
        return _no("link_invalid")
    if not url[len(parts.scheme):].startswith("://"):
        return _no("link_invalid")
    return check_link_title(title)


def check_link_title(title) -> Decision:
    if title is None:
        return OK
    if not isinstance(title, str) or len(title) > LINK_TITLE_MAX:
        return _no("link_title_invalid")
    if any(unicodedata.category(ch) in ("Cc", "Cs") or (unicodedata.category(ch) == "Cf" and ch != ZERO_WIDTH_JOINER)
           for ch in title):
        return _no("link_title_invalid")
    return OK


# --------------------------------------------------------------------------- a list of files and links
def check_hand_in(files: Sequence, links: Sequence = (), limits: Limits = Limits()):
    """The files (objects with name, size, head) and links (url, title) of one hand-in, in this order - the first
    failure wins:
    1. too_many_files - more than limits.files_max files and links together;
    2. every file in its order: check_file (file_name_invalid, file_type_not_allowed, file_too_large,
       file_content_mismatch);
    3. duplicate_name - a later file with the name of an earlier one (case ignored);
    4. submission_too_large - the sizes together exceed limits.total_max_bytes;
    5. every link in its order: check_link (link_invalid, link_title_invalid).
    Returns OK or a Refusal (code, index of the file or link)."""
    if len(files) + len(links) > limits.files_max:
        return Refusal("too_many_files")
    return _check_list(files, links, limits, existing_names=())


def check_new_attachment(existing_names: Iterable[str], existing_count: int, *, file=None, link=None,
                         limits: Limits = TEACHER_LIMITS):
    """One more attachment of a feed item (a file: name, size, head; or a link: url, title) next to the
    `existing_count` it has, files named `existing_names`. Codes: too_many_attachments, then those of check_file
    with limits.file_max_bytes, duplicate_name, or those of check_link. OK or a Refusal (index 0)."""
    if (file is None) == (link is None):
        raise ValueError("one file or one link")
    if existing_count + 1 > limits.files_max:
        return Refusal("too_many_attachments")
    return _check_list([file] if file is not None else [], [link] if link is not None else [], limits,
                       existing_names=existing_names)


def _check_list(files, links, limits: Limits, *, existing_names):
    for i, f in enumerate(files):
        d = check_file(f.name, f.size, f.head, limits.file_max_bytes)
        if not d:
            return Refusal(d.reason, i)
    seen = list(existing_names)
    for i, f in enumerate(files):
        if any(same_name(f.name, other) for other in seen):
            return Refusal("duplicate_name", i)
        seen.append(f.name)
    if limits.total_max_bytes is not None and sum(f.size for f in files) > limits.total_max_bytes:
        return Refusal("submission_too_large")
    for i, link in enumerate(links):
        d = check_link(link.url, getattr(link, "title", None))
        if not d:
            return Refusal(d.reason, i)
    return OK


def quota_ok(used: int, adding: int, quota: Optional[int] = STUDENT_QUOTA_BYTES) -> Decision:
    """The student's quota: `used` - the sizes of the distinct blobs in all their hand-ins; `adding` - the sizes of
    the blobs of the new hand-in they do not have yet (the same file again costs nothing). Up to the quota inclusive;
    a hand-in that adds nothing new always passes (also when the quota was lowered below `used`); quota None or 0:
    no limit. quota_exceeded otherwise."""
    return _no("quota_exceeded") if quota and adding and used + adding > quota else OK


# --------------------------------------------------------------------------- who sees and who changes them
def can_view_item_files(user, item, now: datetime, *, in_audience: bool, extensions: Iterable[datetime] = (),
                        contest=None, participation=None, in_contest_group: bool = False) -> bool:
    """The teacher's files and links of an item are part of its page: a student sees them where they see the
    statement of an assignment (access.can_view_assignment: hidden with it after the due moment with
    after_due_statement = hidden, also for a student with work), or a contest item in their feed
    (access.item_listed). Admins see everything. The student's own hand-in files are their work
    (access.can_see_own_work), not this."""
    if access.is_admin(user):
        return True
    kind = getattr(item.kind, "value", item.kind)
    if kind == "assignment":
        return access.can_view_assignment(user, item, now, in_audience, extensions)
    if kind == "contest":
        return access.item_listed(user, item, now, in_audience, contest=contest, participation=participation,
                                  in_contest_group=in_contest_group)
    return False


def can_change_attachments(item) -> Decision:
    """A teacher adds, removes and reorders attachments of a draft or a published item; an archived one is read
    only (item_archived)."""
    return _no("item_archived") if getattr(item.state, "value", item.state) == "archived" else OK
