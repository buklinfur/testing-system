"""A problem of a contest for a student: its statement (of the version pinned now), examples, limits, what may be
sent, and the pictures of the statement (D-070, D-071)."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from protocol.problem_package import PICTURE_DIR

from .. import content_files, scores
from ..blobstore import BlobStore
from ..contests import access
from ..contests import service as contests
from ..models import ContestProblem, Language, ProblemVersion, Submission, TestCase, TestGroup, User, Visibility
from ..settings import SubmitSettings
from ..submissions import intake
from .common import Seen, StudentError, full_page, head_text, listed_problem, started

EXAMPLE_MAX = 64 * 1024


def statement_refusal(user: User, s: Seen, cp: ContestProblem, now: datetime, extensions) -> Optional[str]:
    """Why the statement is closed now (None: open): not_participant, press_start / not_started_yet, no_access,
    statement_hidden (after the due moment with after_due_statement hidden)."""
    if access.can_view_problem(user, s.contest, cp, s.participation, now, s.in_group, extensions):
        return None
    if s.participation is None:
        return "not_participant"
    if not s.has_access(user):
        return "no_access"
    if not started(s.contest, s.participation, now):
        personal = s.contest.timing_mode.value == "personal" and now >= s.contest.starts_at
        return "press_start" if personal else "not_started_yet"
    return "statement_hidden"


def open_problem(db: Session, user: User, slug: str, label: str, now: datetime) -> tuple:
    """(seen contest, contest problem, its pinned version, extensions) with the statement open, or the refusal."""
    s = full_page(db, user, slug, now)
    cp = listed_problem(db, user, s, label, now)
    extensions = contests.extensions_for(db, cp, user)
    refusal = statement_refusal(user, s, cp, now, extensions)
    if refusal:
        raise StudentError(refusal, "the statement is not open now")
    return s, cp, db.get(ProblemVersion, cp.problem_version_id), extensions


def enabled_languages(db: Session) -> list:
    return list(db.execute(select(Language.key, Language.name).where(Language.enabled.is_(True))
                           .order_by(Language.key)).all())


def quiz_items(version: ProblemVersion) -> Optional[list]:
    """The questions of a problem without code as a student sees them: never `correct`, `accept`, `match`, `eps` or
    `explanation`."""
    ptype = version.type.value
    if ptype == "choice":
        return [{"id": q.get("id"), "text": q.get("text"),
                 "options": [{"id": o.get("id"), "text": o.get("text")} for o in q.get("options") or []],
                 "points": q.get("points", 1)} for q in version.config.get("questions") or []]
    if ptype == "text_answer":
        return [{"id": a.get("id"), "text": a.get("text"), "points": a.get("points", 1)}
                for a in version.config.get("answers") or []]
    return None


def examples(db: Session, blobs: BlobStore, version: ProblemVersion) -> list:
    """Tests of the groups that are open and shown in the statement, in order; each text at most EXAMPLE_MAX."""
    out = []
    rows = db.execute(select(TestCase, TestGroup.name).join(TestGroup, TestGroup.id == TestCase.group_id)
                      .where(TestGroup.version_id == version.id, TestGroup.show_in_statement.is_(True),
                             TestGroup.visibility == Visibility.open)
                      .order_by(TestGroup.position, TestCase.position)).all()
    for case, group in rows:
        text_in, cut_in = _head(blobs, case.input_sha256)
        text_out, cut_out = _head(blobs, case.answer_sha256)
        out.append({"group": group, "name": case.name, "input": text_in, "answer": text_out,
                    "truncated": cut_in or cut_out})
    return out


def _head(blobs: BlobStore, sha: Optional[str]) -> tuple:
    return head_text(blobs, sha, EXAMPLE_MAX)


def problem_view(db: Session, user: User, blobs: BlobStore, settings: SubmitSettings, slug: str, label: str,
                 now: datetime) -> dict:
    s, cp, version, extensions = open_problem(db, user, slug, label, now)
    c, cfg = s.contest, version.config or {}
    sub = cfg.get("submission") or {}
    code = version.type.value not in intake.QUIZ_TYPES
    enabled = enabled_languages(db)
    allowed = intake.allowed_languages(c.languages, cfg.get("languages"), [key for key, _name in enabled])
    names = dict(enabled)
    last, counted = _my_counts(db, user, s, cp)
    cooldown_left = None
    if last is not None and c.submit_cooldown_sec:
        left = (last + timedelta(seconds=c.submit_cooldown_sec) - now).total_seconds()
        cooldown_left = max(0, int(left + 0.999)) or None
    out = {"label": cp.label, "type": version.type, "titles": version.titles, "statements": version.statements,
           "limits": cfg.get("limits") or {}, "note_md": cp.note_md,
           "max_score": scores.contest_max(scores.version_max_score(db, version.id), cp.points),
           "available_from": cp.available_from, "due_at": access.effective_due(cp, extensions),
           "after_due": {"statement": cp.after_due_statement, "submit": cp.after_due_submit},
           "examples": examples(db, blobs, version) if code else [],
           "mine": {"counted_submissions": counted, "limit": c.max_submissions_per_problem,
                    "cooldown_left_s": cooldown_left}}
    if code:
        out["languages"] = [{"key": key, "name": names[key]} for key in allowed]
        out["source_max_kb"] = settings.source_max_bytes // 1024
        out["archive"] = {"allowed": sub.get("allow_archive", True) is not False,
                          "max_kb": intake.archive_limit(sub, settings.archive_max_bytes) // 1024,
                          "max_files": sub.get("max_files", intake.ARCHIVE_MAX_FILES), "entry": sub.get("entry")}
    else:
        out["questions"] = quiz_items(version)
    return out


def _my_counts(db: Session, user: User, s: Seen, cp: ContestProblem) -> tuple:
    """(my last submission's moment, my submissions in my window: the ones the limit counts)."""
    last = db.execute(select(func.max(Submission.created_at)).where(
        Submission.user_id == user.id, Submission.contest_problem_id == cp.id)).scalar()
    window = access.contest_window_for(s.contest, s.participation)
    counted = contests.counted_submissions(db, cp.id, user.id, window.start, window.end) if window else 0
    return last, counted


def statement_file(db: Session, user: User, blobs: BlobStore, slug: str, label: str, path: str, now: datetime, *,
                   request):
    """A picture of the statement: only a file of statement/images/ of the pinned version, after the statement is
    open; anything else - a test, a checker, a path that is not there - is file_not_found."""
    _s, _cp, version, _ext = open_problem(db, user, slug, label, now)
    files = version.files or {}
    if not isinstance(path, str) or not path.startswith(PICTURE_DIR) or ".." in PurePosixPath(path).parts \
            or path not in files:
        raise StudentError("file_not_found", "no such picture in this statement")
    return content_files.file_response(blobs, files[path], PurePosixPath(path).name, request=request)
