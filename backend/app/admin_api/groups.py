"""/api/admin groups (classes, D-020): the same service as `judgectl group` (app/groups.py; docs/ADMIN_API.md).

Groups are addressed by id here. Students come by user id; an admin in the list is refused (not_a_student), an
unknown id is 404 with every missing one. Archiving answers the contests the group is attached to and the feed
items addressed to it (K6, `feed_items` since 4v.1b).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import groups as g
from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import get_db, now
from ..groups import GroupError
from ..ids import Id
from ..jsonable import plain
from ..models import Group, User
from .deps import Page, admin_actor, like, page, paginate, users_by_id

router = APIRouter()
STATUS = {"group_not_found": 404, "group_name_taken": 409, "group_archived": 409, "group_name_invalid": 422,
          "not_a_student": 422}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameBody(Body):
    name: str = Field(max_length=200)


class MembersBody(Body):
    user_ids: list[Id] = Field(min_length=1, max_length=200)


def refused(exc: GroupError) -> ApiError:
    return ApiError(STATUS.get(exc.code, 422), exc.code, str(exc), **exc.data)


def _group(db: Session, group_id: int) -> Group:
    group = db.get(Group, group_id)
    if group is None:
        raise ApiError(404, "group_not_found")
    return group


_users = users_by_id


def _detail(db: Session, group: Group, history: bool = False) -> dict:
    out = g.group_summary(db, group)
    out["contests"] = [{"slug": c.slug, "title": c.title, "state": c.state} for c in g.contests_of(db, group)]
    out["students"] = [{"user_id": u.id, "login": u.login, "display_name": u.display_name, "added_at": m.added_at,
                        "removed_at": m.removed_at} for m, u in g.members(db, group, history)]
    return plain(out)


@router.get("/groups")
def list_groups(q: Optional[str] = Query(None, max_length=64), archived: Literal["false", "true", "all"] = "false",
                pg: Page = Depends(page), db: Session = Depends(get_db)):
    """Groups not archived by default; archived=true: only archived ones; archived=all: every group."""
    query = select(Group)
    if archived == "false":
        query = query.where(Group.archived_at.is_(None))
    elif archived == "true":
        query = query.where(Group.archived_at.is_not(None))
    if q:
        query = query.where(Group.name.ilike(like(q), escape="\\"))
    return paginate(db, query, Group.id, pg, lambda row: g.group_summary(db, row[0]))


@router.post("/groups", status_code=201)
def create_group(body: NameBody, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    try:
        group = g.create(db, actor, body.name, now(request))
    except GroupError as exc:
        raise refused(exc) from exc
    db.commit()
    return _detail(db, group)


@router.get("/groups/{group_id}")
def get_group(group_id: Id, history: bool = False, db: Session = Depends(get_db)):
    """Students now (history=true: also those removed, with when), and the contests it is attached to."""
    return _detail(db, _group(db, group_id), history)


@router.put("/groups/{group_id}")
def rename_group(group_id: Id, body: NameBody, request: Request, db: Session = Depends(get_db),
                 actor: audit.Actor = Depends(admin_actor)):
    group = _group(db, group_id)
    try:
        g.rename(db, actor, group, body.name, now(request))
    except GroupError as exc:
        raise refused(exc) from exc
    db.commit()
    return _detail(db, group)


@router.post("/groups/{group_id}/archive")
def archive_group(group_id: Id, request: Request, db: Session = Depends(get_db),
                  actor: audit.Actor = Depends(admin_actor)):
    group = _group(db, group_id)
    attached = g.archive(db, actor, group, now(request))
    db.commit()
    feed_items = [{"id": i.id, "kind": i.kind, "state": i.state, "title": i.title} for i in g.feed_items_of(db, group)]
    return plain({"group": _detail(db, group), "contests": attached, "feed_items": feed_items})


@router.post("/groups/{group_id}/unarchive")
def unarchive_group(group_id: Id, request: Request, db: Session = Depends(get_db),
                    actor: audit.Actor = Depends(admin_actor)):
    group = _group(db, group_id)
    try:
        g.unarchive(db, actor, group, now(request))
    except GroupError as exc:
        raise refused(exc) from exc
    db.commit()
    return _detail(db, group)


@router.post("/groups/{group_id}/members")
def add_members(group_id: Id, body: MembersBody, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    group = _group(db, group_id)
    try:
        result = g.add_members(db, actor, group, _users(db, body.user_ids), now(request))
    except GroupError as exc:
        raise refused(exc) from exc
    db.commit()
    return result


@router.post("/groups/{group_id}/members/remove")
def remove_members(group_id: Id, body: MembersBody, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    group = _group(db, group_id)
    result = g.remove_members(db, actor, group, _users(db, body.user_ids), now(request))
    db.commit()
    return result
