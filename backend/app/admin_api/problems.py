"""/api/admin problems: upload a package, the catalog, one version, its files, archiving (docs/ADMIN_API.md).

The upload goes through problems.importer.import_package with a file, like `judgectl problem import`; the lists through
problems.catalog, like `judgectl problem list|versions|show`.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from protocol.problem_package import PackageError

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..blobstore import BlobStore
from ..content_files import file_response
from ..http_body import require_type, save_body
from ..jsonable import plain
from .. import problem_checks as checks
from ..models import Problem
from ..problems import catalog
from ..problems.importer import import_package
from .deps import Page, admin_actor, page, paginate

router = APIRouter()
STATUS = {"problem_not_found": 404, "version_not_found": 404, "problem_archived": 409}
RECHECK_CODES = {"created": 202, "already_queued": 200, "already_running": 200}


def _problem(db: Session, slug: str) -> Problem:
    try:
        return catalog.get_problem(db, slug)
    except catalog.CatalogError as exc:
        raise ApiError(404, exc.code, str(exc)) from exc


def _version(db: Session, problem: Problem, number: int):
    try:
        return catalog.get_version(db, problem, number)
    except catalog.CatalogError as exc:
        raise ApiError(404, exc.code, str(exc)) from exc


@router.post("/problems", status_code=201)
async def upload(request: Request, dry_run: bool = False, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    """The body is the zip of a package (application/zip). 201 created; 200 exists (the same content) or dry run."""
    require_type(request, "application/zip")
    settings = request.app.state.settings
    with tempfile.TemporaryDirectory(prefix="judge-upload-") as tmp:
        path = Path(tmp) / "package.zip"
        with open(path, "wb") as out:
            await save_body(request, out, settings.imports.max_archive_bytes)
        at = now(request)

        def work():
            try:
                result = import_package(db, BlobStore(settings.blob_dir), settings.imports, actor, at,
                                        archive=path, source_name="upload", dry_run=dry_run)
            except PackageError as exc:
                db.rollback()
                raise ApiError(STATUS.get(exc.code, 422), exc.code, "the package was not imported",
                               problems=exc.problems) from exc
            db.commit()
            return result

        result = await run_in_threadpool(work)
    body = plain({"slug": result.slug, "result": result.kind, "version": result.number,
                  "content_sha256": result.content_sha256, "warnings": result.warnings, "dry_run": dry_run,
                  "status": result.status, "check_id": result.check.id if result.check is not None else None})
    return JSONResponse(body, status_code=201 if result.kind == "created" else 200)


@router.get("/problems")
def list_problems(q: Optional[str] = Query(None, max_length=64), archived: Literal["false", "true", "all"] = "false",
                  pg: Page = Depends(page), db: Session = Depends(get_db)):
    """Active problems by default; archived=true: only archived ones; archived=all: every problem."""
    which = {"false": False, "true": True, "all": None}[archived]
    return paginate(db, catalog.problems_query(q, which), Problem.id, pg,
                    lambda row: catalog.problem_summary(db, row[0]))


@router.get("/problems/{slug}")
def get_problem(slug: str, db: Session = Depends(get_db)):
    return plain(catalog.problem_detail(db, _problem(db, slug)))


@router.get("/problems/{slug}/versions/{number}")
def get_version(slug: str, number: int, db: Session = Depends(get_db)):
    problem = _problem(db, slug)
    return plain(catalog.version_detail(db, problem, _version(db, problem, number)))


@router.post("/problems/{slug}/versions/{number}/recheck")
def recheck(slug: str, number: int, request: Request, db: Session = Depends(get_db),
            actor: audit.Actor = Depends(admin_actor)):
    """Check the version again with a worker (D-054). 202: queued; 200: a check is already queued or running
    (nothing new). The status of the version changes only when the check ends."""
    problem = _problem(db, slug)
    version = _version(db, problem, number)
    try:
        out = checks.recheck(db, actor, version, now(request))
    except checks.CheckError as exc:
        db.rollback()
        raise ApiError(STATUS.get(exc.code, 409), exc.code, str(exc)) from exc
    db.commit()
    body = plain({"slug": slug, "version": number, "result": out.kind, "status": version.status,
                  "check": checks.check_row(out.check)})
    return JSONResponse(body, status_code=RECHECK_CODES[out.kind])


@router.get("/problems/{slug}/versions/{number}/files/{path:path}")
def get_file(slug: str, number: int, path: str, request: Request, db: Session = Depends(get_db)):
    """One file of the package (a test, the statement, a picture), looked up only among the version's files."""
    problem = _problem(db, slug)
    version = _version(db, problem, number)
    sha = (version.files or {}).get(path)
    if sha is None:
        raise ApiError(404, "file_not_found", f"{slug}@{number} has no file {path!r}")
    return file_response(BlobStore(request.app.state.settings.blob_dir), sha, path)


def _archive(db: Session, slug: str, archived: bool, actor: audit.Actor, request: Request) -> dict:
    problem = _problem(db, slug)
    if problem.archived != archived:
        problem.archived = archived
        audit.record(db, actor, "problem.archive" if archived else "problem.unarchive", now(request),
                     object_type="problem", object_id=problem.id, details={"slug": problem.slug})
        db.commit()
    return plain(catalog.problem_summary(db, problem))


@router.post("/problems/{slug}/archive")
def archive(slug: str, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    return _archive(db, slug, True, actor, request)


@router.post("/problems/{slug}/unarchive")
def unarchive(slug: str, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    return _archive(db, slug, False, actor, request)
