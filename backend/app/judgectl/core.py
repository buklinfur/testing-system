"""Shared plumbing of judgectl: the context (database, settings, terminal), errors, output and secrets input."""
from __future__ import annotations

import getpass as _getpass
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import BinaryIO, Callable, Iterator, Optional, TextIO

from sqlalchemy.orm import Session

from ..auth.audit import Actor
from ..auth.passwords import MIN_LENGTH, PasswordPolicyError, check_policy
from ..auth.totp import SecretBox, TotpConfigError
from ..models import Role
from ..settings import Settings
from ..jsonable import plain
from ..timeutil import TimezoneError, display, zone

PASSWORD_ATTEMPTS = 3


class CliError(Exception):
    """A user-facing error: printed as `judgectl: <message>`, exit code 1."""


def _stdout() -> TextIO:
    return sys.stdout


def _stderr() -> TextIO:
    return sys.stderr


def _stdin() -> BinaryIO:
    return sys.stdin.buffer


@dataclass
class Context:
    """Everything a command touches; tests replace the database, the terminal and the clock."""

    session_factory: Callable[[], Session]
    settings: Settings
    actor: Actor
    out: TextIO = field(default_factory=_stdout)
    err: TextIO = field(default_factory=_stderr)
    getpass: Callable[[str], str] = _getpass.getpass
    input: Callable[[str], str] = input
    stdin: BinaryIO = field(default_factory=_stdin)  # `-` as a file argument: a zip or a contest.yaml piped in
    interactive: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty()
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    json: bool = False  # --json of the current command

    def print(self, *parts) -> None:
        print(*parts, file=self.out)

    def note(self, message: str) -> None:
        """Hints and progress go to stderr: stdout stays parseable (`... | tail -1`, --json)."""
        print(message, file=self.err)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """One transaction per command: commit on success, roll back on any error."""
        db = self.session_factory()
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def box(self) -> SecretBox:
        try:
            return SecretBox(self.settings.auth.secret_key)
        except TotpConfigError as exc:
            raise CliError(str(exc)) from exc

    @property
    def tz(self):
        """JUDGE_TIMEZONE for tables (--json is always UTC)."""
        try:
            return zone(self.settings.timezone)
        except TimezoneError as exc:
            raise CliError(str(exc)) from exc

    def time(self, value: Optional[datetime]) -> str:
        return display(value, self.tz)

    def require_terminal(self, what: str) -> None:
        if not self.interactive():
            raise CliError(f"{what} needs an interactive terminal: the password and the 2FA code are typed in")


def confirm(ctx: Context, expected: str, what: str, yes: bool) -> None:
    """An irreversible step: the operator types `expected` again (or passes --yes in scripts)."""
    if yes:
        return
    if not ctx.interactive():
        raise CliError(f"{what} cannot be undone: confirm it in a terminal or pass --yes")
    if ctx.input(f"{what} cannot be undone. Type {expected} to confirm: ").strip() != expected:
        raise CliError("not confirmed, nothing changed")


def read_input(ctx: Context, source: str, limit: int, what: str) -> bytes:
    """The bytes of a file argument, or of stdin for `-` (how a user hands a file to judgectl running as judge)."""
    if source == "-":
        data = ctx.stdin.read(limit + 1)
        if not data:
            raise CliError(f"nothing on stdin: pipe {what} into it")
    else:
        try:
            with open(source, "rb") as f:
                data = f.read(limit + 1)
        except OSError as exc:
            raise CliError(f"cannot read {source}: {exc.strerror} (judgectl runs as judge: give the file on stdin "
                           f"with `-`)") from exc
    if len(data) > limit:
        raise CliError(f"{source}: bigger than {limit // (1024 * 1024)} MB")
    return data


def spool(stream: BinaryIO, dest, limit: int, what: str) -> int:
    """Copy a stream into the file `dest` in chunks; CliError as soon as it passes `limit` or if it is empty."""
    total = 0
    with open(dest, "wb") as out:
        while chunk := stream.read(1 << 20):
            total += len(chunk)
            if total > limit:
                raise CliError(f"stdin: bigger than {limit // (1024 * 1024)} MB")
            out.write(chunk)
    if not total:
        raise CliError(f"nothing on stdin: pipe {what} into it")
    return total


# ----------------------------------------------------------------------------- output
jsonable = plain  # one conversion for judgectl --json and the admin API (app/jsonable.py)


def emit_json(ctx: Context, data) -> None:
    ctx.print(json.dumps(jsonable(data), ensure_ascii=False, indent=2))


def cell(value, tz=timezone.utc) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return display(value, tz)
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def table(ctx: Context, rows: list[dict], columns: list[tuple[str, str]]) -> None:
    """A plain aligned table; `columns` are (key, header)."""
    if not rows:
        ctx.print("(none)")
        return
    tz = ctx.tz
    cells = [[cell(row.get(key), tz) for key, _ in columns] for row in rows]
    widths = [max([len(header)] + [len(line[i]) for line in cells]) for i, (_, header) in enumerate(columns)]
    for line in [[header for _, header in columns]] + cells:
        ctx.print("  ".join(text.ljust(width) for text, width in zip(line, widths)).rstrip())


def show(ctx: Context, rows: list[dict], columns: list[tuple[str, str]]) -> None:
    if ctx.json:
        emit_json(ctx, rows)
    else:
        table(ctx, rows, columns)


def open_private_new(path: str) -> TextIO:
    """A new file only this user can read (0600); an existing file is never overwritten."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError as exc:
        raise CliError(f"{path} already exists: refusing to overwrite it, choose another --out") from exc
    except OSError as exc:
        raise CliError(f"cannot create {path}: {exc.strerror}") from exc
    os.fchmod(fd, 0o600)  # in case of an unusual umask
    return os.fdopen(fd, "w", encoding="utf-8-sig", newline="")  # BOM + ';': Excel on Windows opens it as is


# ----------------------------------------------------------------------------- secrets typed in
def ask_new_password(ctx: Context, login: str, role: Role) -> str:
    """Typed twice without echo (getpass); never printed, logged or stored anywhere but as a hash."""
    for _ in range(PASSWORD_ATTEMPTS):
        first = ctx.getpass(f"New password for {login} (at least {MIN_LENGTH[role]} characters): ")
        try:
            check_policy(first, login, role)
        except PasswordPolicyError as exc:
            ctx.note(f"  {exc}")
            continue
        if ctx.getpass("Repeat it: ") != first:
            ctx.note("  the two entries differ")
            continue
        return first
    raise CliError(f"no acceptable password after {PASSWORD_ATTEMPTS} attempts, nothing changed")
