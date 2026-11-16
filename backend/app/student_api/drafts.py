"""/api/feed/{item}/work and /api/feed/{item}/draft of a student: the hand-in, the files of the own work, the draft
uploaded one file at a time (docs/STUDENT_API.md §6, D-101; the rules: docs/CLASSROOM.md §10). The services are
app/student/classroom.py over classroom/drafts.py and works.py. An item that is not a manual assignment is 409
not_an_assignment on every route here, reading included.

The upload of a draft file is the raw body (Content-Type application/octet-stream), its name in ?name= (UTF-8,
percent-encoded, decoded strictly: http_body.upload_name). The route is exempt from the 1 MB JSON limit by its path
pattern (routes.UPLOAD_PATTERNS) and reads the body itself, in this order (D-082, D-095):
  the Origin (the public app), the student session and its CSRF token, the Content-Type (415), ?name= (422), a
  declared Content-Length above JUDGE_SUBMISSION_FILE_MAX_MB (413 file_too_large), the cheap refusals of the services
  (drafts.precheck: the item, can_submit_work, the draft's count, the name and its type, a repeated name, the quota
  by the declared length) - then the transaction is rolled back, so no transaction and no lock is held while the
  body arrives - the body, streamed into <blob root>/tmp (blobstore.Incoming) and cut at the limit (413
  file_too_large, with or without Content-Length), and only then drafts.add_file in a new transaction, which decides
  everything again under the student's lock. A refused or broken upload leaves nothing in the store or its tmp.
`now` is the moment the request was received, taken once before anything else.

The hand-in is JSON {text?, files: [ids of draft files, in their order], links: [{url, title?}]}: the named draft
files become the hand-in's (the rest of the draft stays), an id that is not a live file of this student's draft of
this item is 404 draft_file_not_found.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..api_errors import ApiError
from ..auth_api import Current, client_ip, get_db, now
from ..classroom import attachments
from ..classroom.items import FeedError
from ..content_files import file_response
from ..http_body import declared_length, require_type, save_body, upload_name
from ..ids import Id, StrictId
from ..jsonable import plain
from ..models import SubmissionFileKind
from ..student import classroom as sc
from .deps import blobs, refused, student_session

router = APIRouter()

UPLOAD_TYPE = "application/octet-stream"
UPLOAD_DOC = {
    "parameters": [{"name": "name", "in": "query", "required": True, "schema": {"type": "string"},
                    "description": "the file name with its extension, UTF-8 percent-encoded (encodeURIComponent)"}],
    "requestBody": {"required": True, "content": {UPLOAD_TYPE: {"schema": {"type": "string", "format": "binary"}}}},
}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LinkIn(Body):
    """Limits of the schema above those of the rules (2000 and 200 characters): a longer value is link_invalid /
    link_title_invalid from the rules, not invalid_request."""
    url: StrictStr = Field(max_length=10_000)
    title: Optional[StrictStr] = Field(None, max_length=1_000)


class HandInBody(Body):
    """Limits of the schema above those of the rules (JUDGE_ASSIGNMENT_TEXT_MAX_KB, JUDGE_SUBMISSION_FILES_MAX): more
    is text_too_long / too_many_files from the rules."""
    text: Optional[StrictStr] = Field(None, max_length=1_000_000)
    files: list[StrictId] = Field(default_factory=list, max_length=200)
    links: list[LinkIn] = Field(default_factory=list, max_length=200)


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "file_too_large", f"a file is at most {max(1, limit // (1024 * 1024))} MB")


# ----------------------------------------------------------------------------- the work
@router.post("/feed/{item_id}/work", status_code=201)
def hand_in(item_id: Id, body: HandInBody, request: Request, db: Session = Depends(get_db),
            cur: Current = Depends(student_session)):
    """A hand-in: the text, the draft files named by id (they leave the draft) and the links. 201 {submission: {id,
    at, late}, work}. 404 item_not_found, draft_file_not_found; 403 no_access; 409 not_an_assignment, item_archived,
    deadline_passed, already_graded, quota_exceeded; 422 answer_required, text_not_expected, text_too_long,
    files_required, files_not_expected, too_many_files, submission_too_large, duplicate_name, link_invalid, ..."""
    at = now(request)
    settings = request.app.state.settings
    try:
        out = sc.hand_in(db, cur.user, item_id, at, blobs=blobs(request), text=body.text, draft_ids=body.files,
                         links=body.links, text_limit=settings.assignment_text_max_bytes,
                         limits=attachments.hand_in_limits(settings.files), quota=settings.files.student_quota_bytes,
                         ip=client_ip(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(out)


@router.get("/feed/{item_id}/work/files/{file_id}")
def get_work_file(item_id: Id, file_id: Id, request: Request, db: Session = Depends(get_db),
                  cur: Current = Depends(student_session)):
    """A file of one's own hand-in (the ids are in the page of the item: hand_ins[].files); the text answer, when
    asked by its id, as text. 404 file_not_found: another student's, another item's, missing."""
    try:
        row = sc.work_file(db, cur.user, item_id, file_id, now(request))
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.path, text=row.kind == SubmissionFileKind.text,
                         request=request)


# ----------------------------------------------------------------------------- the draft
@router.get("/feed/{item_id}/draft")
def get_draft(item_id: Id, request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The live files of the own draft in the order of upload, each with expires_at (the whole draft goes then)."""
    try:
        out = sc.draft(db, cur.user, item_id, now(request), request.app.state.settings.files.draft_ttl_days)
    except FeedError as exc:
        raise refused(exc) from exc
    return plain({"draft": out})


@router.post("/feed/{item_id}/draft/files", status_code=201, openapi_extra=UPLOAD_DOC)
async def upload_draft_file(item_id: Id, request: Request, db: Session = Depends(get_db),
                            cur: Current = Depends(student_session)):
    """One more file of the draft (see the module docstring). 201 {file, draft}. 415 another Content-Type; 422
    invalid_request (no name, twice, broken UTF-8), file_type_not_allowed, file_content_mismatch, file_empty,
    duplicate_name, too_many_files, submission_too_large; 413 file_too_large; 409 quota_exceeded and the refusals of
    a hand-in now (deadline_passed, already_graded, files_not_expected, ...)."""
    at = now(request)
    require_type(request, UPLOAD_TYPE)
    name = upload_name(request)
    files = request.app.state.settings.files
    limit, limits, quota = files.submission_file_max_bytes, attachments.hand_in_limits(files), files.student_quota_bytes
    declared = declared_length(request.scope.get("headers", []))
    if declared is not None and declared > limit:
        raise _too_large(limit)

    def precheck():
        try:
            return sc.precheck_upload(db, cur.user, item_id, name, at, limits=limits, quota=quota, declared=declared)
        except FeedError as exc:
            raise refused(exc) from exc
        finally:
            db.rollback()  # no transaction (and no lock) is held while the body is received

    item = await run_in_threadpool(precheck)
    with blobs(request).incoming(name) as upload:
        try:
            await save_body(request, upload, limit)
        except ApiError as exc:
            if exc.status == 413:
                raise _too_large(limit) from exc
            raise

        def work():
            try:
                out = sc.add_draft_file(db, cur.user, item, upload, at, limits=limits, quota=quota,
                                        ttl_days=files.draft_ttl_days)
            except FeedError as exc:
                db.rollback()
                raise refused(exc) from exc
            db.commit()
            return plain(out)

        return await run_in_threadpool(work)


@router.get("/feed/{item_id}/draft/files/{file_id}")
def get_draft_file(item_id: Id, file_id: Id, request: Request, db: Session = Depends(get_db),
                   cur: Current = Depends(student_session)):
    """A live file of one's own draft of this item. 404 draft_file_not_found: another student's, another item's,
    deleted, missing."""
    try:
        row = sc.draft_file(db, cur.user, item_id, file_id, now(request))
    except FeedError as exc:
        raise refused(exc) from exc
    return file_response(blobs(request), row.blob_sha256, row.name, request=request)


@router.post("/feed/{item_id}/draft/files/{file_id}/delete", status_code=204)
def delete_draft_file(item_id: Id, file_id: Id, request: Request, db: Session = Depends(get_db),
                      cur: Current = Depends(student_session)):
    """Delete a file of the draft: its name is free at once; its bytes stay in the quota until the draft expires.
    404 draft_file_not_found."""
    try:
        sc.remove_draft_file(db, cur.user, item_id, file_id, now(request))
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)


@router.post("/feed/{item_id}/draft/from-last")
def draft_from_last(item_id: Id, request: Request, db: Session = Depends(get_db),
                    cur: Current = Depends(student_session)):
    """The files of the last own hand-in copied into the draft (the same bytes: no upload, no quota; a name the draft
    has is skipped). 200 {draft}; with no hand-in nothing changes, expires_at included. 409/422 as an upload."""
    files = request.app.state.settings.files
    try:
        out = sc.draft_from_last(db, cur.user, item_id, now(request), limits=attachments.hand_in_limits(files),
                                 ttl_days=files.draft_ttl_days)
    except FeedError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(out)
