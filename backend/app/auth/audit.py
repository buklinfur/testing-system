"""audit_log (R18): every administrative action, from the web or the console, and admin logins."""
from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import AuditLog


@dataclass(frozen=True)
class Actor:
    """Who does something: a web user (user_id) or a console user (cli = "cli:<unix user>")."""

    user_id: Optional[int] = None
    cli: Optional[str] = None
    ip: Optional[str] = None
    sudo_user: Optional[str] = None  # the human behind `sudo -u judge judgectl ...`

    @property
    def label(self) -> str:
        return self.cli if self.cli else f"user:{self.user_id}"


def cli_actor() -> Actor:
    return Actor(cli=f"cli:{pwd.getpwuid(os.geteuid()).pw_name}", sudo_user=os.environ.get("SUDO_USER") or None)


def record(db: Session, actor: Actor, action: str, now: datetime, *, object_type: Optional[str] = None,
           object_id: Any = None, details: Optional[dict] = None) -> AuditLog:
    details = dict(details or {})
    if actor.sudo_user:
        details["sudo_user"] = actor.sudo_user
    row = AuditLog(at=now, actor_user_id=actor.user_id, actor_cli=actor.cli, action=action, object_type=object_type,
                   object_id=None if object_id is None else str(object_id), ip=actor.ip, details=details)
    db.add(row)
    return row
