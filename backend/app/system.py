"""One look at the installation for judgectl status and GET /api/admin/system: migrations, load, queue metrics,
workers, configuration. The blob store is looked at only by judgectl (it reads the disk; MODULE_4A 5.7).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import queue as q
from .models import Worker


def migration_head() -> Optional[str]:
    """The newest migration the code knows."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory(str(Path(__file__).resolve().parent.parent / "migrations")).get_current_head()


def migration_current(db: Session) -> Optional[str]:
    if not db.execute(text("SELECT to_regclass('alembic_version') IS NOT NULL")).scalar():
        return None
    return db.execute(text("SELECT version_num FROM alembic_version")).scalar()


def config_problems(settings) -> list:
    """What would stop the public app from starting (the same check as app.public_main)."""
    from .public_main import PublicConfigError, check_config  # here: judgectl needs FastAPI only for this

    try:
        check_config(settings)
    except PublicConfigError as exc:
        return [line.strip(" -") for line in str(exc).splitlines()[1:]]
    return []


def snapshot(db: Session, settings) -> dict:
    """Call on a transaction that has not read anything yet: every number is of one moment (q.read_one_moment)."""
    q.read_one_moment(db)
    current = migration_current(db)
    head = migration_head()
    return {
        "migration": {"current": current, "head": head, "ok": current == head},
        "load": q.load_snapshot(db, settings.queue).to_dict(),
        "metrics": q.collect_metrics(db, settings.queue),
        "workers": q.worker_table(db, settings.queue),
        "workers_known": db.execute(select(func.count()).select_from(Worker)).scalar_one(),
        "config_problems": config_problems(settings),
        "timezone": settings.timezone,
    }
