"""What a student sends, checked before anything is stored (4b.1, D-091): pure functions, no database, no clock.

The service of stage 5 calls them after the cheap refusals of access (contests/access.can_submit) and before the
rate, the lock and the blob; a refusal is an IntakeError with a stable code for the API (422, except 413 for sizes).
The runner keeps its own limits (runner/archive.py) as a safety net; the server never takes more than it would.

* language: one of the contest's languages ∩ the problem's ∩ the enabled ones (None of a list = no narrowing);
* a single file: at most JUDGE_SOURCE_MAX_KB; strictly UTF-8 (a BOM is removed, line ends are kept as they are);
  not empty (only whitespace counts as empty) - `source_encoding` asks to save the file in UTF-8;
* an archive: only when the problem allows it (submission.allow_archive, default yes); at most
  min(JUDGE_ARCHIVE_MAX_KB, submission.max_archive_kb); a zip with files, at most submission.max_files (20), names
  a relative path without `..`, no symlinks, no encryption, no name twice (after the single top folder is stripped,
  as the runner does). Names and contents are kept as they are. An interpreted language runs its entry file (the one
  named, else submission.entry, else main.py), which must be in it; a named entry must be in it for any language;
* answers of a problem without code (choice / text_answer): an object of known question ids; choice - a list of
  distinct option ids of that question, text - a string of at most ANSWER_MAX_CHARS.
"""
from __future__ import annotations

import io
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, Optional

from protocol.languages import ENTRY_DEFAULTS

# what the runner takes at most (runner/config.py; tests/test_intake_units.py keeps them equal)
RUNNER_SOURCE_MAX_BYTES = 256 * 1024
RUNNER_ARCHIVE_MAX_BYTES = 1024 * 1024
ARCHIVE_MAX_FILES = 20  # submission.max_files of a problem, by default (protocol/schemas/problem.schema.json)
ANSWER_MAX_CHARS = 1000
ENTRY_MAX_CHARS = 255
BOM = b"\xef\xbb\xbf"
QUIZ_TYPES = ("choice", "text_answer")

# code -> HTTP status of the student API (stage 6); every code is raised below
CODES = {"language_not_allowed": 422, "archive_not_allowed": 422, "archive_invalid": 422, "archive_too_many_files": 422,
         "entry_not_found": 422, "entry_invalid": 422, "source_too_large": 413, "source_empty": 422,
         "source_encoding": 422, "answers_invalid": 422, "answers_expected": 422, "source_expected": 422}


class IntakeError(ValueError):
    def __init__(self, code: str, message: str):
        assert code in CODES, code
        self.code, self.status = code, CODES[code]
        super().__init__(message)


@dataclass(frozen=True)
class Source:
    """A submission ready to be stored: the bytes of the blob (a single file without its BOM, or the archive as it
    is), whether it is an archive and the entry file to record (None: the language's default applies)."""
    data: bytes
    is_archive: bool
    entry: Optional[str] = None


def _v(value) -> str:
    return getattr(value, "value", value)


def allowed_languages(contest_languages: Optional[Iterable[str]], problem_languages: Optional[Iterable[str]],
                      enabled: Iterable[str]) -> list:
    """The languages a student may use for a problem of a contest, in the order of `enabled`."""
    out = list(enabled)
    for narrowing in (contest_languages, problem_languages):
        if narrowing is not None:
            keep = set(narrowing)
            out = [key for key in out if key in keep]
    return out


def check_language(language: Optional[str], allowed: Iterable[str]) -> str:
    if not isinstance(language, str) or language not in set(allowed):
        raise IntakeError("language_not_allowed", f"language {language!r} is not allowed here")
    return language


def check_source(data: bytes, max_bytes: int) -> Source:
    """A single source file. The size is checked on what arrived (the BOM counts)."""
    if len(data) > max_bytes:
        raise IntakeError("source_too_large", f"the source is larger than {max_bytes // 1024} KB")
    if data.startswith(BOM):
        data = data[len(BOM):]
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise IntakeError("source_encoding", "the source is not UTF-8: save the file in UTF-8") from None
    if not text.strip():
        raise IntakeError("source_empty", "the source is empty")
    return Source(data, is_archive=False)


def archive_limit(problem_submission: Optional[dict], server_max_bytes: int) -> int:
    kb = (problem_submission or {}).get("max_archive_kb")
    return min(server_max_bytes, kb * 1024) if isinstance(kb, int) and kb > 0 else server_max_bytes


def _name_ok(name: str) -> Optional[PurePosixPath]:
    if not name or "\x00" in name or "\\" in name or re.match(r"^[A-Za-z]:", name):
        return None
    path = PurePosixPath(name)
    return None if path.is_absolute() or ".." in path.parts else path


def check_archive(data: bytes, language: str, entry: Optional[str], problem_submission: Optional[dict],
                  server_max_bytes: int) -> Source:
    """A zip archive, by the rules of the module docstring. Nothing is unpacked: only the directory is read."""
    sub = problem_submission or {}
    if sub.get("allow_archive", True) is False:
        raise IntakeError("archive_not_allowed", "this problem takes a single file, not an archive")
    limit = archive_limit(sub, server_max_bytes)
    if len(data) > limit:
        raise IntakeError("source_too_large", f"the archive is larger than {limit // 1024} KB")
    if entry is not None and (not isinstance(entry, str) or not entry or len(entry) > ENTRY_MAX_CHARS
                              or _name_ok(entry) is None):
        raise IntakeError("entry_invalid", "the entry file must be a relative path inside the archive")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, NotImplementedError, EOFError):
        raise IntakeError("archive_invalid", "this is not a zip archive that can be read") from None
    if not infos:
        raise IntakeError("archive_invalid", "the archive has no files")
    max_files = sub.get("max_files") if isinstance(sub.get("max_files"), int) else ARCHIVE_MAX_FILES
    if len(infos) > max_files:
        raise IntakeError("archive_too_many_files", f"the archive has more than {max_files} files")
    parts = []
    for info in infos:
        path = _name_ok(info.filename)
        if path is None:
            raise IntakeError("archive_invalid", "the archive has a file outside of it (an absolute path or ..)")
        if info.flag_bits & 0x1:
            raise IntakeError("archive_invalid", "the archive is encrypted")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise IntakeError("archive_invalid", "the archive has a symbolic link")
        parts.append(path.parts)
    strip = 1 if all(len(p) > 1 for p in parts) and len({p[0] for p in parts}) == 1 else 0
    names = ["/".join(p[strip:]) for p in parts]
    if len(set(names)) != len(names):
        raise IntakeError("archive_invalid", "a file name is in the archive twice")
    needed = entry if entry is not None else (sub.get("entry") or ENTRY_DEFAULTS.get(language))
    if needed is not None and needed not in names:
        raise IntakeError("entry_not_found", f"the archive has no file {needed}")
    return Source(data, is_archive=True, entry=entry)


def _storable(text: str) -> bool:
    """PostgreSQL keeps neither NUL nor a lone surrogate (which JSON may carry) in jsonb."""
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_answers(problem_type, config: dict, answers) -> dict:
    """The answers of a problem without code, by its questions (config: problem.yaml without its tests)."""
    ptype = _v(problem_type)
    if ptype not in QUIZ_TYPES:
        raise IntakeError("source_expected", "this problem takes a program, not answers")
    if not isinstance(answers, dict):
        raise IntakeError("answers_invalid", "answers are an object {question id: answer}")
    items = {q.get("id"): q for q in (config or {}).get("questions" if ptype == "choice" else "answers") or []}
    for qid, given in answers.items():
        item = items.get(qid)
        if item is None:
            raise IntakeError("answers_invalid", f"no question {qid!r}")
        if ptype == "choice":
            options = {o.get("id") for o in item.get("options") or []}
            if not isinstance(given, list) or len(set(map(str, given))) != len(given) \
                    or any(not isinstance(o, str) or o not in options for o in given):
                raise IntakeError("answers_invalid", f"question {qid}: a list of distinct options of the question")
        elif not isinstance(given, str) or len(given) > ANSWER_MAX_CHARS or not _storable(given):
            raise IntakeError("answers_invalid", f"question {qid}: a text of at most {ANSWER_MAX_CHARS} characters")
    return answers


def check_code_problem(problem_type) -> None:
    """A program is sent to a problem that takes one."""
    if _v(problem_type) in QUIZ_TYPES:
        raise IntakeError("answers_expected", "this problem takes answers, not a program")
