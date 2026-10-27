"""judgectl admin ... and judgectl user ...: accounts through the service layer (app.auth.users).

Admins are managed only here (R2). Their passwords are typed twice without echo and their 2FA is enrolled
with a QR code in the terminal; both need a terminal. Generated student passwords are printed once (or
written to a new 0600 file by `user import`) and stored only as hashes.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import segno
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import audit, ratelimit, totp, users
from ..models import AuthLockout, Role, User
from . import csvimport
from .core import CliError, Context, ask_new_password, emit_json, open_private_new, show

CODE_ATTEMPTS = 3
OUT_HEADER = ["фамилия", "имя", "логин", "пароль", "display_name"]


# ----------------------------------------------------------------------------- helpers
def _user(db: Session, login: str, role: Role) -> User:
    user = users.get_by_login(db, login)
    if user.role != role:
        other = "admin" if user.role == Role.admin else "user"
        raise CliError(f"{user.login!r} is {'an admin' if user.role == Role.admin else 'a student'}: "
                       f"use `judgectl {other} ...`")
    return user


def _locks(db: Session, now: datetime) -> dict:
    """login -> end of its active brute-force lockout."""
    rows = db.execute(select(AuthLockout.login, func.max(AuthLockout.until))
                      .where(AuthLockout.login.is_not(None), AuthLockout.cleared_at.is_(None), AuthLockout.until > now)
                      .group_by(AuthLockout.login)).all()
    return dict(rows)


def two_factor_state(user: User) -> str:
    if user.totp_enabled_at is not None:
        return "confirmed"
    return "pending" if user.totp_secret_enc else "none"


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


def admin_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        now = ctx.clock()
        locks = _locks(db, now)
        rows = [{"login": u.login, "display_name": u.display_name, "active": u.is_active,
                 "two_factor": two_factor_state(u), "last_login_at": u.last_login_at, "last_login_ip": u.last_login_ip,
                 "locked_until": locks.get(u.login), "created_at": u.created_at}
                for u in db.execute(select(User).where(User.role == Role.admin).order_by(User.login)).scalars()]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("active", "ACTIVE"), ("two_factor", "2FA"),
                     ("last_login_at", "LAST LOGIN"), ("last_login_ip", "FROM"), ("locked_until", "LOCKED UNTIL")])
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
    results = []
    try:
        with ctx.session() as db:
            now = ctx.clock()
            created, problems = {}, []
            # explicit logins first, so a generated one never takes a login asked for further down
            for i, row in sorted(enumerate(parsed.rows), key=lambda pair: pair[1].login is None):
                try:
                    created[i] = users.create_student(db, ctx.actor, now, surname=row.surname,
                                                      first_name=row.first_name, login=row.login,
                                                      display_name=row.display_name)
                except users.UserError as exc:
                    problems.append((row.line, f"line {row.line}: {exc}"))
            if problems:
                raise csvimport.CsvImportError([text for _, text in sorted(problems)])
            for i, row in enumerate(parsed.rows):
                user, password = created[i]
                results.append({"surname": row.surname, "first_name": row.first_name, "login": user.login,
                                "password": password, "display_name": user.display_name})
            audit.record(db, ctx.actor, "user.import", now, object_type="file", object_id=Path(args.file).name,
                         details={"count": len(results), "encoding": parsed.encoding, "logins": [r["login"] for r in results]})
            if handle is not None:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow(OUT_HEADER)
                for r in results:
                    writer.writerow([r["surname"], r["first_name"], r["login"], r["password"], r["display_name"]])
                handle.flush()
                os.fsync(handle.fileno())
                handle.close()
    except BaseException:
        if handle is not None:
            handle.close()
            os.unlink(args.out)
        raise
    if handle is None:
        if ctx.json:
            emit_json(ctx, results)
        else:
            ctx.print(";".join(OUT_HEADER))
            for r in results:
                ctx.print(";".join([r["surname"], r["first_name"], r["login"], r["password"], r["display_name"]]))
    elif ctx.json:
        emit_json(ctx, {"created": len(results), "out": args.out, "logins": [r["login"] for r in results]})
    else:
        ctx.print(f"{len(results)} students created ({parsed.encoding}, '{parsed.delimiter}'); "
                  f"logins and passwords are in {args.out} (0600, the passwords are nowhere else)")
    return 0


def user_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        now = ctx.clock()
        locks = _locks(db, now)
        query = select(User).where(User.role == Role.student).order_by(User.login)
        if not args.all:
            query = query.where(User.is_active.is_(True))
        rows = [{"login": u.login, "display_name": u.display_name, "active": u.is_active,
                 "last_login_at": u.last_login_at, "locked_until": locks.get(u.login), "created_at": u.created_at}
                for u in db.execute(query).scalars()]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("active", "ACTIVE"),
                     ("last_login_at", "LAST LOGIN"), ("locked_until", "LOCKED UNTIL")])
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
            was = ratelimit.unlock(db, now, ctx.actor.label, ip=args.ip)
            audit.record(db, ctx.actor, "ip.unlock", now, object_type="ip", object_id=args.ip,
                         details={"was_locked": was})
            subject = f"IP {args.ip}"
        else:
            user = users.find_by_login(db, args.login)
            if user is not None and user.role == Role.admin:
                raise CliError(f"{user.login!r} is an admin: use `judgectl admin unlock`")
            was = users.unlock(db, ctx.actor, args.login, now)
            subject = args.login.strip().lower()
    ctx.print(f"{subject}: {'lockout lifted' if was else 'was not locked'}; the failure counter is reset")
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
        token, expires = users.issue_login_token(db, ctx.actor, user, ctx.settings.auth, ctx.clock())
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
    ctx.print(f"{user.login}: valid until {expires.astimezone():%Y-%m-%d %H:%M}, one use")
    ctx.print(url or token)
    return 0
