"""/api/admin contests: the same contest.yaml as JSON, through the same spec and apply (docs/ADMIN_API.md, CONTESTS.md).

GET /contests/<slug> returns `spec` in the shape of contest.yaml: sending it back with PUT changes nothing.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from .. import groups as g
from ..contests import deadlines, service
from ..contests.service import ContestError
from ..contests.spec import ContestSpecError, from_data
from ..jsonable import plain
from ..models import Contest, ContestState, DeadlineExtension, Group, Participation, User
from ..timeutil import TimezoneError, parse_local, zone
from .deps import Page, admin_actor, page, paginate

router = APIRouter()
STATUS = {"contest_not_found": 404, "contest_problem_not_found": 404, "version_not_found": 404,
          "extension_not_found": 404, "extension_not_later": 422, "extension_subject": 422, "not_a_student": 422}


class PinBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=8)
    version: int = Field(ge=1)
    rejudge: bool  # required: rejudging every submission of the problem is a decision, not a default


class ExtensionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: Optional[int] = None  # exactly one of user_id / group_id
    group_id: Optional[int] = None
    due_at: str = Field(max_length=40, description="like contest.yaml: without an offset in JUDGE_TIMEZONE")
    reason: Optional[str] = Field(None, max_length=200)


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
    members = g.contest_member_ids(db, contest)
    return paginate(db, query, Participation.id, pg,
                    lambda row: service.participant_out(contest, row[0], row[1], members))


# ----------------------------------------------------------------------------- deadline extensions (D-023, D-038)
def _row(db: Session, contest: Contest, label: str):
    try:
        return service.visible_row(db, contest, label)
    except ContestError as exc:
        raise refused(exc) from exc


@router.get("/contests/{slug}/extensions")
def contest_extensions(slug: str, label: Optional[str] = None, db: Session = Depends(get_db)):
    contest = _contest(db, slug)
    try:
        return plain({"items": deadlines.list_extensions(db, contest, label)})
    except ContestError as exc:
        raise refused(exc) from exc


@router.get("/contests/{slug}/problems/{label}/extensions")
def problem_extensions(slug: str, label: str, db: Session = Depends(get_db)):
    return contest_extensions(slug, label, db)


@router.post("/contests/{slug}/problems/{label}/extensions")
def add_extension(slug: str, label: str, body: ExtensionBody, request: Request, db: Session = Depends(get_db),
                  actor: audit.Actor = Depends(admin_actor)):
    """201 for a new extension; 200 when it replaced one of the same student or group (`replaced`: the old due)."""
    contest = _contest(db, slug)
    try:
        due = parse_local(body.due_at, zone(request.app.state.settings.timezone))
    except ValueError as exc:
        raise ApiError(422, "due_at_invalid", "due_at: a time like 2026-11-17 23:59:59 or with Z / +07:00") from exc
    user = group = None
    if body.user_id is not None:
        user = db.get(User, body.user_id)
        if user is None:
            raise ApiError(404, "user_not_found")
    if body.group_id is not None:
        group = db.get(Group, body.group_id)
        if group is None:
            raise ApiError(404, "group_not_found")
    try:
        row, replaced = deadlines.add_extension(db, actor, contest, label, due, now(request), user=user, group=group,
                                                reason=body.reason)
    except ContestError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    item = next(i for i in deadlines.list_extensions(db, contest, label) if i["id"] == row.id)
    return JSONResponse(plain({"extension": item, "replaced": replaced}), status_code=200 if replaced else 201)


@router.post("/contests/{slug}/problems/{label}/extensions/{extension_id}/delete", status_code=204)
def delete_extension(slug: str, label: str, extension_id: int, request: Request, db: Session = Depends(get_db),
                     actor: audit.Actor = Depends(admin_actor)):
    """Only an extension of this very problem of this contest: an id of another one is 404, like a missing one."""
    row = _row(db, _contest(db, slug), label)
    found = db.get(DeadlineExtension, extension_id)
    if found is None or found.contest_problem_id != row.id:
        raise ApiError(404, "extension_not_found")
    deadlines.remove_extension(db, actor, extension_id, now(request))
    db.commit()
    return Response(status_code=204)
