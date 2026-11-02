"""judgectl admin ... and judgectl user ...: accounts through the service layer (app.auth.users).

Admins are managed only here (R2). Their passwords are typed twice without echo and their 2FA is enrolled
with a QR code in the terminal; both need a terminal. Generated student passwords are printed once (or
written to a new 0600 file by `user import`) and stored only as hashes.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import segno
from sqlalchemy.orm import Session

from ..auth import audit, csvimport, ratelimit, totp, users
from ..models import Role, User
from .core import CliError, Context, ask_new_password, confirm, emit_json, open_private_new, show

CODE_ATTEMPTS = 3


# ----------------------------------------------------------------------------- helpers
def _user(db: Session, login: str, role: Role) -> User:
    user = users.get_by_login(db, login)
    if user.role != role:
        other = "admin" if user.role == Role.admin else "user"
        raise CliError(f"{user.login!r} is {'an admin' if user.role == Role.admin else 'a student'}: "
                       f"use `judgectl {other} ...`")
    return user


_locks = users.active_locks
two_factor_state = users.two_factor_state


def _print_qr(ctx: Context, text: str) -> None:
    segno.make(text, error="m").terminal(out=ctx.out, compact=True)


def _enrol_2fa(ctx: Context, db: Session, user: User) -> bool:
    """A new secret (committed at once: the old one is gone either way), QR + otpauth URI, then the code."""
    box = ctx.box()
    secret = users.enroll_totp(db, ctx.actor, user, box, ctx.clock())
    db.commit()
    uri = totp.provisioning_uri(secret, user.login, ctx.settings.auth.totp_issuer)
    ctx.print(f"Scan this with an authenticator app (Google Authenticator, Aegis, ...) on the phone of {user.login}:")
    _print_qr(ctx, uri)
    ctx.print(f"or add it by hand: {uri}")
    for _ in range(CODE_ATTEMPTS):
        code = ctx.input("6-digit code from the app: ")
        if users.confirm_totp(db, ctx.actor, user, box, code, ctx.clock()):
            db.commit()
            return True
        ctx.note("  wrong code (is the phone's clock right?)")
    return False


def _not_confirmed(login: str) -> CliError:
    return CliError(f"2FA of {login} is NOT confirmed, so {login} cannot log in; "
                    f"enrol again with: judgectl admin reset-2fa {login}")


# ----------------------------------------------------------------------------- admin
def admin_add(ctx: Context, args) -> int:
    ctx.require_terminal("admin add")
    ctx.box()  # a missing JUDGE_SECRET_KEY is reported before anything is asked
    login = args.login.strip().lower()
    with ctx.session() as db:
        if users.find_by_login(db, login) is not None:
            raise CliError(f"login {login!r} is already taken")
    password = ask_new_password(ctx, login, Role.admin)  # no transaction stays open while someone types
    with ctx.session() as db:
        user = users.create_admin(db, ctx.actor, ctx.clock(), login=args.login, display_name=args.name,
                                  password=password)
        db.commit()
        ctx.print(f"Admin {user.login} created. Now the second factor (2FA).")
        if not _enrol_2fa(ctx, db, user):
            raise _not_confirmed(user.login)
    ctx.print(f"2FA confirmed: {user.login} can log in.")
    return 0


def admin_reset_2fa(ctx: Context, args) -> int:
    """Also the way to repeat an enrolment that was not confirmed."""
    ctx.require_terminal("admin reset-2fa")
    ctx.box()
    with ctx.session() as db:
        user = _user(db, args.login, Role.admin)
        if not _enrol_2fa(ctx, db, user):
            raise _not_confirmed(user.login)
    ctx.print(f"2FA confirmed: {user.login} can log in with the new code; all old sessions were ended.")
    return 0


def admin_reset_password(ctx: Context, args) -> int:
    ctx.require_terminal("admin reset-password")
    with ctx.session() as db:
        login = _user(db, args.login, Role.admin).login
    password = ask_new_password(ctx, login, Role.admin)
    with ctx.session() as db:
        user = _user(db, login, Role.admin)
        users.reset_password(db, ctx.actor, user, ctx.clock(), new_password=password)
    ctx.print(f"Password of {user.login} changed; its sessions were ended and its lockout lifted.")
    return 0


def _fails(ctx: Context, db: Session, login: str, now: datetime) -> int:
    return ratelimit.current_failures(db, ctx.settings.auth, now, login)


def admin_list(ctx: Context, args) -> int:
    """Active admins (--all: disabled ones too); FAILS counts towards the next lockout (limit: admin one)."""
    with ctx.session() as db:
        now = ctx.clock()
        locks = users.active_locks(db, now)
        query = users.users_query(role=Role.admin, active=None if args.all else True).order_by(User.login)
        rows = [users.describe(db, u, ctx.settings.auth, now, locks) for u in db.execute(query).scalars()]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("active", "ACTIVE"), ("two_factor", "2FA"),
                     ("last_login_at", "LAST LOGIN"), ("last_login_ip", "FROM"), ("failures", "FAILS"),
                     ("locked_until", "LOCKED UNTIL")])
    return 0


def _set_active(ctx: Context, login: str, role: Role, active: bool) -> int:
    with ctx.session() as db:
        user = _user(db, login, role)
        users.set_active(db, ctx.actor, user, active, ctx.clock())
    ctx.print(f"{user.login} {'enabled' if active else 'disabled (all sessions ended)'}")
    return 0


def admin_disable(ctx: Context, args) -> int:
    return _set_active(ctx, args.login, Role.admin, False)


def admin_enable(ctx: Context, args) -> int:
    return _set_active(ctx, args.login, Role.admin, True)


def admin_unlock(ctx: Context, args) -> int:
    with ctx.session() as db:
        user = _user(db, args.login, Role.admin)
        was = users.unlock(db, ctx.actor, user.login, ctx.clock())
    ctx.print(f"{user.login}: {'lockout lifted' if was else 'was not locked'}; the failure counter is reset")
    return 0


# ----------------------------------------------------------------------------- user (students)
def _student_out(user: User, password: Optional[str] = None) -> dict:
    out = {"login": user.login, "display_name": user.display_name}
    if password is not None:
        out["password"] = password
    return out


def user_add(ctx: Context, args) -> int:
    with ctx.session() as db:
        user, password = users.create_student(db, ctx.actor, ctx.clock(), surname=args.surname or "",
                                              first_name=args.first_name or "", login=args.login,
                                              display_name=args.name)
    show(ctx, [_student_out(user, password)], [("login", "LOGIN"), ("password", "PASSWORD"), ("display_name", "NAME")])
    if not ctx.json:
        ctx.note("The password is shown only now.")
    return 0


def user_import(ctx: Context, args) -> int:
    """All or nothing: any bad line (format, taken login) and no user is created; the result goes to a new
    0600 file (or stdout with --out -) and the file is removed again if the import does not commit."""
    try:
        data = Path(args.file).read_bytes()
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc.strerror}") from exc
    parsed = csvimport.parse(data)
    handle = None if args.out == "-" else open_private_new(args.out)  # before any change: refuse early
    try:
        with ctx.session() as db:
            import_id, results = users.import_students(db, ctx.actor, parsed, ctx.clock(), Path(args.file).name)
            if handle is not None:
                handle.write(csvimport.result_csv(results))
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
    except BaseException:
        if handle is not None:
            handle.close()
            os.unlink(args.out)
        raise
    ctx.note(f"import id {import_id}; undo while nobody has logged in: judgectl user import-rollback {import_id}")
    if handle is None:
        if ctx.json:
            emit_json(ctx, results)
        else:
            ctx.out.write(csvimport.result_csv(results).replace("\r\n", "\n"))
    elif ctx.json:
        emit_json(ctx, {"import_id": import_id, "created": len(results), "out": args.out,
                        "logins": [r["login"] for r in results]})
    else:
        ctx.print(f"{len(results)} students created ({parsed.encoding}, '{parsed.delimiter}'); "
                  f"logins and passwords are in {args.out} (0600, the passwords are nowhere else)")
        classes = sorted({r["group"] for r in results if r["group"]})
        if classes:
            ctx.print(f"groups: {', '.join(classes)} (judgectl group show <group>)")
    return 0


def user_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        now = ctx.clock()
        locks = users.active_locks(db, now)
        query = users.users_query(role=Role.student, active=None if args.all else True).order_by(User.login)
        rows = [users.describe(db, u, ctx.settings.auth, now, locks) for u in db.execute(query).scalars()]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("active", "ACTIVE"),
                     ("last_login_at", "LAST LOGIN"), ("failures", "FAILS"), ("locked_until", "LOCKED UNTIL")])
    return 0


def user_reset_password(ctx: Context, args) -> int:
    with ctx.session() as db:
        user = _user(db, args.login, Role.student)
        password = users.reset_password(db, ctx.actor, user, ctx.clock())
    show(ctx, [_student_out(user, password)], [("login", "LOGIN"), ("password", "PASSWORD"), ("display_name", "NAME")])
    if not ctx.json:
        ctx.note("The password is shown only now; the old sessions and QR tokens no longer work.")
    return 0


def user_disable(ctx: Context, args) -> int:
    return _set_active(ctx, args.login, Role.student, False)


def user_enable(ctx: Context, args) -> int:
    return _set_active(ctx, args.login, Role.student, True)


def user_unlock(ctx: Context, args) -> int:
    """By login (any login, even one that does not exist: failures are counted for those too) or by IP."""
    if (args.login is None) == (args.ip is None):
        raise CliError("give a LOGIN or --ip ADDRESS")
    with ctx.session() as db:
        now = ctx.clock()
        if args.ip is not None:
            was = users.unlock_ip(db, ctx.actor, args.ip, now)
            subject = f"IP {args.ip}"
        else:
            user = users.find_by_login(db, args.login)
            if user is not None and user.role == Role.admin:
                raise CliError(f"{user.login!r} is an admin: use `judgectl admin unlock`")
            was = users.unlock(db, ctx.actor, args.login, now)
            subject = args.login.strip().lower()
    ctx.print(f"{subject}: {'lockout lifted' if was else 'was not locked'}; the failure counter is reset")
    return 0


def user_set_timezone(ctx: Context, args) -> int:
    with ctx.session() as db:
        user = _user(db, args.login, Role.student)
        zone_name = users.set_timezone(db, ctx.actor, user, None if args.zone == "-" else args.zone, ctx.clock())
        login = user.login
    ctx.print(f"{login}: time zone {zone_name}" if zone_name else f"{login}: no time zone (the browser's is used)")
    return 0


def user_set_login(ctx: Context, args) -> int:
    with ctx.session() as db:
        user = _user(db, args.login, Role.student)
        old = user.login
        new = users.set_login(db, ctx.actor, user, args.new_login, ctx.clock())
    ctx.print(f"{old} -> {new}")
    return 0


def user_qr_token(ctx: Context, args) -> int:
    """A one-time login link for a student (7 days; a new one revokes the previous one)."""
    from ..public_main import PublicConfigError, canonical_origin  # here: the rest of judgectl needs no FastAPI

    with ctx.session() as db:
        user = _user(db, args.login, Role.student)
        (_u, token, expires), = users.issue_login_tokens(db, ctx.actor, [user], ctx.settings.auth, ctx.clock())
    try:
        url = f"{canonical_origin(ctx.settings.public_origin)}/login/qr#{token}"
    except PublicConfigError:
        url = None
        ctx.note("JUDGE_PUBLIC_ORIGIN is not set: only the token is shown, not the link")
    if ctx.json:
        emit_json(ctx, {"login": user.login, "token": token, "url": url, "expires_at": expires})
        return 0
    if url and not args.no_qr:
        _print_qr(ctx, url)
    ctx.print(f"{user.login}: valid until {ctx.time(expires)} ({ctx.settings.timezone}), one use")
    ctx.print(url or token)
    return 0


# ----------------------------------------------------------------------------- deletion, imports, failures
def _delete(ctx: Context, args, role: Role) -> int:
    with ctx.session() as db:
        user = _user(db, args.login, role)
        login, found = user.login, users.traces(db, user)
    if found:  # refuse before asking for a confirmation that could not help
        raise CliError(f"{login} cannot be deleted, it has traces ({', '.join(f'{k}: {n}' for k, n in found.items())}); "
                       f"disable it instead: judgectl {'admin' if role == Role.admin else 'user'} disable {login}")
    confirm(ctx, login, f"Deleting {login}", args.yes)
    with ctx.session() as db:
        details = users.delete_user(db, ctx.actor, _user(db, login, role), ctx.clock())
    if ctx.json:
        emit_json(ctx, details)
    else:
        ctx.print(f"{login} deleted (sessions: {details['sessions_deleted']}, QR tokens: {details['qr_tokens_deleted']}, "
                  f"failures: {details['failures_deleted']}, lockouts: {details['lockouts_deleted']})")
    return 0


def admin_delete(ctx: Context, args) -> int:
    return _delete(ctx, args, Role.admin)


def user_delete(ctx: Context, args) -> int:
    return _delete(ctx, args, Role.student)


def user_imports(ctx: Context, args) -> int:
    with ctx.session() as db:
        undone = users.rolled_back_imports(db)
        rows = [{"id": r.id, "at": r.at, "file": r.object_id, "count": (r.details or {}).get("count"),
                 "by": r.actor_cli + (f" ({r.details['sudo_user']})" if (r.details or {}).get("sudo_user") else ""),
                 "rolled_back": r.id in undone} for r in users.imports(db)]
    show(ctx, rows, [("id", "ID"), ("at", "AT"), ("file", "FILE"), ("count", "USERS"), ("by", "BY"),
                     ("rolled_back", "ROLLED BACK")])
    return 0


def user_import_rollback(ctx: Context, args) -> int:
    confirm(ctx, str(args.import_id), f"Deleting every user of import {args.import_id}", args.yes)
    with ctx.session() as db:
        result = users.import_rollback(db, ctx.actor, args.import_id, ctx.clock())
    if ctx.json:
        emit_json(ctx, {"import_id": args.import_id, **result})
    else:
        ctx.print(f"import {args.import_id} rolled back: {len(result['deleted'])} users deleted")
        if result["groups_deleted"]:
            ctx.print(f"  groups it created, deleted: {', '.join(result['groups_deleted'])}")
        for kept in result["groups_kept"]:
            ctx.print(f"  group kept, {kept}")
    return 0


def _failures(ctx: Context, args, role: Optional[Role]) -> int:
    """role=None: any login that is not an admin (failures exist for unknown logins too)."""
    login = args.login.strip().lower()
    with ctx.session() as db:
        user = users.find_by_login(db, login)
        if role == Role.admin:
            _user(db, login, Role.admin)
        elif user is not None and user.role == Role.admin:
            raise CliError(f"{login!r} is an admin: use `judgectl admin failures`")
        now = ctx.clock()
        rows = [{"at": f.at, "kind": f.kind, "ip": f.ip} for f in ratelimit.recent_failures(db, login, args.last)]
        counting = _fails(ctx, db, login, now)
        locked = _locks(db, now).get(login)
    limit = ctx.settings.auth.admin_login_fail_limit if role == Role.admin else ctx.settings.auth.login_fail_limit
    if ctx.json:
        emit_json(ctx, {"login": login, "counting": counting, "limit": limit, "locked_until": locked,
                        "failures": rows})
        return 0
    show(ctx, rows, [("at", "AT"), ("kind", "KIND"), ("ip", "IP")])
    ctx.print(f"{login}: {counting} of {limit} failures towards a lockout"
              + (f"; locked until {ctx.time(locked)}" if locked else ""))
    return 0


def admin_failures(ctx: Context, args) -> int:
    return _failures(ctx, args, Role.admin)


def user_failures(ctx: Context, args) -> int:
    return _failures(ctx, args, None)
