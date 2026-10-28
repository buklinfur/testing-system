"""/api/admin users: students to manage, admins to look at (docs/ADMIN_API.md, "Пользователи").

Everything goes through app.auth.users, the same functions judgectl uses. Admins are read-only here: any action on
one is 403 admin_in_console (R2). Passwords and QR tokens appear only in the answer that creates them (no-store,
like every answer of the public app), never in audit_log or the logs.
"""
from __future__ import annotations

import io
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..api_errors import ApiError
from ..auth import audit, csvimport, ratelimit, users
from ..auth.users import UserError
from ..auth_api import get_db, now
from ..http_body import require_type, save_body
from ..models import AuditLog, Role, User
from ..jsonable import plain
from .deps import Page, admin_actor, page, paginate

CSV_LIMIT = 1024 * 1024
STATUS = {"user_not_found": 404, "import_not_found": 404, "admin_in_console": 403, "login_taken": 409,
          "has_traces": 409, "already_rolled_back": 409, "users_logged_in": 409, "import_empty": 409}

router = APIRouter()


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewStudent(Body):
    surname: str = Field("", max_length=100)
    first_name: str = Field("", max_length=100)
    login: Optional[str] = Field(None, max_length=64)
    display_name: Optional[str] = Field(None, max_length=128)


class NewLogin(Body):
    login: str = Field(max_length=64)


class ConfirmLogin(Body):
    confirm_login: str = Field(max_length=64)


class ConfirmImport(Body):
    confirm_import_id: int


class IpBody(Body):
    ip: str = Field(max_length=64)


class QrBody(Body):
    user_ids: list[int] = Field(min_length=1, max_length=200)


def refused(exc: UserError) -> ApiError:
    return ApiError(STATUS.get(exc.code, 422), exc.code, str(exc), **exc.data)


def _user(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise ApiError(404, "user_not_found")
    return user


def _student(db: Session, user_id: int) -> User:
    user = _user(db, user_id)
    if user.role != Role.student:
        raise ApiError(403, "admin_in_console", "admins are managed only from the console (judgectl admin ...)")
    return user


def _out(db: Session, request: Request, user: User) -> dict:
    at = now(request)
    return plain(users.describe(db, user, request.app.state.settings.auth, at, users.active_locks(db, at)))


# ----------------------------------------------------------------------------- lists and one user
@router.get("/users")
def list_users(request: Request, q: Optional[str] = Query(None, max_length=100),
               role: Optional[Literal["student", "admin"]] = None, active: Optional[bool] = None,
               locked: Optional[bool] = None, pg: Page = Depends(page), db: Session = Depends(get_db)):
    at = now(request)
    locks = users.active_locks(db, at)
    query = users.users_query(q=q, role=Role(role) if role else None, active=active)
    if locked is True:
        query = query.where(User.login.in_(list(locks)))
    elif locked is False and locks:
        query = query.where(User.login.not_in(list(locks)))
    settings = request.app.state.settings.auth
    return paginate(db, query, User.id, pg, lambda row: users.describe(db, row[0], settings, at, locks))


@router.get("/users/imports")
def list_imports(pg: Page = Depends(page), db: Session = Depends(get_db)):
    undone = users.rolled_back_imports(db)
    query = select(AuditLog).where(AuditLog.action == "user.import")

    def out(row):
        entry = row[0]
        details = entry.details or {}
        return {"id": entry.id, "at": entry.at, "file": entry.object_id, "count": details.get("count"),
                "by_user_id": entry.actor_user_id, "by_console": entry.actor_cli, "rolled_back": entry.id in undone}
    return paginate(db, query, AuditLog.id, pg, out)


@router.get("/users/{user_id}")
def get_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    return _out(db, request, _user(db, user_id))


@router.get("/users/{user_id}/failures")
def user_failures(user_id: int, request: Request, limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    user = _user(db, user_id)
    at, settings = now(request), request.app.state.settings.auth
    return plain({"login": user.login, "counting": ratelimit.current_failures(db, settings, at, user.login),
            "limit": settings.admin_login_fail_limit if user.role == Role.admin else settings.login_fail_limit,
            "locked_until": users.active_locks(db, at).get(user.login),
            "items": [{"at": f.at, "kind": f.kind.value, "ip": f.ip} for f in ratelimit.recent_failures(db, user.login, limit)]})


# ----------------------------------------------------------------------------- create and import
@router.post("/users", status_code=201)
def create_student(body: NewStudent, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    try:
        user, password = users.create_student(db, actor, now(request), surname=body.surname,
                                              first_name=body.first_name, login=body.login,
                                              display_name=body.display_name)
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    return {"user": _out(db, request, user), "password": password}


@router.post("/users/import", status_code=201)
async def import_class(request: Request, dry_run: bool = False, db: Session = Depends(get_db),
                       actor: audit.Actor = Depends(admin_actor)):
    """The body is the class list itself (text/csv, at most 1 MB): all or nothing, every bad line reported."""
    require_type(request, "text/csv")
    buffer = io.BytesIO()
    await save_body(request, buffer, CSV_LIMIT)
    at = now(request)

    def work():
        try:
            parsed = csvimport.parse(buffer.getvalue())
            if not dry_run:
                result = users.import_students(db, actor, parsed, at, "upload.csv")
                db.commit()
                return result
            step = db.begin_nested()
            try:
                return None, users.import_students(db, actor, parsed, at, "upload.csv")[1]
            finally:
                step.rollback()
        except csvimport.CsvImportError as exc:
            db.rollback()
            raise ApiError(422, "csv_invalid", "nothing was imported", problems=exc.problems) from exc

    import_id, results = await run_in_threadpool(work)
    if dry_run:
        return JSONResponse({"dry_run": True, "users": [{k: r[k] for k in ("surname", "first_name", "login", "display_name")}
                                                        for r in results]}, status_code=200)
    if "text/csv" in request.headers.get("accept", ""):
        return Response(("﻿" + csvimport.result_csv(results)).encode("utf-8"), status_code=201,
                        media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="students-import-{import_id}.csv"'})
    return {"import_id": import_id, "users": results}


@router.post("/users/imports/{import_id}/rollback")
def rollback_import(import_id: int, body: ConfirmImport, request: Request, db: Session = Depends(get_db),
                    actor: audit.Actor = Depends(admin_actor)):
    if body.confirm_import_id != import_id:
        raise ApiError(422, "confirmation_mismatch", "confirm_import_id must repeat the id of the import")
    try:
        logins = users.import_rollback(db, actor, import_id, now(request))
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    return {"import_id": import_id, "deleted": logins}


# ----------------------------------------------------------------------------- one student
@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, request: Request, db: Session = Depends(get_db),
                   actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    password = users.reset_password(db, actor, user, now(request))
    db.commit()
    return {"user": _out(db, request, user), "password": password}


@router.post("/users/{user_id}/disable")
def disable(user_id: int, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    users.set_active(db, actor, user, False, now(request))
    db.commit()
    return _out(db, request, user)


@router.post("/users/{user_id}/enable")
def enable(user_id: int, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    users.set_active(db, actor, user, True, now(request))
    db.commit()
    return _out(db, request, user)


@router.put("/users/{user_id}/login")
def set_login(user_id: int, body: NewLogin, request: Request, db: Session = Depends(get_db),
              actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    try:
        users.set_login(db, actor, user, body.login, now(request))
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    return _out(db, request, user)


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    was = users.unlock(db, actor, user.login, now(request))
    db.commit()
    return {"login": user.login, "was_locked": was}


@router.post("/users/{user_id}/delete", status_code=204)
def delete_user(user_id: int, body: ConfirmLogin, request: Request, db: Session = Depends(get_db),
                actor: audit.Actor = Depends(admin_actor)):
    user = _student(db, user_id)
    if body.confirm_login.strip().lower() != user.login:
        raise ApiError(422, "confirmation_mismatch", "confirm_login must repeat the login of the user")
    try:
        users.delete_user(db, actor, user, now(request))
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    return Response(status_code=204)


# ----------------------------------------------------------------------------- logins, IPs, QR tokens
@router.post("/logins/{login}/unlock")
def unlock_login(login: str, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    """Any login, also one that does not exist (failures are counted for those too); not an admin's."""
    user = users.find_by_login(db, login)
    if user is not None and user.role == Role.admin:
        raise ApiError(403, "admin_in_console", "admins are managed only from the console (judgectl admin unlock)")
    was = users.unlock(db, actor, login, now(request))
    db.commit()
    return {"login": login.strip().lower(), "was_locked": was}


@router.post("/ips/unlock")
def unlock_ip(body: IpBody, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    try:
        was = users.unlock_ip(db, actor, body.ip, now(request))
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    return {"ip": body.ip.strip(), "was_locked": was}


@router.post("/users/qr-tokens")
def qr_tokens(body: QrBody, request: Request, db: Session = Depends(get_db), actor: audit.Actor = Depends(admin_actor)):
    """One-time login links for a list of students (cards with QR codes are printed by the web UI, module 5)."""
    ids = list(dict.fromkeys(body.user_ids))
    found = {u.id: u for u in db.execute(select(User).where(User.id.in_(ids))).scalars()}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ApiError(404, "user_not_found", problems=[f"no user {i}" for i in missing])
    try:
        issued = users.issue_login_tokens(db, actor, [found[i] for i in ids], request.app.state.settings.auth,
                                          now(request))
    except UserError as exc:
        raise refused(exc) from exc
    db.commit()
    origin = request.app.state.origin
    return plain({"tokens": [{"user_id": u.id, "login": u.login, "display_name": u.display_name,
                              "url": f"{origin}/login/qr#{token}", "expires_at": expires} for u, token, expires in issued]})
