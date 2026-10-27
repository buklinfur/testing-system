"""Login steps used by the public API: password, TOTP code (admins), QR token (students), password change.

Results never tell "no such login" from "wrong password": both are `invalid`, take the time of one argon2
verification and count as a failure of that login. A locked login or IP is refused before any check.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuthFailureKind, Role, SessionState, User, UserSession
from ..settings import AuthSettings
from . import audit, login_tokens, ratelimit, sessions
from .logins import is_valid_login, normalize_login
from .passwords import burn_verify_time, check_policy, hash_password, verify_password
from .totp import SecretBox, match_code


class Outcome(str, enum.Enum):
    ok = "ok"
    invalid = "invalid"  # wrong login, password, code or token: one answer for all
    locked = "locked"  # too many failures of this login or IP


@dataclass
class LoginResult:
    outcome: Outcome
    token: Optional[str] = None  # the new cookie value
    session: Optional[UserSession] = None
    user: Optional[User] = None
    locked_until: Optional[datetime] = None


def _lock(db: Session, now: datetime, login: Optional[str], ip: str) -> Optional[LoginResult]:
    until = ratelimit.locked_until(db, now, ip=ip)
    if login is not None:
        by_login = ratelimit.locked_until(db, now, login=login)
        until = max(filter(None, (until, by_login)), default=None)
    return LoginResult(Outcome.locked, locked_until=until) if until else None


def _logged_in(user: User, ip: str, now: datetime) -> None:
    user.last_login_at = now
    user.last_login_ip = ip


def login_password(db: Session, settings: AuthSettings, now: datetime, *, login: str, password: str, ip: str,
                   user_agent: Optional[str] = None) -> LoginResult:
    """Students get an active session; admins an mfa_pending one that only allows the TOTP step."""
    login = normalize_login(login)
    locked = _lock(db, now, login, ip)
    if locked:
        return locked
    user = db.execute(select(User).where(User.login == login)).scalar_one_or_none() if is_valid_login(login) else None
    if user is None:
        burn_verify_time(password)
        ok, new_hash = False, None
    else:
        ok, new_hash = verify_password(user.password_hash, password)
        # a disabled user, or an admin whose 2FA is not set up, cannot log in; the answer stays the same
        ok = ok and user.is_active and (user.role != Role.admin or user.totp_enabled_at is not None)
    if not ok:
        ratelimit.record_failure(db, settings, now, login=login, ip=ip, kind=AuthFailureKind.password,
                                 admin=user is not None and user.role == Role.admin)
        return LoginResult(Outcome.invalid)
    if new_hash:
        user.password_hash = new_hash  # argon2 parameters changed since this hash was made
    if user.role == Role.admin:
        token, row = sessions.create(db, user, SessionState.mfa_pending, settings, now, ip, user_agent)
    else:
        token, row = sessions.create(db, user, SessionState.active, settings, now, ip, user_agent)
        _logged_in(user, ip, now)
    return LoginResult(Outcome.ok, token=token, session=row, user=user)


def login_totp(db: Session, settings: AuthSettings, box: SecretBox, now: datetime, *, row: UserSession, code: str,
               ip: str) -> LoginResult:
    """Second step of an admin login: the mfa_pending session becomes active with a new token."""
    user = db.execute(select(User).where(User.id == row.user_id).with_for_update()).scalar_one()  # one code, one use
    if row.state != SessionState.mfa_pending or not sessions.is_live(row, user, now):
        return LoginResult(Outcome.invalid)
    locked = _lock(db, now, user.login, ip)
    if locked:
        return locked
    step = match_code(box.decrypt(user.totp_secret_enc), code, now, user.totp_last_step) if user.totp_secret_enc else None
    if step is None:
        ratelimit.record_failure(db, settings, now, login=user.login, ip=ip, kind=AuthFailureKind.totp, admin=True)
        return LoginResult(Outcome.invalid)
    user.totp_last_step = step
    token = sessions.promote(row, user, settings, now)
    _logged_in(user, ip, now)
    audit.record(db, audit.Actor(user_id=user.id, ip=ip), "auth.login", now, object_type="user", object_id=user.id)
    return LoginResult(Outcome.ok, token=token, session=row, user=user)


def login_qr(db: Session, settings: AuthSettings, now: datetime, *, token: str, ip: str,
             user_agent: Optional[str] = None) -> LoginResult:
    locked = _lock(db, now, None, ip)
    if locked:
        return locked
    user = login_tokens.consume(db, token, now)
    if user is None:
        ratelimit.record_failure(db, settings, now, login=None, ip=ip, kind=AuthFailureKind.qr)
        return LoginResult(Outcome.invalid)
    cookie, row = sessions.create(db, user, SessionState.active, settings, now, ip, user_agent)
    _logged_in(user, ip, now)
    return LoginResult(Outcome.ok, token=cookie, session=row, user=user)


class PasswordChangeError(ValueError):
    pass


def change_password(db: Session, settings: AuthSettings, now: datetime, *, row: UserSession, user: User,
                    current: str, new: str, ip: str, box: Optional[SecretBox] = None,
                    totp_code: Optional[str] = None) -> LoginResult:
    """The user changes their own password: the current password (and for admins a TOTP code) is required.

    Other sessions are revoked; this one stays with a new token. Wrong secrets count as login failures.
    Raises PasswordChangeError (with a message for the user) if the new password breaks the policy.
    """
    locked = _lock(db, now, user.login, ip)
    if locked:
        return locked
    ok, _ = verify_password(user.password_hash, current)
    if not ok:
        ratelimit.record_failure(db, settings, now, login=user.login, ip=ip, kind=AuthFailureKind.password,
                                 admin=user.role == Role.admin)
        return LoginResult(Outcome.invalid)
    if user.role == Role.admin:
        user = db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
        step = match_code(box.decrypt(user.totp_secret_enc), totp_code or "", now, user.totp_last_step) \
            if box is not None and user.totp_secret_enc else None
        if step is None:
            ratelimit.record_failure(db, settings, now, login=user.login, ip=ip, kind=AuthFailureKind.totp, admin=True)
            return LoginResult(Outcome.invalid)
        user.totp_last_step = step
    try:
        check_policy(new, user.login, user.role)
    except ValueError as exc:
        raise PasswordChangeError(str(exc)) from exc
    user.password_hash = hash_password(new)
    user.password_changed_at = now
    revoked = sessions.revoke_all(db, user.id, now, keep_id=row.id)
    token = sessions.reissue(row)
    if user.role == Role.admin:
        audit.record(db, audit.Actor(user_id=user.id, ip=ip), "auth.change_password", now, object_type="user",
                     object_id=user.id, details={"sessions_revoked": revoked})
    return LoginResult(Outcome.ok, token=token, session=row, user=user)


def logout(row: UserSession, now: datetime) -> None:
    sessions.revoke(row, now)
