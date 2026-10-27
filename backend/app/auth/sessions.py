"""Server-side sessions (R11). The cookie carries a random token; the database keeps only sha256(token).

Lifetimes: student 30 days sliding; admin 12 h sliding and at most 7 days; `mfa_pending` (admin passed the
password, not yet the TOTP code) 5 minutes and allows nothing else. Moving to `active` issues a new token,
so a token seen before the second factor is useless afterwards.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import Role, SessionState, User, UserSession
from ..settings import AuthSettings

TOUCH_EVERY = timedelta(seconds=60)  # do not write last_seen_at on every request
USER_AGENT_MAX = 256


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _lifetime(role: Role, state: SessionState, settings: AuthSettings, now: datetime):
    """(expires_at, absolute_expires_at) of a fresh session."""
    if state == SessionState.mfa_pending:
        end = now + timedelta(seconds=settings.mfa_pending_s)
        return end, end
    if role == Role.admin:
        return now + timedelta(seconds=settings.admin_idle_s), now + timedelta(seconds=settings.admin_absolute_s)
    return now + timedelta(seconds=settings.student_idle_s), None


def create(db: Session, user: User, state: SessionState, settings: AuthSettings, now: datetime,
           ip: Optional[str] = None, user_agent: Optional[str] = None) -> tuple[str, UserSession]:
    token = new_token()
    expires, absolute = _lifetime(user.role, state, settings, now)
    row = UserSession(token_hash=hash_token(token), user_id=user.id, state=state, csrf_token=new_token(),
                      created_at=now, last_seen_at=now, expires_at=expires, absolute_expires_at=absolute,
                      ip=ip, user_agent=(user_agent or None) and user_agent[:USER_AGENT_MAX])
    db.add(row)
    db.flush()
    return token, row


def is_live(row: UserSession, user: User, now: datetime) -> bool:
    return (row.revoked_at is None and now < row.expires_at
            and (row.absolute_expires_at is None or now < row.absolute_expires_at) and user.is_active)


def find(db: Session, token: Optional[str], now: datetime) -> Optional[tuple[UserSession, User]]:
    """The live session of this cookie token and its user, or None. Does not extend the session."""
    if not token:
        return None
    found = db.execute(select(UserSession, User).join(User, User.id == UserSession.user_id)
                       .where(UserSession.token_hash == hash_token(token))).first()
    if found is None or not is_live(found[0], found[1], now):
        return None
    return found[0], found[1]


def touch(row: UserSession, user: User, settings: AuthSettings, now: datetime) -> None:
    """Slide the idle timeout of an active session (never beyond its absolute limit)."""
    if row.state != SessionState.active or now - row.last_seen_at < TOUCH_EVERY:
        return
    idle = settings.admin_idle_s if user.role == Role.admin else settings.student_idle_s
    expires = now + timedelta(seconds=idle)
    if row.absolute_expires_at is not None:
        expires = min(expires, row.absolute_expires_at)
    row.last_seen_at = now
    row.expires_at = expires


def reissue(row: UserSession) -> str:
    """New token and CSRF token for the same session (after the second factor or a password change)."""
    token = new_token()
    row.token_hash = hash_token(token)
    row.csrf_token = new_token()
    return token


def promote(row: UserSession, user: User, settings: AuthSettings, now: datetime) -> str:
    """mfa_pending -> active with a new token; the admin lifetime starts now."""
    row.state = SessionState.active
    row.expires_at, row.absolute_expires_at = _lifetime(user.role, SessionState.active, settings, now)
    row.last_seen_at = now
    return reissue(row)


def revoke(row: UserSession, now: datetime) -> None:
    if row.revoked_at is None:
        row.revoked_at = now


def revoke_all(db: Session, user_id: int, now: datetime, keep_id: Optional[int] = None) -> int:
    """Revoke every live session of a user (except `keep_id`). Returns how many were revoked."""
    stmt = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if keep_id is not None:
        stmt = stmt.where(UserSession.id != keep_id)
    return db.execute(stmt.values(revoked_at=now).execution_options(synchronize_session="fetch")).rowcount
