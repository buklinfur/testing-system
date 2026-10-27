"""Shared plumbing of judgectl: the context (database, settings, terminal), errors, output and secrets input."""
from __future__ import annotations

import getpass as _getpass
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Iterator, TextIO

from sqlalchemy.orm import Session

from ..auth.audit import Actor
from ..auth.passwords import MIN_LENGTH, PasswordPolicyError, check_policy
from ..auth.totp import SecretBox, TotpConfigError
from ..models import Role
from ..settings import Settings

PASSWORD_ATTEMPTS = 3


class CliError(Exception):
    """A user-facing error: printed as `judgectl: <message>`, exit code 1."""


def _stdout() -> TextIO:
    return sys.stdout


def _stderr() -> TextIO:
    return sys.stderr


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

    def require_terminal(self, what: str) -> None:
        if not self.interactive():
            raise CliError(f"{what} needs an interactive terminal: the password and the 2FA code are typed in")


# ----------------------------------------------------------------------------- output
def jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def emit_json(ctx: Context, data) -> None:
    ctx.print(json.dumps(jsonable(data), ensure_ascii=False, indent=2))


def cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def table(ctx: Context, rows: list[dict], columns: list[tuple[str, str]]) -> None:
    """A plain aligned table; `columns` are (key, header)."""
    if not rows:
        ctx.print("(none)")
        return
    cells = [[cell(row.get(key)) for key, _ in columns] for row in rows]
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
