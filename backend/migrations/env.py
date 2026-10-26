"""Alembic environment. The database URL comes from DATABASE_URL (app.settings), not from alembic.ini.

    DATABASE_URL=postgresql+psycopg://... alembic upgrade head
    DATABASE_URL=... alembic revision --autogenerate -m "..."   # then review the file by hand
"""
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/: the package is imported as `app`

from app.models import Base  # noqa: E402
from app.settings import Settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    return Settings.from_env().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (alembic upgrade --sql) without connecting."""
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")  # given by tests (tests/pg/test_migrations_pg.py)
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
