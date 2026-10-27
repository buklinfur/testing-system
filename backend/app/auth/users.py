"""Account management shared by judgectl and the admin API: create, reset, enable/disable, 2FA enrolment.

Every change is written to audit_log with its actor. Functions flush but never commit: the caller owns
the transaction (session_scope in judgectl, the request session in the API).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (AuditLog, Contest, Judgement, LoginToken, Participation, Problem, Role, Submission, User,
                      UserSession)
from ..timeutil import iso_utc
from . import audit, login_tokens, ratelimit, sessions
from .logins import LoginError, checked_login, default_display_name, default_login, unique_login
from .passwords import PasswordPolicyError, check_policy, generate_password, hash_password
from .totp import SecretBox, match_code, new_secret


class UserError(ValueError):
    pass


def find_by_login(db: Session, raw_login: str) -> Optional[User]:
    return db.execute(select(User).where(User.login == raw_login.strip().lower())).scalar_one_or_none()


def get_by_login(db: Session, raw_login: str) -> User:
    user = find_by_login(db, raw_login)
    if user is None:
        raise UserError(f"no user {raw_login.strip().lower()!r}")
    return user


def _check_policy(password: str, login: str, role: Role) -> None:
    try:
        check_policy(password, login, role)
    except PasswordPolicyError as exc:
        raise UserError(str(exc)) from exc


def _taken(db: Session):
    return lambda login: db.execute(select(User.id).where(User.login == login)).first() is not None


def _explicit_login(db: Session, raw: str) -> str:
    try:
        login = checked_login(raw)
    except LoginError as exc:
        raise UserError(str(exc)) from exc
    if _taken(db)(login):
        raise UserError(f"login {login!r} is already taken")
    return login


def create_student(db: Session, actor: audit.Actor, now: datetime, *, surname: str = "", first_name: str = "",
                   login: Optional[str] = None, display_name: Optional[str] = None) -> tuple[User, str]:
    """(user, generated password). Login: as given, or surname.initial with a number on collision."""
    if login:
        login = _explicit_login(db, login)
    else:
        try:
            login = unique_login(default_login(surname, first_name), _taken(db))
        except LoginError as exc:
            raise UserError(str(exc)) from exc
    display_name = (display_name or "").strip() or default_display_name(surname, first_name)
    if not display_name:
        raise UserError("a display name is required")
    password = generate_password()
    user = User(login=login, display_name=display_name[:128], role=Role.student, is_active=True,
                password_hash=hash_password(password), password_changed_at=now)
    db.add(user)
    db.flush()
    audit.record(db, actor, "user.create", now, object_type="user", object_id=user.id,
                 details={"login": login, "role": "student"})
    return user, password


def create_admin(db: Session, actor: audit.Actor, now: datetime, *, login: str, display_name: str,
                 password: str) -> User:
    """An admin with the password they chose (>= 12). 2FA is enrolled right after (enroll_totp + confirm_totp)."""
    login = _explicit_login(db, login)
    _check_policy(password, login, Role.admin)
    if not display_name.strip():
        raise UserError("a display name is required")
    user = User(login=login, display_name=display_name.strip()[:128], role=Role.admin, is_active=True,
                password_hash=hash_password(password), password_changed_at=now)
    db.add(user)
    db.flush()
    audit.record(db, actor, "user.create", now, object_type="user", object_id=user.id,
                 details={"login": login, "role": "admin"})
    return user


def _revoke_access(db: Session, user: User, now: datetime) -> dict:
    return {"sessions_revoked": sessions.revoke_all(db, user.id, now),
            "qr_tokens_revoked": login_tokens.revoke_all(db, user.id, now)}


def reset_password(db: Session, actor: audit.Actor, user: User, now: datetime,
                   new_password: Optional[str] = None) -> str:
    """R8: a new password, all sessions and QR tokens revoked, the brute-force lockout lifted.

    Students get a generated password; admins must pass the one they chose (policy applies).
    """
    if new_password is None:
        if user.role == Role.admin:
            raise UserError("an admin password is chosen by the admin, not generated")
        new_password = generate_password()
    else:
        _check_policy(new_password, user.login, user.role)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = now
    details = _revoke_access(db, user, now)
    details["unlocked"] = ratelimit.unlock(db, now, actor.label, login=user.login)
    audit.record(db, actor, "user.reset_password", now, object_type="user", object_id=user.id, details=details)
    return new_password


def set_active(db: Session, actor: audit.Actor, user: User, active: bool, now: datetime) -> None:
    """R9: users are never deleted; disabling revokes every session and QR token."""
    user.is_active = active
    details = {} if active else _revoke_access(db, user, now)
    audit.record(db, actor, "user.enable" if active else "user.disable", now, object_type="user",
                 object_id=user.id, details=details)


def set_login(db: Session, actor: audit.Actor, user: User, raw_login: str, now: datetime) -> str:
    old = user.login
    login = _explicit_login(db, raw_login)
    user.login = login
    audit.record(db, actor, "user.set_login", now, object_type="user", object_id=user.id,
                 details={"old": old, "new": login})
    return login


def unlock(db: Session, actor: audit.Actor, raw_login: str, now: datetime) -> bool:
    login = raw_login.strip().lower()
    was_locked = ratelimit.unlock(db, now, actor.label, login=login)
    audit.record(db, actor, "user.unlock", now, object_type="login", object_id=login,
                 details={"was_locked": was_locked})
    return was_locked


def issue_login_token(db: Session, actor: audit.Actor, user: User, settings, now: datetime) -> tuple[str, datetime]:
    try:
        token, expires = login_tokens.issue(db, user, settings, now)
    except login_tokens.LoginTokenError as exc:
        raise UserError(str(exc)) from exc
    audit.record(db, actor, "user.qr_token", now, object_type="user", object_id=user.id,
                 details={"expires_at": iso_utc(expires)})
    return token, expires


def enroll_totp(db: Session, actor: audit.Actor, user: User, box: SecretBox, now: datetime) -> str:
    """A new TOTP secret (returned in clear once, for the QR). Until confirm_totp the admin cannot log in.

    Used both for a new admin and for `admin reset-2fa`; the latter also revokes all sessions.
    """
    if user.role != Role.admin:
        raise UserError("2FA is for admins")
    secret = new_secret()
    was_enabled = user.totp_enabled_at is not None
    user.totp_secret_enc = box.encrypt(secret)
    user.totp_enabled_at = None
    user.totp_last_step = None
    details = {"reset": was_enabled, "sessions_revoked": sessions.revoke_all(db, user.id, now)}
    audit.record(db, actor, "user.reset_2fa" if was_enabled else "user.enroll_2fa", now, object_type="user",
                 object_id=user.id, details=details)
    return secret


def confirm_totp(db: Session, actor: audit.Actor, user: User, box: SecretBox, code: str, now: datetime) -> bool:
    """The admin typed a code from the app: the enrolment is complete. False if the code is wrong."""
    if user.totp_secret_enc is None:
        raise UserError("no 2FA enrolment in progress")
    step = match_code(box.decrypt(user.totp_secret_enc), code, now, user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    user.totp_enabled_at = now
    audit.record(db, actor, "user.confirm_2fa", now, object_type="user", object_id=user.id)
    return True


# ----------------------------------------------------------------------------- deletion (R9 refined, MODULE_4A 5.5)
# Everything that would keep a trace of a user. A user with any of them is only disabled, never deleted.
TRACES = (
    ("problems owned", Problem.owner_id),
    ("contests created", Contest.created_by),
    ("contest participations", Participation.user_id),
    ("submissions", Submission.user_id),
    ("rejudges requested", Judgement.requested_by),
    ("audit records as actor (web actions or logins)", AuditLog.actor_user_id),
)


def traces(db: Session, user: User) -> dict:
    """{what: count} of everything that references the user (empty: the user can be deleted)."""
    found = {}
    for label, column in TRACES:
        n = db.execute(select(func.count()).select_from(column.table).where(column == user.id)).scalar_one()
        if n:
            found[label] = n
    return found


def _describe(found: dict) -> str:
    return ", ".join(f"{label}: {n}" for label, n in found.items())


def _erase(db: Session, user: User) -> dict:
    """Delete the user with its sessions, QR tokens and the failures / lockouts of its login (no audit here)."""
    removed = {"sessions_deleted": db.execute(delete(UserSession).where(UserSession.user_id == user.id)).rowcount,
               "qr_tokens_deleted": db.execute(delete(LoginToken).where(LoginToken.user_id == user.id)).rowcount}
    removed.update(ratelimit.forget_login(db, user.login))
    db.delete(user)
    db.flush()
    return removed


def delete_user(db: Session, actor: audit.Actor, user: User, now: datetime) -> dict:
    """Delete a user created by mistake. Refused (UserError) if anything references it: then disable instead."""
    found = traces(db, user)
    if found:
        raise UserError(f"{user.login} cannot be deleted, it has traces ({_describe(found)}); "
                        f"disable it instead")
    user_id, details = user.id, {"login": user.login, "display_name": user.display_name, "role": user.role.value}
    details.update(_erase(db, user))
    audit.record(db, actor, "user.delete", now, object_type="user", object_id=user_id, details=details)
    return details


def imports(db: Session) -> list[AuditLog]:
    """`user import` runs, newest first (their audit rows: id, time, file, logins)."""
    return list(db.execute(select(AuditLog).where(AuditLog.action == "user.import")
                           .order_by(AuditLog.id.desc())).scalars())


def rolled_back_imports(db: Session) -> set:
    rows = db.execute(select(AuditLog.object_id).where(AuditLog.action == "user.import_rollback")).scalars()
    return {int(x) for x in rows if x and x.isdigit()}


def import_rollback(db: Session, actor: audit.Actor, import_id: int, now: datetime) -> list:
    """Delete every user of one `user import`, if none of them has logged in or left any trace. Returns logins."""
    row = db.get(AuditLog, import_id)
    if row is None or row.action != "user.import":
        raise UserError(f"no user import with id {import_id} (see: judgectl user imports)")
    if import_id in rolled_back_imports(db):
        raise UserError(f"import {import_id} was already rolled back")
    ids = (row.details or {}).get("user_ids")
    if ids:
        found = list(db.execute(select(User).where(User.id.in_(ids)).order_by(User.id)).scalars())
    else:  # imports of session 4a.1 recorded only logins
        found = list(db.execute(select(User).where(User.login.in_((row.details or {}).get("logins", [])),
                                                   User.role == Role.student).order_by(User.id)).scalars())
    if not found:
        raise UserError(f"import {import_id}: none of its users exists any more")
    logged_in = [u.login for u in found if u.last_login_at is not None
                 or db.execute(select(UserSession.id).where(UserSession.user_id == u.id)).first() is not None]
    if logged_in:
        raise UserError(f"import {import_id} cannot be rolled back, these users have logged in: "
                        f"{', '.join(logged_in)} (disable them instead)")
    with_traces = {u.login: traces(db, u) for u in found}
    with_traces = {login: t for login, t in with_traces.items() if t}
    if with_traces:
        raise UserError(f"import {import_id} cannot be rolled back, these users have traces: "
                        + "; ".join(f"{login} ({_describe(t)})" for login, t in with_traces.items()))
    logins = [u.login for u in found]
    for u in found:
        _erase(db, u)
    audit.record(db, actor, "user.import_rollback", now, object_type="audit_log", object_id=import_id,
                 details={"logins": logins, "missing": len(ids or (row.details or {}).get("logins", [])) - len(logins)})
    return logins
