"""/api/contests/{slug}/problems/{label} of a student: the statement, its pictures and sending a submission
(docs/STUDENT_API.md §6).

A submission is the raw body, its kind by Content-Type: text/plain - one source file (?language=), application/zip -
an archive (?language=, ?entry=), application/json - {"answers": {...}} of a problem without code; anything else
415 before a byte is read. The route is exempt from the 1 MB JSON limit by its path pattern (routes.UPLOAD_PATTERNS)
and reads the body itself, in this order:
  the Origin (the public app), the student session and its CSRF token, the Content-Type (415), a declared
  Content-Length above the bound of the kind (413), the cheap refusals of the services (submissions.precheck: the
  contest, the problem, the kind, the language, access.can_submit) - then the transaction is rolled back, so no
  transaction (and no lock) is held while the body arrives - the body, streamed and cut at the limit of this problem
  (413 as soon as it passes it, with or without Content-Length), and only then submissions.submit in a new
  transaction: the intake, the rate, the decision again, the blob, the row, the queue.
`now` is the moment the request was received, taken once before anything else: a slow upload never becomes late.
413 of a source or an archive is source_too_large (the intake's code), of answers too_large.
"""
from __future__ import annotations

import io
import json
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..api_errors import ApiError
from ..auth_api import Current, client_ip, get_db, now
from ..http_body import declared_length, save_body
from ..jsonable import plain
from ..student import problems as sp
from ..student import submissions as sv
from ..student.common import StudentError
from .deps import blobs, refused, student_session
from .types import Label, Slug

router = APIRouter()

KIND_OF_TYPE = {"text/plain": "source", "application/zip": "archive", "application/json": "answers"}

SUBMIT_DOC = {
    "requestBody": {"required": True, "content": {
        "text/plain": {"schema": {"type": "string", "format": "binary"}},
        "application/zip": {"schema": {"type": "string", "format": "binary"}},
        "application/json": {"schema": {"type": "object", "additionalProperties": False, "required": ["answers"],
                                         "properties": {"answers": {"type": "object"}}}},
    }},
}


@router.get("/contests/{slug}/problems/{label}")
def get_problem(slug: Slug, label: Label, request: Request, db: Session = Depends(get_db),
                cur: Current = Depends(student_session)):
    """The statement of the version pinned now, open examples, limits, languages, and the student's own counts.
    404 contest_not_found / problem_not_found; 403 no_access; 409 not_participant, press_start, not_started_yet,
    statement_hidden."""
    settings = request.app.state.settings
    try:
        return plain(sp.problem_view(db, cur.user, blobs(request), settings.submit, slug, label, now(request)))
    except StudentError as exc:
        raise refused(exc) from exc


@router.get("/contests/{slug}/problems/{label}/files/{path:path}")
def get_statement_file(slug: Slug, label: Label, path: str, request: Request, db: Session = Depends(get_db),
                       cur: Current = Depends(student_session)):
    """A picture of the statement (statement/images/...), after the statement is open; ETag and 304 by If-None-Match.
    Anything else of the package is 404 file_not_found."""
    try:
        return sp.statement_file(db, cur.user, blobs(request), slug, label, path, now(request), request=request)
    except StudentError as exc:
        raise refused(exc) from exc


def _kind(request: Request) -> str:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    kind = KIND_OF_TYPE.get(content_type)
    if kind is None:
        raise ApiError(415, "unsupported_media_type", "send text/plain (a source file), application/zip (an archive) "
                                                      "or application/json ({answers})")
    return kind


def _too_large(kind: str, limit: int) -> ApiError:
    code = "too_large" if kind == "answers" else "source_too_large"
    return ApiError(413, code, f"the body is larger than {max(1, limit // 1024)} KB")


def _answers(raw: bytes):
    try:
        body = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ApiError(422, "invalid_request", problems=["body: not JSON"]) from exc
    if not isinstance(body, dict) or set(body) != {"answers"}:
        raise ApiError(422, "invalid_request", problems=["body: an object with the one key answers"])
    return body["answers"]


@router.post("/contests/{slug}/problems/{label}/submissions", status_code=201, openapi_extra=SUBMIT_DOC)
async def submit(slug: Slug, label: Label, request: Request,
                 language: Optional[str] = Query(None, max_length=32, description="text/plain and application/zip"),
                 entry: Optional[str] = Query(None, max_length=1000, description="application/zip: the file to run"),
                 db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """One submission (see the module docstring). 201 {id, judgement_id, counted, late}. 415 another Content-Type;
    413 source_too_large / too_large; 422 the intake (language_not_allowed, source_encoding, archive_invalid, ...);
    409 the state (not_participant, submission_limit, deadline_passed, contest_over, ...); 429 rate_limited, cooldown
    with Retry-After."""
    at = now(request)
    kind = _kind(request)
    settings = request.app.state.settings.submit
    if kind != "archive" and entry is not None or kind == "answers" and language is not None:
        raise ApiError(422, "invalid_request", problems=["entry is only for an archive, language only for a program"])
    bound = {"source": settings.source_max_bytes, "archive": settings.archive_max_bytes,
             "answers": sv.ANSWERS_MAX_BYTES}[kind]
    declared = declared_length(request.scope.get("headers", []))
    if declared is not None and declared > bound:
        raise _too_large(kind, bound)

    def precheck() -> int:
        try:
            return sv.precheck(db, cur.user, settings, slug, label, at, kind=kind, language=language)
        except StudentError as exc:
            raise refused(exc) from exc
        finally:
            db.rollback()  # no transaction (and no lock) is held while the body is received

    limit = await run_in_threadpool(precheck)
    buffer = io.BytesIO()
    try:
        await save_body(request, buffer, limit)
    except ApiError as exc:
        if exc.status == 413:
            raise _too_large(kind, limit) from exc
        raise
    raw = buffer.getvalue()
    payload = {"answers": _answers(raw)} if kind == "answers" else {kind: raw, "language": language}
    if kind == "archive":
        payload["entry"] = entry

    def work() -> dict:
        try:
            made = sv.submit(db, cur.user, blobs(request), settings, slug, label, at, ip=client_ip(request), **payload)
        except StudentError as exc:
            db.rollback()
            raise refused(exc) from exc
        db.commit()
        return made

    return plain(await run_in_threadpool(work))
