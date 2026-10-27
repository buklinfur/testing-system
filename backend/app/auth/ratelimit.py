"""Brute-force limits (R12): per login first, per IP as a generous fallback.

Every failed step (password, TOTP code, QR token) is a row in auth_failures. When the failures of a login
(or an IP) within the window reach the limit, an auth_lockouts row blocks it until `until`. Failures are
counted only after the newest lockout of the same subject, so the failures that caused a lockout do not
lock again right after it, and unlocking (a lockout row that is already cleared) resets the counter
without deleting any history.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import AuthFailure, AuthFailureKind, AuthLockout
from ..settings import AuthSettings

LOGIN_MAX = 64  # whatever was typed is stored truncated


def _subject(login: Optional[str], ip: Optional[str]):
    return (AuthLockout.login == login) if login is not None else (AuthLockout.ip == ip)


def locked_until(db: Session, now: datetime, *, login: Optional[str] = None, ip: Optional[str] = None) -> Optional[datetime]:
    """End of the active lockout of this login (or IP), or None."""
    return db.execute(select(func.max(AuthLockout.until)).where(
        _subject(login, ip), AuthLockout.cleared_at.is_(None), AuthLockout.until > now)).scalar()


def _failures_since(db: Session, now: datetime, window_s: int, *, login=None, ip=None) -> int:
    start = now - timedelta(seconds=window_s)
    last_lockout = db.execute(select(func.max(AuthLockout.created_at)).where(_subject(login, ip))).scalar()
    if last_lockout is not None and last_lockout > start:
        start = last_lockout
    column, value = (AuthFailure.login, login) if login is not None else (AuthFailure.ip, ip)
    return db.execute(select(func.count()).select_from(AuthFailure)
                      .where(column == value, AuthFailure.at > start, AuthFailure.at <= now)).scalar_one()


def record_failure(db: Session, settings: AuthSettings, now: datetime, *, login: Optional[str], ip: str,
                   kind: AuthFailureKind, admin: bool = False) -> None:
    """Store a failure and lock the login / IP if it reached its limit (admins have the stricter one)."""
    login = login[:LOGIN_MAX] if login else None
    db.add(AuthFailure(login=login, ip=ip, kind=kind, at=now))
    db.flush()
    if login is not None:
        limit = settings.admin_login_fail_limit if admin else settings.login_fail_limit
        if _failures_since(db, now, settings.login_window_s, login=login) >= limit:
            db.add(AuthLockout(login=login, until=now + timedelta(seconds=settings.login_lockout_s), created_at=now))
    if _failures_since(db, now, settings.ip_window_s, ip=ip) >= settings.ip_fail_limit:
        db.add(AuthLockout(ip=ip, until=now + timedelta(seconds=settings.ip_lockout_s), created_at=now))
    db.flush()


def unlock(db: Session, now: datetime, by: str, *, login: Optional[str] = None, ip: Optional[str] = None) -> bool:
    """Lift the lockout of a login (or IP) and reset its failure counter. True if it was locked."""
    was_locked = db.execute(update(AuthLockout).where(
        _subject(login, ip), AuthLockout.cleared_at.is_(None), AuthLockout.until > now)
        .values(cleared_at=now, cleared_by=by)).rowcount > 0
    db.add(AuthLockout(login=login, ip=None if login is not None else ip, until=now, created_at=now,
                       cleared_at=now, cleared_by=by))  # counter reset marker
    db.flush()
    return was_locked
