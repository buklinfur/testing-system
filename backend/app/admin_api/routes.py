"""The router of /api/admin. Every endpoint depends on admin_session (deps.py); sections come in their own modules."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth_api import Current, session_out
from .deps import admin_session

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_session)])

# uploads read their raw body themselves (http_body.save_body, own limits): exempt from the 1 MB JSON limit
UPLOADS: frozenset = frozenset()


@router.get("/me")
def me(cur: Current = Depends(admin_session)):
    """The admin behind this session (and its CSRF token): the first call of the admin web UI."""
    return session_out(cur.row, cur.user)
