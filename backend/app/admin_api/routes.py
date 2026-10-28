"""The router of /api/admin. Every endpoint depends on admin_session (deps.py); sections come in their own modules."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth_api import Current, session_out
from .deps import admin_session

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_session)])

# Uploads read their raw body themselves (http_body.save_body, own limits) and are exempt from the 1 MB JSON limit.
# Exact (method, path) pairs only, compared with the same scope["path"] the router uses: a trailing slash, a double
# slash, another case or another method is not exempt (the body limit applies, then the router answers).
UPLOADS: frozenset = frozenset({("POST", "/api/admin/problems"), ("POST", "/api/admin/users/import")})


@router.get("/me")
def me(cur: Current = Depends(admin_session)):
    """The admin behind this session (and its CSRF token): the first call of the admin web UI."""
    return session_out(cur.row, cur.user)
