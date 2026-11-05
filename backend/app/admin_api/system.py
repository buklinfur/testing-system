"""/api/admin system, audit and languages (docs/ADMIN_API.md).

GET /system is judgectl status without the blob store (that reads the disk; the console shows it). GET /audit
reads audit_log, which never holds passwords, tokens or TOTP codes (tested through this very endpoint).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import system
from ..api_errors import ApiError
from ..auth_api import get_db
from ..jsonable import plain
from ..models import AuditLog, Language
from ..timeutil import parse_local, zone
from .deps import Page, page, paginate

router = APIRouter()


@router.get("/system")
def get_system(request: Request, db: Session = Depends(get_db)):
    """Migrations, load (what students see), queue metrics, workers, configuration: one moment of the database."""
    db.commit()  # end the transaction the session check read in: the snapshot starts its own (read_one_moment)
    return plain(system.snapshot(db, request.app.state.settings))


def _moment(request: Request, value: Optional[str], name: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return parse_local(value, zone(request.app.state.settings.timezone))
    except ValueError as exc:
        raise ApiError(422, "time_invalid", f"{name}: a time like 2026-11-10T09:00:00Z") from exc


@router.get("/audit")
def get_audit(request: Request, action: Optional[str] = Query(None, max_length=64,
                                                               description="exact, or a prefix ending with '.'"),
              object_type: Optional[str] = Query(None, max_length=32), object_id: Optional[str] = Query(None, max_length=64),
              actor_user_id: Optional[int] = None, console: Optional[bool] = None,
              since: Optional[str] = Query(None, max_length=40), until: Optional[str] = Query(None, max_length=40),
              pg: Page = Depends(page), db: Session = Depends(get_db)):
    """Newest first. action=contest. is every contest.* action; console=true: only what the console did,
    console=false: only the web; since <= at < until."""
    query = select(AuditLog)
    if action:
        if action.endswith("."):
            query = query.where(AuditLog.action.startswith(action, autoescape=True))
        else:
            query = query.where(AuditLog.action == action)
    if object_type:
        query = query.where(AuditLog.object_type == object_type)
    if object_id:
        query = query.where(AuditLog.object_id == object_id)
    if actor_user_id is not None:
        query = query.where(AuditLog.actor_user_id == actor_user_id)
    if console is not None:
        query = query.where(AuditLog.actor_cli.is_not(None) if console else AuditLog.actor_cli.is_(None))
    start, end = _moment(request, since, "since"), _moment(request, until, "until")
    if start is not None:
        query = query.where(AuditLog.at >= start)
    if end is not None:
        query = query.where(AuditLog.at < end)

    def out(row):
        e = row[0]
        return {"id": e.id, "at": e.at, "action": e.action, "actor_user_id": e.actor_user_id, "actor_cli": e.actor_cli,
                "ip": e.ip, "object_type": e.object_type, "object_id": e.object_id, "details": e.details}
    return paginate(db, query, AuditLog.id, pg, out)


@router.get("/languages")
def get_languages(db: Session = Depends(get_db)):
    rows = db.execute(select(Language).order_by(Language.key)).scalars()
    return {"items": [{"key": lang.key, "name": lang.name, "enabled": lang.enabled} for lang in rows]}
