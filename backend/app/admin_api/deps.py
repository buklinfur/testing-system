"""What every /api/admin endpoint depends on: a fully logged-in active admin, the audit actor, the cursor.

Access: no live session -> 401 not_authenticated; an admin between the password and the TOTP code -> 401
mfa_required; a student -> 403 admin_required (a disabled user has no live session at all). Unsafe methods need
X-CSRF-Token as in /api/auth; the Origin of unsafe requests is checked earlier, by the public app.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api_errors import ApiError
from ..auth import audit
from ..auth_api import Current, _session, client_ip, get_db
from ..jsonable import plain
from ..models import Role, User

DEFAULT_LIMIT, MAX_LIMIT = 50, 200


def admin_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Current:
    cur = _session(request, response, db, allow_pending=False)
    if cur.user.role != Role.admin:
        raise ApiError(403, "admin_required")
    return cur


def admin_actor(request: Request, cur: Current = Depends(admin_session)) -> audit.Actor:
    """Who appears in audit_log for this request: the admin and the client address."""
    return audit.Actor(user_id=cur.user.id, ip=client_ip(request))


@dataclass(frozen=True)
class Page:
    cursor: Optional[int]  # show rows with a smaller id (newest first)
    limit: int


def page(cursor: Optional[int] = Query(None, ge=1), limit: int = Query(DEFAULT_LIMIT, ge=1)) -> Page:
    return Page(cursor, min(limit, MAX_LIMIT))


def paginate(db: Session, query, id_column, pg: Page, out: Callable) -> dict:
    """{"items": [out(row) ...], "next_cursor": id or None} for a select ordered by id, newest first."""
    if pg.cursor is not None:
        query = query.where(id_column < pg.cursor)
    rows = db.execute(query.order_by(id_column.desc()).limit(pg.limit + 1)).all()
    more = len(rows) > pg.limit
    rows = rows[:pg.limit]
    items = [out(row) for row in rows]
    return plain({"items": items, "next_cursor": _row_id(rows[-1], id_column) if more and rows else None})


def _row_id(row, id_column):
    first = row[0]
    return getattr(first, id_column.key, first)


def like(text: str) -> str:
    """An ILIKE pattern that matches `text` literally anywhere (% and _ escaped)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def users_by_id(db: Session, ids: list[int]) -> list[User]:
    """The users of these ids in this order (repeats dropped); any missing -> 404 user_not_found naming every one,
    before anything changes."""
    ids = list(dict.fromkeys(ids))
    found = {u.id: u for u in db.execute(select(User).where(User.id.in_(ids))).scalars()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ApiError(404, "user_not_found", problems=[f"no user {i}" for i in missing])
    return [found[i] for i in ids]
