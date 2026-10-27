"""One-time QR login tokens for students (R10). Only sha256(token) is stored.

The link is https://<domain>/login/qr#<token>: the token sits in the fragment, so it never reaches the
Caddy log or a Referer header; the frontend POSTs it. A new token revokes the previous ones.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..models import LoginToken, Role, User
from ..settings import AuthSettings
from .sessions import hash_token, new_token


class LoginTokenError(ValueError):
    pass


def revoke_all(db: Session, user_id: int, now: datetime) -> int:
    return db.execute(update(LoginToken).where(LoginToken.user_id == user_id, LoginToken.used_at.is_(None),
                                               LoginToken.revoked_at.is_(None)).values(revoked_at=now)).rowcount


def issue(db: Session, user: User, settings: AuthSettings, now: datetime) -> tuple[str, datetime]:
    """(token, expires_at). Admins never get one."""
    if user.role != Role.student:
        raise LoginTokenError("QR login is for students only")
    if not user.is_active:
        raise LoginTokenError(f"user {user.login} is disabled")
    revoke_all(db, user.id, now)
    token = new_token()
    expires = now + timedelta(seconds=settings.qr_token_ttl_s)
    db.add(LoginToken(token_hash=hash_token(token), user_id=user.id, created_at=now, expires_at=expires))
    db.flush()
    return token, expires


def consume(db: Session, token: str, now: datetime) -> Optional[User]:
    """The student this token logs in, marking it used; None if it is unknown, used, revoked or expired."""
    if not token:
        return None
    found = db.execute(select(LoginToken, User).join(User, User.id == LoginToken.user_id)
                       .where(LoginToken.token_hash == hash_token(token))
                       .with_for_update(of=LoginToken)).first()  # two requests with one token: only one wins
    if found is None:
        return None
    row, user = found
    if row.used_at is not None or row.revoked_at is not None or now >= row.expires_at:
        return None
    if user.role != Role.student or not user.is_active:
        return None
    row.used_at = now
    return user
