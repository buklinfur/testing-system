"""/api/admin submissions: lists with filters, the full teacher's view, the source, rejudges (docs/ADMIN_API.md).

The same services as `judgectl submission` (app/submissions/). Everything here is untrusted text from students
(the source, compile log, checker messages, output): the web shows it only as text (ADMIN_API.md, "Недоверенный
текст"); the source itself is never served as a page (content_files, MODULE_4A 5.7).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..blobstore import BlobStore
from ..content_files import file_response
from ..jsonable import plain
from ..models import Submission
from ..submissions import rejudge, view
from ..submissions.view import Filters, ViewError
from .deps import Page, admin_actor, page, paginate

router = APIRouter()

VERDICT = Literal["OK", "WA", "PE", "TL", "ML", "RE", "CE", "OL", "PARTIAL", "SKIPPED", "IE"]
STATUS = Literal["queued", "compiling", "running", "finished", "internal_error"]


def refused(exc: ViewError) -> ApiError:
    extra = {"affected": exc.affected} if hasattr(exc, "affected") else {}
    return ApiError(exc.status, exc.code, str(exc), **extra)


def blobs(request: Request) -> BlobStore:
    return BlobStore(request.app.state.settings.blob_dir)


class RejudgeBody(BaseModel):
    """What to rejudge (at least one of contest+label, contest_problem_id, problem_version, submission_ids), and
    either dry_run (count only) or expect (the count dry_run gave: refused with 409 if it changed)."""
    model_config = ConfigDict(extra="forbid")
    contest: Optional[str] = Field(None, max_length=64)
    label: Optional[str] = Field(None, max_length=8)
    contest_problem_id: Optional[int] = None
    problem_version: Optional[str] = Field(None, max_length=80, description="<slug>@<version>")
    submission_ids: Optional[list[int]] = Field(None, min_length=1, max_length=1000)
    verdicts: list[VERDICT] = Field(default_factory=list, description="only those whose current verdict is one of")
    user_id: Optional[int] = None
    group_id: Optional[int] = None
    dry_run: bool = False
    expect: Optional[int] = Field(None, ge=0)

    def filters(self) -> Filters:
        return Filters(contest=self.contest, label=self.label, contest_problem_id=self.contest_problem_id,
                       problem_version=self.problem_version, submission_ids=self.submission_ids,
                       verdicts=list(self.verdicts), user_id=self.user_id, group_id=self.group_id)


@router.get("/submissions")
def list_submissions(contest: Optional[str] = Query(None, max_length=64), label: Optional[str] = Query(None, max_length=8),
                     problem: Optional[str] = Query(None, max_length=64), user_id: Optional[int] = None,
                     group_id: Optional[int] = None, verdict: Optional[VERDICT] = None,
                     status: Optional[STATUS] = None, late: Optional[bool] = None, pg: Page = Depends(page),
                     db: Session = Depends(get_db)):
    """Newest first. verdict and status: of the current judgement; group_id: its current members."""
    f = Filters(contest=contest, label=label, problem=problem, user_id=user_id, group_id=group_id,
                verdicts=[verdict] if verdict else [], status=status, late=late)
    try:
        query = view.submissions_query(db, f)
    except ViewError as exc:
        raise refused(exc) from exc
    return paginate(db, query, Submission.id, pg, view.summary)


@router.post("/submissions/rejudge")
def rejudge_many(body: RejudgeBody, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """Two steps: {dry_run: true} answers {affected}; then the same filter with expect: affected does it."""
    if body.dry_run == (body.expect is not None):
        raise ApiError(422, "dry_run_or_expect", "send dry_run: true to count, then expect: <the count> to rejudge")
    f = body.filters()
    try:
        if body.dry_run:
            return {"affected": rejudge.count(db, f), "dry_run": True}
        result = rejudge.run(db, actor, f, now(request), expect=body.expect)
    except ViewError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return {**result, "dry_run": False}


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: int, request: Request, judgement_id: Optional[int] = None,
                   db: Session = Depends(get_db)):
    """The source (text up to 256 KB, or the names in an archive), every judgement, and every test of the current
    judgement (or of judgement_id), hidden tests included."""
    try:
        return plain(view.detail(db, blobs(request), submission_id, judgement_id))
    except ViewError as exc:
        raise refused(exc) from exc


@router.get("/submissions/{submission_id}/source")
def get_source(submission_id: int, request: Request, db: Session = Depends(get_db)):
    """A single file as text/plain, an archive as application/zip, always an attachment (never a page)."""
    try:
        sub = view.get_row(db, submission_id)[0]
    except ViewError as exc:
        raise refused(exc) from exc
    if sub.source_sha256 is None:
        raise ApiError(404, "no_source", "this submission has answers, not a source")
    name = view.source_name(sub)
    if sub.is_archive:
        return file_response(blobs(request), sub.source_sha256, name, media_type="application/zip", kind="attachment")
    return file_response(blobs(request), sub.source_sha256, name, text=True)


@router.post("/submissions/{submission_id}/rejudge")
def rejudge_one(submission_id: int, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    try:
        result = rejudge.one(db, actor, submission_id, now(request))
    except ViewError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return JSONResponse(plain(result))
