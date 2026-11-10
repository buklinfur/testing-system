"""Account management shared by judgectl and the admin API: create, reset, enable/disable, 2FA enrolment.

Every change is written to audit_log with its actor. Functions flush but never commit: the caller owns
the transaction (session_scope in judgectl, the request session in the API).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (AssignmentReview, AssignmentWork, Attachment, AuditLog, AuthLockout, Comment, CommentThread,
                      Contest, ContestGroup, DeadlineExtension, FeedItem, FeedItemAudience, Group, GroupMember,
                      Judgement, LoginToken, Participation, Problem, ProblemVersion, Role, Submission, ThreadRead,
                      User, UserSession, VersionCheck)
from ..timeutil import iso_utc
from . import audit, login_tokens, ratelimit, sessions
from .logins import LoginError, checked_login, default_display_name, default_login, unique_login
from .passwords import PasswordPolicyError, check_policy, generate_password, hash_password
from .totp import SecretBox, match_code, new_secret


class UserError(ValueError):
    """A refusal with a stable `code` for the API (docs/ADMIN_API.md) and optional `data` (traces, logins)."""

    def __init__(self, message: str, code: str = "user_error", **data):
        super().__init__(message)
        self.code, self.data = code, data


def find_by_login(db: Session, raw_login: str) -> Optional[User]:
    return db.execute(select(User).where(User.login == raw_login.strip().lower())).scalar_one_or_none()


def get_by_login(db: Session, raw_login: str) -> User:
    user = find_by_login(db, raw_login)
    if user is None:
        raise UserError(f"no user {raw_login.strip().lower()!r}", "user_not_found")
    return user


def _check_policy(password: str, login: str, role: Role) -> None:
    try:
        check_policy(password, login, role)
    except PasswordPolicyError as exc:
        raise UserError(str(exc), "password_policy") from exc


def _taken(db: Session):
    return lambda login: db.execute(select(User.id).where(User.login == login)).first() is not None


def _explicit_login(db: Session, raw: str) -> str:
    try:
        login = checked_login(raw)
    except LoginError as exc:
        raise UserError(str(exc), "login_invalid") from exc
    if _taken(db)(login):
        raise UserError(f"login {login!r} is already taken", "login_taken")
    return login


def create_student(db: Session, actor: audit.Actor, now: datetime, *, surname: str = "", first_name: str = "",
                   login: Optional[str] = None, display_name: Optional[str] = None,
                   timezone: Optional[str] = None) -> tuple[User, str]:
    """(user, generated password). Login: as given, or surname.initial with a number on collision. `timezone`:
    an IANA name or None (checked before anything is created)."""
    timezone = checked_timezone(timezone)
    if login:
        login = _explicit_login(db, login)
    else:
        try:
            login = unique_login(default_login(surname, first_name), _taken(db))
        except LoginError as exc:
            raise UserError(str(exc), "login_invalid") from exc
    display_name = (display_name or "").strip() or default_display_name(surname, first_name)
    if not display_name:
        raise UserError("a display name is required", "display_name_required")
    password = generate_password()
    user = User(login=login, display_name=display_name[:128], role=Role.student, is_active=True,
                password_hash=hash_password(password), password_changed_at=now, timezone=timezone)
    db.add(user)
    db.flush()
    audit.record(db, actor, "user.create", now, object_type="user", object_id=user.id,
                 details={"login": login, "role": "student", **({"timezone": timezone} if timezone else {})})
    return user, password


def create_admin(db: Session, actor: audit.Actor, now: datetime, *, login: str, display_name: str,
                 password: str) -> User:
    """An admin with the password they chose (>= 12). 2FA is enrolled right after (enroll_totp + confirm_totp)."""
    login = _explicit_login(db, login)
    _check_policy(password, login, Role.admin)
    if not display_name.strip():
        raise UserError("a display name is required", "display_name_required")
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
            raise UserError("an admin password is chosen by the admin, not generated", "admin_in_console")
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


def checked_timezone(raw: Optional[str]) -> Optional[str]:
    """An IANA name the server knows ('Asia/Novosibirsk', 'UTC'), or None for empty: the UI then uses the browser's
    zone (D-024). UserError otherwise; the API answers it with 422."""
    name = (raw or "").strip()
    if not name:
        return None
    if len(name) > 64 or name.startswith("/") or ".." in name:
        raise UserError(f"not a time zone: {name!r}", "timezone_invalid")
    try:
        return ZoneInfo(name).key
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UserError(f"not a time zone the server knows: {name!r} (e.g. Asia/Novosibirsk, Europe/Moscow)",
                        "timezone_invalid") from exc


def set_timezone(db: Session, actor: audit.Actor, user: User, raw: Optional[str], now: datetime) -> Optional[str]:
    """Only how times are shown to this user; stored times stay UTC. Returns the zone, None when cleared."""
    name = checked_timezone(raw)
    if name != user.timezone:
        old, user.timezone = user.timezone, name
        audit.record(db, actor, "user.set_timezone", now, object_type="user", object_id=user.id,
                     details={"old": old, "new": name})
    return name


def unlock(db: Session, actor: audit.Actor, raw_login: str, now: datetime) -> bool:
    login = raw_login.strip().lower()
    was_locked = ratelimit.unlock(db, now, actor.label, login=login)
    audit.record(db, actor, "user.unlock", now, object_type="login", object_id=login,
                 details={"was_locked": was_locked})
    return was_locked


def unlock_ip(db: Session, actor: audit.Actor, ip: str, now: datetime) -> bool:
    """Lift the lockout of an IP address (a whole class behind one school address) and reset its counter."""
    import ipaddress

    try:
        ip = str(ipaddress.ip_address(ip.strip()))
    except ValueError as exc:
        raise UserError(f"not an IP address: {ip!r}", "invalid_ip") from exc
    was_locked = ratelimit.unlock(db, now, actor.label, ip=ip)
    audit.record(db, actor, "ip.unlock", now, object_type="ip", object_id=ip, details={"was_locked": was_locked})
    return was_locked


def issue_login_tokens(db: Session, actor: audit.Actor, people: list, settings, now: datetime) -> list:
    """R10 for a whole list of students at once: [(user, token, expires_at)], one audit record for the batch (the
    ids, never the tokens). Every user is checked before any token is made: an admin -> admin_in_console, a disabled
    student -> qr_not_allowed."""
    for user in people:
        if user.role != Role.student:
            raise UserError(f"{user.login} is an admin: QR login is for students only", "admin_in_console")
        if not user.is_active:
            raise UserError(f"{user.login} is disabled", "qr_not_allowed")
    out = []
    for user in people:
        token, expires = login_tokens.issue(db, user, settings, now)
        out.append((user, token, expires))
    audit.record(db, actor, "user.qr_tokens", now, object_type="users", object_id=len(people),
                 details={"user_ids": [u.id for u in people],
                          "expires_at": iso_utc(out[0][2]) if out else None})
    return out


def issue_login_token(db: Session, actor: audit.Actor, user: User, settings, now: datetime) -> tuple[str, datetime]:
    """One student: issue_login_tokens with a list of one."""
    (_user, token, expires), = issue_login_tokens(db, actor, [user], settings, now)
    return token, expires


def enroll_totp(db: Session, actor: audit.Actor, user: User, box: SecretBox, now: datetime) -> str:
    """A new TOTP secret (returned in clear once, for the QR). Until confirm_totp the admin cannot log in.

    Used both for a new admin and for `admin reset-2fa`; the latter also revokes all sessions.
    """
    if user.role != Role.admin:
        raise UserError("2FA is for admins", "not_admin")
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
        raise UserError("no 2FA enrolment in progress", "no_enrolment")
    step = match_code(box.decrypt(user.totp_secret_enc), code, now, user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    user.totp_enabled_at = now
    audit.record(db, actor, "user.confirm_2fa", now, object_type="user", object_id=user.id)
    return True


# ----------------------------------------------------------------------------- deletion (R9 refined, MODULE_4A 5.5)
# Everything that would keep a trace of a user. A user with any of them is only disabled, never deleted.
# A student's own membership of groups is not a trace (В5): deleting the user deletes those rows, like sessions.
TRACES = (
    ("problems owned", Problem.owner_id),
    ("problem versions imported", ProblemVersion.imported_by),
    ("contests created", Contest.created_by),
    ("contest participations", Participation.user_id),
    ("submissions", Submission.user_id),
    ("rejudges requested", Judgement.requested_by),
    ("problem checks requested", VersionCheck.requested_by),
    ("audit records as actor (web actions or logins)", AuditLog.actor_user_id),
    ("groups created", Group.created_by),
    ("students added to groups", GroupMember.added_by),
    ("students removed from groups", GroupMember.removed_by),
    ("groups attached to contests", ContestGroup.added_by),
    ("deadline extensions given", DeadlineExtension.created_by),
    ("deadline extensions", DeadlineExtension.user_id),
    # the class feed (4v.1): what a teacher made and did, and a student's own work or personal addressing
    ("feed items created", FeedItem.created_by),
    ("feed addressees added", FeedItemAudience.added_by),
    ("feed items addressed personally", FeedItemAudience.user_id),
    ("assignment works", AssignmentWork.user_id),
    ("assignment reviews", AssignmentReview.reviewer_id),
    # comments (4v.2): what the user wrote or deleted, and a student's private threads; read marks are no trace
    ("comments written", Comment.author_id),
    ("comments deleted", Comment.deleted_by),
    ("private comment threads", CommentThread.student_id),
    # attachments (4v.3): the files and links a teacher put on feed items
    ("feed attachments added", Attachment.created_by),
)

# Rows that reference the user and are deleted with it (_erase): no trace. tests/pg/test_attachment_schema_pg.py
# checks that every foreign key to users is in TRACES or here.
ERASED = (UserSession.user_id, LoginToken.user_id, GroupMember.user_id, ThreadRead.user_id)


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
    """Delete the user with its sessions, QR tokens, group memberships, comment read marks and the failures /
    lockouts of its login (no audit here)."""
    removed = {"sessions_deleted": db.execute(delete(UserSession).where(UserSession.user_id == user.id)).rowcount,
               "qr_tokens_deleted": db.execute(delete(LoginToken).where(LoginToken.user_id == user.id)).rowcount,
               "group_memberships_deleted": db.execute(delete(GroupMember)
                                                       .where(GroupMember.user_id == user.id)).rowcount,
               "thread_reads_deleted": db.execute(delete(ThreadRead).where(ThreadRead.user_id == user.id)).rowcount}
    removed.update(ratelimit.forget_login(db, user.login))
    db.delete(user)
    db.flush()
    return removed


def delete_user(db: Session, actor: audit.Actor, user: User, now: datetime) -> dict:
    """Delete a user created by mistake. Refused (UserError) if anything references it: then disable instead."""
    found = traces(db, user)
    if found:
        raise UserError(f"{user.login} cannot be deleted, it has traces ({_describe(found)}); "
                        f"disable it instead", "has_traces", traces=found)
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
    """Delete every user of one `user import`, if none of them has logged in or left any trace, and the groups the
    import created that nothing else uses (K3). Returns {"deleted": logins, "groups_deleted", "groups_kept"}."""
    row = db.get(AuditLog, import_id)
    if row is None or row.action != "user.import":
        raise UserError(f"no user import with id {import_id} (see: judgectl user imports)", "import_not_found")
    if import_id in rolled_back_imports(db):
        raise UserError(f"import {import_id} was already rolled back", "already_rolled_back")
    ids = (row.details or {}).get("user_ids")
    if ids:
        found = list(db.execute(select(User).where(User.id.in_(ids)).order_by(User.id)).scalars())
    else:  # imports of session 4a.1 recorded only logins
        found = list(db.execute(select(User).where(User.login.in_((row.details or {}).get("logins", [])),
                                                   User.role == Role.student).order_by(User.id)).scalars())
    if not found:
        raise UserError(f"import {import_id}: none of its users exists any more", "import_empty")
    logged_in = [u.login for u in found if u.last_login_at is not None
                 or db.execute(select(UserSession.id).where(UserSession.user_id == u.id)).first() is not None]
    if logged_in:
        raise UserError(f"import {import_id} cannot be rolled back, these users have logged in: "
                        f"{', '.join(logged_in)} (disable them instead)", "users_logged_in", logins=logged_in)
    with_traces = {u.login: traces(db, u) for u in found}
    with_traces = {login: t for login, t in with_traces.items() if t}
    if with_traces:
        raise UserError(f"import {import_id} cannot be rolled back, these users have traces: "
                        + "; ".join(f"{login} ({_describe(t)})" for login, t in with_traces.items()), "has_traces",
                        traces=with_traces)
    logins = [u.login for u in found]
    for u in found:
        _erase(db, u)  # with their group memberships (В5)
    groups_deleted, groups_kept = _rollback_groups(db, (row.details or {}).get("groups_created", []))
    audit.record(db, actor, "user.import_rollback", now, object_type="audit_log", object_id=import_id,
                 details={"logins": logins, "missing": len(ids or (row.details or {}).get("logins", [])) - len(logins),
                          "groups_deleted": groups_deleted, "groups_kept": groups_kept})
    return {"deleted": logins, "groups_deleted": groups_deleted, "groups_kept": groups_kept}


def _rollback_groups(db: Session, group_ids: list) -> tuple[list, list]:
    """K3: the groups the import created are deleted (as if the import never happened) unless something else uses
    them now; existing groups the import added students to only lose those students."""
    from .. import groups as g

    deleted, kept = [], []
    for group_id in group_ids:
        group = db.get(Group, group_id)
        if group is None:
            continue
        why = g.why_kept_after_rollback(db, group)
        if why is None:
            deleted.append(group.name)
            db.delete(group)
        else:
            kept.append(f"{group.name}: {why}")
    db.flush()
    return deleted, kept


# ----------------------------------------------------------------------------- lists (judgectl and the admin API)
def active_locks(db: Session, now: datetime) -> dict:
    """login -> end of its active brute-force lockout."""
    rows = db.execute(select(AuthLockout.login, func.max(AuthLockout.until))
                      .where(AuthLockout.login.is_not(None), AuthLockout.cleared_at.is_(None), AuthLockout.until > now)
                      .group_by(AuthLockout.login)).all()
    return dict(rows)


def two_factor_state(user: User) -> str:
    if user.totp_enabled_at is not None:
        return "confirmed"
    return "pending" if user.totp_secret_enc else "none"


def users_query(q: Optional[str] = None, role: Optional[Role] = None, active: Optional[bool] = None,
                locked_logins: Optional[set] = None):
    """select(User) with the filters of the user list. q: a part of the login or the display name (no wildcards).
    locked_logins: only these logins (the locked ones)."""
    query = select(User)
    if q:
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(User.login.ilike(pattern, escape="\\") | User.display_name.ilike(pattern, escape="\\"))
    if role is not None:
        query = query.where(User.role == role)
    if active is not None:
        query = query.where(User.is_active.is_(active))
    if locked_logins is not None:
        query = query.where(User.login.in_(locked_logins))
    return query


def describe(db: Session, user: User, settings, now: datetime, locks: dict) -> dict:
    """What a list shows about a user: no secrets (no hashes, no TOTP secret)."""
    out = {"id": user.id, "login": user.login, "display_name": user.display_name, "role": user.role.value,
           "active": user.is_active, "created_at": user.created_at, "last_login_at": user.last_login_at,
           "last_login_ip": user.last_login_ip, "failures": ratelimit.current_failures(db, settings, now, user.login),
           "locked_until": locks.get(user.login), "timezone": user.timezone}
    if user.role == Role.admin:
        out["two_factor"] = two_factor_state(user)
    return out


# ----------------------------------------------------------------------------- class import (judgectl and the admin API)
def import_students(db: Session, actor: audit.Actor, parsed, now: datetime, source_name: str) -> tuple:
    """R7, all or nothing: (import id, [{surname, first_name, login, password, display_name, group}]). Any refused
    line (a taken or bad login, a class named like an archived group, ...) -> csvimport.CsvImportError with every
    line number; nothing stays (the caller rolls back). A class creates the group or adds to the one of that name
    (D-020). The audit record names the logins, ids and groups, never a password."""
    from .. import groups as g
    from .csvimport import CsvImportError

    created, problems = {}, []
    classes = {}  # lower-case name -> (name as first written, existing group or None)
    for row in parsed.rows:
        if row.group is None or row.group.lower() in classes:
            continue
        existing = g.live_named(db, row.group)
        if existing is None and g.archived_named(db, row.group):
            classes[row.group.lower()] = (row.group, "archived")
        else:
            classes[row.group.lower()] = (row.group, existing)
    for row in parsed.rows:
        if row.group is not None and classes[row.group.lower()][1] == "archived":
            problems.append((row.line, f"line {row.line}: class {row.group!r} is an archived group "
                                       "(judgectl group unarchive, or another name)"))
    # explicit logins first, so a generated one never takes a login asked for further down
    for i, row in sorted(enumerate(parsed.rows), key=lambda pair: pair[1].login is None):
        try:
            created[i] = create_student(db, actor, now, surname=row.surname, first_name=row.first_name,
                                        login=row.login, display_name=row.display_name)
        except UserError as exc:
            problems.append((row.line, f"line {row.line}: {exc}"))
    if problems:
        raise CsvImportError([text for _, text in sorted(problems)])
    results = []
    for i, row in enumerate(parsed.rows):
        user, password = created[i]
        results.append({"surname": row.surname, "first_name": row.first_name, "login": user.login,
                        "password": password, "display_name": user.display_name, "group": None})
    groups_created, groups_joined = [], []
    for key, (name, existing) in classes.items():
        group = existing or g.create(db, actor, name, now)
        (groups_joined if existing else groups_created).append(group.id)
        members = [created[i][0] for i, row in enumerate(parsed.rows) if row.group and row.group.lower() == key]
        g.add_members(db, actor, group, members, now)
        for i, row in enumerate(parsed.rows):
            if row.group and row.group.lower() == key:
                results[i]["group"] = group.name
    entry = audit.record(db, actor, "user.import", now, object_type="file", object_id=source_name,
                         details={"count": len(results), "encoding": parsed.encoding,
                                  "logins": [r["login"] for r in results],
                                  "user_ids": [created[i][0].id for i in range(len(parsed.rows))],
                                  "groups_created": groups_created, "groups_joined": groups_joined})
    db.flush()
    return entry.id, results
