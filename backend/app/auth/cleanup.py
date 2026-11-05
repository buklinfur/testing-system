"""Pruning of old authentication rows; called by the reaper (app.maintenance) about once an hour."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from ..models import AuthFailure, AuthLockout, LoginToken, UserSession
from ..settings import AuthSettings


def prune(db: Session, settings: AuthSettings, now: datetime) -> dict:
    """Delete failures, finished lockouts, dead sessions and QR tokens older than retention_days."""
    cutoff = now - timedelta(days=settings.retention_days)
    out = {
        "failures": db.execute(delete(AuthFailure).where(AuthFailure.at < cutoff)).rowcount,
        "lockouts": db.execute(delete(AuthLockout).where(AuthLockout.until < cutoff)).rowcount,
        "sessions": db.execute(delete(UserSession).where(or_(
            UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff,
            UserSession.absolute_expires_at < cutoff))).rowcount,
        "login_tokens": db.execute(delete(LoginToken).where(or_(
            LoginToken.expires_at < cutoff, LoginToken.used_at < cutoff, LoginToken.revoked_at < cutoff))).rowcount,
    }
    return out
