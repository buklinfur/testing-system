"""/api/admin contests: the same contest.yaml as JSON, through the same spec and apply (docs/ADMIN_API.md, CONTESTS.md).

GET /contests/<slug> returns `spec` in the shape of contest.yaml: sending it back with PUT changes nothing.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..contests import service
from ..contests.service import ContestError
from ..contests.spec import ContestSpecError, from_data
from ..jsonable import plain
from ..models import Contest, ContestState, Participation, User
from ..timeutil import TimezoneError, zone
from .deps import Page, admin_actor, page, paginate

router = APIRouter()
STATUS = {"contest_not_found": 404, "contest_problem_not_found": 404, "version_not_found": 404}


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=8)
    version: int = Field(ge=1)
    rejudge: bool  # required: rejudging every submission of the problem is a decision, not a default


def refused(exc: ContestError) -> ApiError:
    return ApiError(STATUS.get(exc.code, 409), exc.code, problems=exc.problems)


def _contest(db: Session, slug: str) -> Contest:
    try:
        return service.get_contest(db, slug)
    except ContestError as exc:
        raise refused(exc) from exc


@router.get("/contests")
def list_contests(state: Optional[Literal["draft", "published", "archived"]] = None, pg: Page = Depends(page),
                  db: Session = Depends(get_db)):
    query = service.contests_query(ContestState(state) if state else None)
    return paginate(db, query, Contest.id, pg, lambda row: service.contest_summary(db, row[0]))


@router.get("/contests/{slug}")
def get_contest(slug: str, db: Session = Depends(get_db)):
    return plain(service.contest_detail(db, _contest(db, slug)))


@router.put("/contests/{slug}")
def put_contest(slug: str, request: Request, dry_run: bool = False, body: dict = Body(...),
                db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    """Create or change a contest from the JSON form of contest.yaml (same schema, same rules, same apply)."""
    if body.get("slug") != slug:
        raise ApiError(422, "slug_mismatch", "the slug in the body must be the slug in the path")
    try:
        spec = from_data(body, zone(request.app.state.settings.timezone))
    except ContestSpecError as exc:
        raise ApiError(422, "contest_invalid", "the contest is not valid", problems=exc.problems) from exc
    except TimezoneError as exc:
        raise ApiError(500, "server_misconfigured", str(exc)) from exc
    step = db.begin_nested()
    try:
        result = service.apply(db, actor, spec, now(request))
    except ContestError as exc:
        step.rollback()
        db.rollback()
        raise refused(exc) from exc
    detail = service.contest_detail(db, result.contest)
    if dry_run:
        step.rollback()
        db.rollback()
    else:
        step.commit()
        db.commit()
    created = result.created and not dry_run
    return JSONResponse(plain({"created": result.created, "changes": result.changes, "warnings": result.warnings,
                               "dry_run": dry_run, "contest": None if dry_run and result.created else detail}),
                        status_code=201 if created else 200)


def _state(db: Session, slug: str, state: ContestState, actor: audit.Actor, request: Request) -> dict:
    contest = _contest(db, slug)
    changed = service.set_state(db, actor, contest, state, now(request))
    db.commit()
    return plain({"slug": slug, "state": contest.state, "changed": changed})


@router.post("/contests/{slug}/publish")
def publish(slug: str, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    return _state(db, slug, ContestState.published, actor, request)


@router.post("/contests/{slug}/archive")
def archive(slug: str, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    return _state(db, slug, ContestState.archived, actor, request)


@router.post("/contests/{slug}/pin")
def pin(slug: str, body: PinBody, request: Request, db: Session = Depends(get_db),
        actor: audit.Actor = Depends(admin_actor)):
    contest = _contest(db, slug)
    try:
        result = service.pin(db, actor, contest, body.label, body.version, body.rejudge, now(request))
    except ContestError as exc:
        raise refused(exc) from exc
    db.commit()
    return plain(result)


@router.post("/contests/{slug}/code")
def regenerate_code(slug: str, request: Request, db: Session = Depends(get_db),
                    actor: audit.Actor = Depends(admin_actor)):
    contest = _contest(db, slug)
    try:
        code = service.regenerate_code(db, actor, contest, now(request))
    except ContestError as exc:
        raise refused(exc) from exc
    db.commit()
    return {"slug": slug, "invite_code": code}


@router.get("/contests/{slug}/participants")
def participants(slug: str, pg: Page = Depends(page), db: Session = Depends(get_db)):
    contest = _contest(db, slug)
    query = select(Participation, User).join(User, User.id == Participation.user_id).where(
        Participation.contest_id == contest.id)
    return paginate(db, query, Participation.id, pg, lambda row: {
        "user_id": row[1].id, "login": row[1].login, "display_name": row[1].display_name,
        "joined_at": row[0].joined_at, "started_at": row[0].started_at})
