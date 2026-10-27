"""Public authentication API (/api/auth): password and TOTP login, QR login, logout, me, password change.

Every endpoint of the public app that needs a user depends on `current_session` (or `any_session` where an
admin between the password and the TOTP code is allowed: /totp and /logout only):
- the cookie `__Host-session` (HttpOnly, Secure, SameSite=Lax, Path=/) carries the session token;
- unsafe methods additionally need `X-CSRF-Token` equal to the CSRF token of that session (the Origin of
  every unsafe request is checked earlier, by the middleware of app.public_main).

Wrong login, password, code or token all answer 401 `invalid_credentials`; a locked login or IP answers
429 `locked` with Retry-After, the same for existing and unknown logins. Failures are committed before the
error is raised, otherwise the rollback of the request would forget them.
"""
from __future__ import annotations

import hmac
import ipaddress
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .auth import flows, sessions
from .auth.flows import LoginResult, Outcome
from .models import SessionState, User, UserSession
from .timeutil import iso_utc

COOKIE = "__Host-session"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1"})  # Caddy runs on the same machine (R17)

router = APIRouter(prefix="/api/auth")


# ----------------------------------------------------------------------------- wiring
def get_db(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def now(request: Request) -> datetime:
    return request.app.state.clock()


def client_ip(request: Request) -> str:
    """The peer address; behind Caddy (a localhost peer) the last X-Forwarded-For entry, which Caddy appends."""
    peer = request.client.host if request.client else ""
    if peer in TRUSTED_PROXIES:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded.rsplit(",", 1)[-1].strip()))
            except ValueError:
                pass
    return peer[:45] or "unknown"


def set_session_cookie(response: Response, token: str, row: UserSession, at: datetime) -> None:
    max_age = max(1, math.ceil((row.expires_at - at).total_seconds()))
    response.set_cookie(COOKIE, token, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")


@dataclass
class Current:
    row: UserSession
    user: User


def _csrf_ok(request: Request, row: UserSession) -> bool:
    sent = request.headers.get(CSRF_HEADER) or ""
    return hmac.compare_digest(sent.encode(), row.csrf_token.encode())


def _session(request: Request, response: Response, db: Session, allow_pending: bool) -> Current:
    at = now(request)
    token = request.cookies.get(COOKIE)
    found = sessions.find(db, token, at)
    if found is None:
        raise HTTPException(401, "not_authenticated")
    row, user = found
    if row.state != SessionState.active and not allow_pending:
        raise HTTPException(401, "mfa_required")
    if request.method not in SAFE_METHODS and not _csrf_ok(request, row):
        raise HTTPException(403, "csrf_token_required")
    before = row.expires_at
    sessions.touch(row, user, request.app.state.settings.auth, at)
    if row.expires_at != before:  # slid: keep the cookie alive as long as the session
        db.commit()
        set_session_cookie(response, token, row, at)
    return Current(row, user)


def current_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Current:
    """A fully logged-in user. Every protected endpoint of the public app uses this."""
    return _session(request, response, db, allow_pending=False)


def any_session(request: Request, response: Response, db: Session = Depends(get_db)) -> Current:
    """Also an admin who has passed the password but not yet the TOTP code."""
    return _session(request, response, db, allow_pending=True)


# ----------------------------------------------------------------------------- bodies and answers
class LoginBody(BaseModel):
    login: str = Field(max_length=256)
    password: str = Field(max_length=1024)


class TotpBody(BaseModel):
    code: str = Field(max_length=32)


class QrBody(BaseModel):
    token: str = Field(max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)
    totp_code: Optional[str] = Field(None, max_length=32)  # required for admins


def user_out(user: User) -> dict:
    return {"id": user.id, "login": user.login, "display_name": user.display_name, "role": user.role.value}


def session_out(row: UserSession, user: User) -> dict:
    """What the frontend needs: the state, the CSRF token for unsafe requests and, once active, the user."""
    active = row.state == SessionState.active
    return {"state": row.state.value, "csrf_token": row.csrf_token, "expires_at": iso_utc(row.expires_at),
            "user": user_out(user) if active else None}


def _refuse(result: LoginResult, at: datetime, invalid_status: int = 401):
    if result.outcome == Outcome.locked:
        retry = max(1, math.ceil((result.locked_until - at).total_seconds()))
        raise HTTPException(429, "locked", headers={"Retry-After": str(retry)})
    raise HTTPException(invalid_status, "invalid_credentials")


def _logged_in(result: LoginResult, response: Response, at: datetime) -> dict:
    set_session_cookie(response, result.token, result.session, at)
    return session_out(result.session, result.user)


# ----------------------------------------------------------------------------- endpoints
@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)):
    """Students get an active session; admins an `mfa_pending` one that only allows /totp and /logout."""
    at = now(request)
    result = flows.login_password(db, request.app.state.settings.auth, at, login=body.login, password=body.password,
                                  ip=client_ip(request), user_agent=request.headers.get("user-agent"))
    db.commit()
    if result.outcome != Outcome.ok:
        _refuse(result, at)
    return _logged_in(result, response, at)


@router.post("/totp")
def totp(body: TotpBody, request: Request, response: Response, cur: Current = Depends(any_session),
         db: Session = Depends(get_db)):
    """Second step of an admin login: the session becomes active under a new token (and a new CSRF token)."""
    if cur.row.state != SessionState.mfa_pending:
        raise HTTPException(409, "already_authenticated")
    at = now(request)
    result = flows.login_totp(db, request.app.state.settings.auth, request.app.state.box, at, row=cur.row,
                              code=body.code, ip=client_ip(request))
    db.commit()
    if result.outcome != Outcome.ok:
        _refuse(result, at)
    return _logged_in(result, response, at)


@router.post("/qr")
def qr(body: QrBody, request: Request, response: Response, db: Session = Depends(get_db)):
    """Login by a one-time QR token (students only); the frontend posts the token from the URL fragment."""
    at = now(request)
    result = flows.login_qr(db, request.app.state.settings.auth, at, token=body.token, ip=client_ip(request),
                            user_agent=request.headers.get("user-agent"))
    db.commit()
    if result.outcome != Outcome.ok:
        _refuse(result, at)
    return _logged_in(result, response, at)


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Revokes the session (CSRF token required). Without a live session it only clears the cookie."""
    found = sessions.find(db, request.cookies.get(COOKIE), now(request))
    if found is not None:
        row, _user = found
        if not _csrf_ok(request, row):
            raise HTTPException(403, "csrf_token_required")
        flows.logout(row, now(request))
        db.commit()
    clear_session_cookie(response)
    response.status_code = 204
    return None


@router.get("/me")
def me(cur: Current = Depends(current_session)):
    return session_out(cur.row, cur.user)


@router.post("/password")
def change_password(body: PasswordBody, request: Request, response: Response, cur: Current = Depends(current_session),
                    db: Session = Depends(get_db)):
    """The current password (admins: and a TOTP code) is required; other sessions are revoked and this one
    gets a new token. A new password against the rules answers 422 with a message for the user."""
    at = now(request)
    try:
        result = flows.change_password(db, request.app.state.settings.auth, at, row=cur.row, user=cur.user,
                                       current=body.current_password, new=body.new_password, ip=client_ip(request),
                                       box=request.app.state.box, totp_code=body.totp_code)
    except flows.PasswordChangeError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    if result.outcome != Outcome.ok:
        _refuse(result, at, invalid_status=403)  # 403, not 401: the session itself is still fine
    return _logged_in(result, response, at)
