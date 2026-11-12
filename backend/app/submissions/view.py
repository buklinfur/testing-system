"""The admin view of submissions: one set of filters for lists and rejudges, the full view of one submission
(every test, hidden ones too, and every judgement), and its source. judgectl and /api/admin call only these.

The verdict and status of a submission are those of its current judgement (the one the student sees). A group is
its current members (as in the access rules and the leaderboard of 4b), not everybody who was ever in it.
Everything here is the teachers' view: never hand it to a student (explain.student_view / 4b do that).
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.orm import Session, aliased

from ..blobstore import BlobStore
from ..ids import parse_id
from ..models import (Contest, ContestProblem, Group, GroupMember, Judgement, JudgementStatus, Problem, ProblemVersion,
                      Submission, TestCase, TestGroup, TestResult, User, Verdict, Worker)

SOURCE_PREVIEW_MAX = 256 * 1024
SUFFIXES = {"python": ".py", "c": ".c", "cpp": ".cpp"}


class ViewError(ValueError):
    """A refusal with a stable `code` and an HTTP-like `status` for the API."""

    def __init__(self, message: str, code: str, status: int = 422):
        self.code, self.status = code, status
        super().__init__(message)


@dataclass
class Filters:
    contest: Optional[str] = None  # slug
    label: Optional[str] = None  # with contest: a problem of it that is not hidden
    contest_problem_id: Optional[int] = None
    problem: Optional[str] = None  # slug: any contest it is in
    problem_version: Optional[str] = None  # "<slug>@<n>": every contest problem pinned to it, hidden ones too
    submission_ids: Optional[list] = None
    user_id: Optional[int] = None
    group_id: Optional[int] = None  # current members of the group
    verdicts: list = field(default_factory=list)  # of the current judgement
    status: Optional[str] = None  # of the current judgement
    late: Optional[bool] = None

    def narrowing(self) -> bool:
        """Whether it names what to rejudge: a contest problem, a version or submissions (not just "everything")."""
        return bool((self.contest and self.label) or self.contest_problem_id or self.problem_version
                    or self.submission_ids)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "")}


CURRENT = aliased(Judgement)


def base_query():
    """(Submission, User, ContestProblem, Contest, Problem, ProblemVersion of the pin, current Judgement or None)."""
    return (select(Submission, User, ContestProblem, Contest, Problem, ProblemVersion, CURRENT)
            .join(User, User.id == Submission.user_id)
            .join(ContestProblem, ContestProblem.id == Submission.contest_problem_id)
            .join(Contest, Contest.id == ContestProblem.contest_id)
            .join(Problem, Problem.id == ContestProblem.problem_id)
            .join(ProblemVersion, ProblemVersion.id == ContestProblem.problem_version_id)
            .outerjoin(CURRENT, and_(CURRENT.submission_id == Submission.id, CURRENT.is_current.is_(True))))


def _version(db: Session, ref: str) -> ProblemVersion:
    slug, _, text = (ref or "").partition("@")
    number = parse_id(text)
    version = db.execute(select(ProblemVersion).join(Problem, Problem.id == ProblemVersion.problem_id)
                         .where(Problem.slug == slug, ProblemVersion.version == number)
                         ).scalar_one_or_none() if number is not None else None
    if version is None:
        raise ViewError(f"no problem version {ref!r} (write <slug>@<version>, e.g. sum@2)", "version_not_found", 404)
    return version


def apply_filters(db: Session, query, f: Filters):
    """The query narrowed by `f`; ViewError for a contest, problem, group or version that does not exist."""
    if f.label and not f.contest:
        raise ViewError("a label needs its contest", "label_needs_contest")
    if f.contest:
        contest = db.execute(select(Contest).where(Contest.slug == f.contest)).scalar_one_or_none()
        if contest is None:
            raise ViewError(f"no contest {f.contest!r}", "contest_not_found", 404)
        query = query.where(Contest.id == contest.id)
        if f.label:
            row = db.execute(select(ContestProblem).where(ContestProblem.contest_id == contest.id,
                                                          ContestProblem.label == f.label,
                                                          ContestProblem.hidden.is_(False))).scalar_one_or_none()
            if row is None:
                raise ViewError(f"contest {f.contest} has no problem {f.label!r}", "contest_problem_not_found", 404)
            query = query.where(ContestProblem.id == row.id)
    if f.contest_problem_id is not None:
        if db.get(ContestProblem, f.contest_problem_id) is None:
            raise ViewError(f"no contest problem {f.contest_problem_id}", "contest_problem_not_found", 404)
        query = query.where(ContestProblem.id == f.contest_problem_id)
    if f.problem:
        if db.execute(select(Problem.id).where(Problem.slug == f.problem)).first() is None:
            raise ViewError(f"no problem {f.problem!r}", "problem_not_found", 404)
        query = query.where(Problem.slug == f.problem)
    if f.problem_version:
        query = query.where(ContestProblem.problem_version_id == _version(db, f.problem_version).id)
    if f.submission_ids:
        query = query.where(Submission.id.in_(list(f.submission_ids)))
    if f.user_id is not None:
        query = query.where(Submission.user_id == f.user_id)
    if f.group_id is not None:
        if db.get(Group, f.group_id) is None:
            raise ViewError(f"no group {f.group_id}", "group_not_found", 404)
        query = query.where(Submission.user_id.in_(select(GroupMember.user_id).where(
            GroupMember.group_id == f.group_id, GroupMember.removed_at.is_(None))))
    if f.verdicts:
        try:
            verdicts = [Verdict(v.strip().upper()) for v in f.verdicts]
        except ValueError as exc:
            raise ViewError(f"unknown verdict in {f.verdicts}", "verdict_invalid") from exc
        query = query.where(CURRENT.verdict.in_(verdicts))
    if f.status:
        try:
            query = query.where(CURRENT.status == JudgementStatus(f.status))
        except ValueError as exc:
            raise ViewError(f"unknown status {f.status!r}", "status_invalid") from exc
    if f.late is not None:
        query = query.where(Submission.late.is_(f.late))
    return query


def submissions_query(db: Session, f: Filters):
    return apply_filters(db, base_query(), f)


def summary(row) -> dict:
    sub, user, cp, contest, problem, version, current = row
    return {"id": sub.id, "user": {"id": user.id, "login": user.login, "display_name": user.display_name},
            "contest": contest.slug, "label": cp.label, "problem": problem.slug, "version": version.version,
            "language": sub.language_key, "archive": sub.is_archive, "created_at": sub.created_at, "late": sub.late,
            "judgement": None if current is None else {"id": current.id, "status": current.status,
                                                       "verdict": current.verdict, "score": current.score}}


# ----------------------------------------------------------------------------- one submission
def source_name(sub: Submission) -> str:
    """The name the source is served under: the archive as a zip, a single file by its language."""
    if sub.is_archive:
        return f"submission-{sub.id}.zip"
    return f"submission-{sub.id}{SUFFIXES.get(sub.language_key or '', '.txt')}"


def source_preview(blobs: BlobStore, sub: Submission) -> Optional[dict]:
    """A single file: its text (UTF-8, undecodable bytes replaced) up to 256 KB. An archive: the names in it.
    The text is the student's own: untrusted, shown only as text (docs/ADMIN_API.md, "Недоверенный текст")."""
    if sub.source_sha256 is None:
        return None
    if not blobs.exists(sub.source_sha256):
        return {"name": source_name(sub), "missing": True}
    data = blobs.get(sub.source_sha256)
    out = {"name": source_name(sub), "size": len(data), "missing": False}
    if sub.is_archive:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                out["files"] = [{"name": i.filename, "size": i.file_size} for i in zf.infolist() if not i.is_dir()]
        except zipfile.BadZipFile:
            out["files"], out["unreadable"] = [], True
        out["entry_file"] = sub.entry_file
        return out
    out["truncated"] = len(data) > SOURCE_PREVIEW_MAX
    out["text"] = data[:SOURCE_PREVIEW_MAX].decode("utf-8", errors="replace")
    return out


def tests_of(db: Session, judgement_id: int) -> list[dict]:
    """Every test result of a judgement, hidden tests included (the admin view)."""
    rows = db.execute(select(TestResult, TestCase.name, TestGroup.name)
                      .join(TestCase, TestCase.id == TestResult.test_case_id)
                      .join(TestGroup, TestGroup.id == TestCase.group_id)
                      .where(TestResult.judgement_id == judgement_id).order_by(TestResult.id)).all()
    return [{"test": name, "group": group, "verdict": r.verdict, "time_ms": r.time_ms, "memory_kb": r.memory_kb,
             "exit_code": r.exit_code, "signal": r.signal, "tl_kind": r.tl_kind,
             "visible_to_student": r.visible_to_student, "checker_message": r.checker_message,
             "stderr_excerpt": r.stderr_excerpt, "output_excerpt": r.output_excerpt} for r, name, group in rows]


def judgements_of(db: Session, submission_id: int) -> list[dict]:
    requester = aliased(User)
    rows = db.execute(select(Judgement, ProblemVersion.version, Worker.name, requester.login)
                      .join(ProblemVersion, ProblemVersion.id == Judgement.problem_version_id)
                      .outerjoin(Worker, Worker.id == Judgement.worker_id)
                      .outerjoin(requester, requester.id == Judgement.requested_by)
                      .where(Judgement.submission_id == submission_id).order_by(Judgement.id)).all()
    return [{"id": j.id, "reason": j.reason, "status": j.status, "verdict": j.verdict, "score": j.score,
             "version": version, "current": j.is_current, "attempt": j.attempt, "failures": j.failures,
             "priority": j.priority, "worker": worker, "requested_by": who, "created_at": j.created_at,
             "started_at": j.started_at, "finished_at": j.finished_at, "tests_done": j.tests_done,
             "tests_total": j.tests_total, "compile_log": j.compile_log, "error": j.error, "summary": j.summary}
            for j, version, worker, who in rows]


def get_row(db: Session, submission_id: int):
    row = db.execute(base_query().where(Submission.id == submission_id)).first()
    if row is None:
        raise ViewError(f"no submission {submission_id}", "submission_not_found", 404)
    return row


def detail(db: Session, blobs: BlobStore, submission_id: int, judgement_id: Optional[int] = None) -> dict:
    """Everything about one submission for a teacher: the source (or the names in an archive), every judgement
    (history), and every test of the current judgement, or of `judgement_id` (one of this submission)."""
    row = get_row(db, submission_id)
    sub = row[0]
    out = summary(row)
    out.update({"entry_file": sub.entry_file, "ip": sub.ip_address, "answers": sub.answers,
                "source": source_preview(blobs, sub), "judgements": judgements_of(db, sub.id)})
    shown = judgement_id if judgement_id is not None else next((j["id"] for j in out["judgements"] if j["current"]),
                                                               None)
    if judgement_id is not None and judgement_id not in {j["id"] for j in out["judgements"]}:
        raise ViewError(f"submission {submission_id} has no judgement {judgement_id}", "judgement_not_found", 404)
    out["tests_of"] = shown
    out["tests"] = tests_of(db, shown) if shown is not None else []
    return out
