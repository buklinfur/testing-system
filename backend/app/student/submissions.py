"""Submissions of a student: sending one (the one path, D-041, now with the intake and the rate of 4b.1), their own
list, the view of one (submissions/student_view.py) and its source.

Order of submit (STUDENT_API.md): the cheap refusals of access first (precheck, then the caller rolls back and reads
the body with no transaction open), then the body's intake (pure), then the lock SUBMIT_RATE of the student and the
rate, then service.submission_decision (it locks the participation row: always after the rate lock), then the blob,
the row and the queue. A refusal stores nothing: no blob, no row.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, exists, select
from sqlalchemy.orm import Session, aliased

from .. import content_files, locks, queue, rates, scores
from ..blobstore import BlobStore
from ..classroom.attachments import ensure_blob
from ..contests import access
from ..contests import service as contests
from ..ids import DB_ID_MAX
from ..models import (Contest, ContestProblem, Judgement, JudgementStatus, Participation, ProblemVersion, Submission,
                      TestCase, TestGroup, TestResult, User)
from ..settings import SubmitSettings
from ..submissions import intake, student_view, view
from ..submissions.intake import IntakeError
from .common import StudentError, full_page, head_text, listed_problem, require_student, seen
from .problems import enabled_languages

CONTENT_MAX = student_view.EXCERPT_MAX
LIST_MAX = 100
ACTIVE = (JudgementStatus.queued, JudgementStatus.compiling, JudgementStatus.running)


def _target(db: Session, user: User, slug: str, label: str, now: datetime) -> tuple:
    s = full_page(db, user, slug, now)
    cp = listed_problem(db, user, s, label, now)
    return s, cp, db.get(ProblemVersion, cp.problem_version_id)


def _refuse(decision) -> None:
    if not decision:
        raise StudentError(decision.reason, f"the submission is not accepted: {decision.reason}",
                           decision.retry_after_s)


def precheck(db: Session, user: User, slug: str, label: str, now: datetime, *, language: Optional[str] = None,
             answers: bool = False) -> None:
    """The cheap refusals before the body is read: the contest and the problem are there, the kind of body fits the
    problem, the language is allowed, and access.can_submit says yes now. The caller rolls back afterwards (the
    participation row was locked)."""
    s, cp, version = _target(db, user, slug, label, now)
    try:
        if answers:
            intake.check_answers(version.type, version.config, {})
        else:
            intake.check_code_problem(version.type)
            _language(db, s.contest, version, language)
    except IntakeError as exc:
        raise StudentError(exc.code, str(exc)) from exc
    _refuse(contests.submission_decision(db, user, s.contest, cp, now, language=language))


def _language(db: Session, contest: Contest, version: ProblemVersion, language: Optional[str]) -> str:
    allowed = intake.allowed_languages(contest.languages, (version.config or {}).get("languages"),
                                       [key for key, _name in enabled_languages(db)])
    return intake.check_language(language, allowed)


def submit(db: Session, user: User, blobs: BlobStore, settings: SubmitSettings, slug: str, label: str, now: datetime,
           *, language: Optional[str] = None, source: Optional[bytes] = None, archive: Optional[bytes] = None,
           entry: Optional[str] = None, answers=None, ip: Optional[str] = None) -> dict:
    """One submission: exactly one of `source` (a single file), `archive` (a zip) or `answers` (a problem without
    code). `now` is the moment the request was received. Returns {id, judgement_id, counted, late}."""
    if sum(x is not None for x in (source, archive, answers)) != 1:
        raise StudentError("invalid_request", "send a source, an archive or answers")
    s, cp, version = _target(db, user, slug, label, now)
    try:
        if answers is not None:
            checked = intake.check_answers(version.type, version.config, answers)
            body, language = None, None
        else:
            intake.check_code_problem(version.type)
            language = _language(db, s.contest, version, language)
            body = intake.check_source(source, settings.source_max_bytes) if source is not None else \
                intake.check_archive(archive, language, entry, (version.config or {}).get("submission"),
                                     settings.archive_max_bytes)
            checked = None
    except IntakeError as exc:
        raise StudentError(exc.code, str(exc)) from exc

    locks.take_pair(db, locks.SUBMIT_RATE, user.id)  # then the participation row (submission_decision)
    moments = db.execute(select(Submission.created_at).where(
        Submission.user_id == user.id, Submission.created_at > now - rates.SUBMIT_WINDOW)).scalars().all()
    rate = rates.window_ok(moments, now, settings.rate_per_min, rates.SUBMIT_WINDOW)
    if not rate:
        raise StudentError("rate_limited", "too many submissions in a minute", rate.retry_after_s)
    decision = contests.submission_decision(db, user, s.contest, cp, now, language=language)
    _refuse(decision)

    sha = None
    if body is not None:
        sha = blobs.put(body.data)
        ensure_blob(db, sha, len(body.data))
    sub = Submission(user_id=user.id, contest_problem_id=cp.id, language_key=language, source_sha256=sha,
                     is_archive=bool(body and body.is_archive), entry_file=body.entry if body else None,
                     answers=checked, ip_address=ip, created_at=now, late=decision.late)
    db.add(sub)
    db.flush()
    j = queue.enqueue(db, sub)
    return {"id": sub.id, "judgement_id": j.id, "counted": decision.counted, "late": decision.late}


# ----------------------------------------------------------------------------- reading one's own
CURRENT = aliased(Judgement)


def _counted(contest: Contest, participation: Optional[Participation], at: datetime) -> bool:
    window = access.contest_window_for(contest, participation)
    return window is not None and window.contains(at)


def _short(sub: Submission, cp: ContestProblem, contest: Contest, part: Optional[Participation],
           j: Optional[Judgement]) -> dict:
    status = student_view.status_of(j) if j is not None else "queued"
    done = status == "done"
    return {"id": sub.id, "contest": contest.slug, "label": cp.label, "language": sub.language_key,
            "archive": sub.is_archive, "created_at": sub.created_at, "late": sub.late,
            "counted": _counted(contest, part, sub.created_at), "status": status,
            "verdict": j.verdict if done else None,
            "score": scores.contest_score(j.score, j.max_score, cp.points) if done else None,
            "max_score": scores.contest_max(j.max_score if j is not None else None, cp.points)}


def my_submissions(db: Session, user: User, now: datetime, *, contest: Optional[str] = None,
                   label: Optional[str] = None, cursor: Optional[int] = None, limit: int = 50) -> dict:
    """The student's own submissions, newest first, by a cursor (an id). With `contest`: that contest (it must be
    one the student sees, also only for their results); `label` narrows it to one problem."""
    require_student(user)
    if label is not None and contest is None:
        raise StudentError("label_needs_contest", "a label needs its contest")
    query = (select(Submission, ContestProblem, Contest, Participation, CURRENT)
             .join(ContestProblem, ContestProblem.id == Submission.contest_problem_id)
             .join(Contest, Contest.id == ContestProblem.contest_id)
             .outerjoin(Participation, and_(Participation.contest_id == Contest.id,
                                            Participation.user_id == Submission.user_id))
             .outerjoin(CURRENT, and_(CURRENT.submission_id == Submission.id, CURRENT.is_current.is_(True)))
             .where(Submission.user_id == user.id))
    if contest is not None:
        query = query.where(Contest.id == seen(db, user, contest, now).contest.id)
        if label is not None:
            query = query.where(ContestProblem.label == label)
    if cursor is not None:
        query = query.where(Submission.id < cursor)
    limit = max(1, min(limit, LIST_MAX))
    rows = db.execute(query.order_by(Submission.id.desc()).limit(limit + 1)).all()
    items = []
    for sub, cp, c, part, j in rows[:limit]:
        if access.can_see_own_results(user, c, part):
            items.append(_short(sub, cp, c, part, j))
    return {"items": items, "next_cursor": rows[limit - 1][0].id if len(rows) > limit else None}


def _own(db: Session, user: User, submission_id: int) -> tuple:
    require_student(user)
    if not isinstance(submission_id, int) or not 1 <= submission_id <= DB_ID_MAX:
        raise StudentError("submission_not_found", "no such submission")
    row = db.execute(select(Submission, ContestProblem, Contest, Participation)
                     .join(ContestProblem, ContestProblem.id == Submission.contest_problem_id)
                     .join(Contest, Contest.id == ContestProblem.contest_id)
                     .outerjoin(Participation, and_(Participation.contest_id == Contest.id,
                                                    Participation.user_id == Submission.user_id))
                     .where(Submission.id == submission_id, Submission.user_id == user.id)).first()
    if row is None or not access.can_see_own_results(user, row[2], row[3]):
        raise StudentError("submission_not_found", "no such submission")  # another student's: the same answer
    return tuple(row)


def _levels(db: Session, version: ProblemVersion) -> list:
    """The problem level and every group override of a version (student_view.effective_policy's pinned_levels)."""
    overrides = db.execute(select(TestGroup.feedback).where(TestGroup.version_id == version.id,
                                                            TestGroup.feedback.is_not(None))).scalars().all()
    return [(version.config or {}).get("feedback")] + list(overrides)


def my_submission(db: Session, user: User, blobs: BlobStore, submission_id: int, now: datetime) -> dict:
    """The student's view of one of their submissions: the current judgement (with `rechecking` while a rejudge
    runs; no history), shown by student_view under effective_policy (the version it was judged on, capped by the
    version pinned now)."""
    sub, cp, contest, part = _own(db, user, submission_id)
    j = db.execute(select(Judgement).where(Judgement.submission_id == sub.id, Judgement.is_current.is_(True))
                   ).scalar_one_or_none()
    rechecking = db.execute(select(exists().where(Judgement.submission_id == sub.id,
                                                  Judgement.is_current.is_(False),
                                                  Judgement.status.in_(ACTIVE)))).scalar()
    out = {"id": sub.id, "contest": contest.slug, "label": cp.label, "language": sub.language_key,
           "archive": sub.is_archive, "entry_file": sub.entry_file, "answers": sub.answers,
           "created_at": sub.created_at, "has_source": sub.source_sha256 is not None}
    counted = _counted(contest, part, sub.created_at)
    if j is None:
        out.update({"status": "queued", "counted": counted, "late": sub.late, "rechecking": rechecking,
                    "score": None, "max_score": None})
        return out
    judged = db.get(ProblemVersion, j.problem_version_id)
    pinned = db.get(ProblemVersion, cp.problem_version_id)
    groups = [(name, vis, fb) for name, vis, fb in db.execute(
        select(TestGroup.name, TestGroup.visibility, TestGroup.feedback).where(TestGroup.version_id == judged.id)
        .order_by(TestGroup.position)).all()]
    judged_feedback = (judged.config or {}).get("feedback")
    pinned_levels = _levels(db, pinned)
    same = judged.id == pinned.id
    open_now = access.visible_tests(user, contest, now, [(name, vis) for name, vis, _fb in groups])
    policy = student_view.effective_policy(judged_feedback, groups, pinned_levels, same, open_now)
    results = db.execute(select(TestResult, TestCase, TestGroup.name)
                         .join(TestCase, TestCase.id == TestResult.test_case_id)
                         .join(TestGroup, TestGroup.id == TestCase.group_id)
                         .where(TestResult.judgement_id == j.id).order_by(TestResult.id)).all()
    rows, contents = [], {}
    for r, case, group in results:
        rows.append({"group": group, "test_case_id": case.id, "verdict": r.verdict, "time_ms": r.time_ms,
                     "memory_kb": r.memory_kb, "exit_code": r.exit_code, "signal": r.signal, "tl_kind": r.tl_kind,
                     "checker_message": r.checker_message, "output_excerpt": r.output_excerpt})
        rule = policy.get(group)
        if rule is not None and rule.visibility == "open":  # read only what may be shown
            contents[case.id] = {"input": head_text(blobs, case.input_sha256, CONTENT_MAX)[0],
                                 "answer": head_text(blobs, case.answer_sha256, CONTENT_MAX)[0]}
    out.update(student_view.student_view(
        j, results=rows, policy=policy,
        problem_feedback=student_view.effective_feedback(judged_feedback, pinned_levels), contents=contents,
        points=cp.points, counted=counted, late=sub.late, rechecking=rechecking))
    return out


def my_source(db: Session, user: User, blobs: BlobStore, submission_id: int, *, request):
    """The source of one's own submission (a single file as text, an archive as a zip)."""
    sub, _cp, _contest, _part = _own(db, user, submission_id)
    if sub.source_sha256 is None:
        raise StudentError("file_not_found", "this submission has answers, not a source")
    return content_files.file_response(blobs, sub.source_sha256, view.source_name(sub), text=not sub.is_archive,
                                       media_type="application/zip" if sub.is_archive else None, request=request)
