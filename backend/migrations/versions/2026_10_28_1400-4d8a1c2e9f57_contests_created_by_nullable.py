"""contests.created_by nullable: a contest applied from the console has no creating web user (module 4a.2)

Revision ID: 4d8a1c2e9f57
Revises: 9e2f4a7c1b30
Create Date: 2026-10-28 14:00:00

The downgrade gives such contests to the first admin and refuses (nothing changed) if there is none.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4d8a1c2e9f57'
down_revision: Union[str, Sequence[str], None] = '9e2f4a7c1b30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('contests', 'created_by', existing_type=sa.INTEGER(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM contests WHERE created_by IS NULL")).scalar_one():
        admin = bind.execute(sa.text("SELECT min(id) FROM users WHERE role = 'admin'")).scalar()
        if admin is None:
            raise RuntimeError("contests without a creator exist and there is no admin to give them to; "
                               "create an admin first (nothing was changed)")
        bind.execute(sa.text("UPDATE contests SET created_by = :a WHERE created_by IS NULL"), {"a": admin})
    op.alter_column('contests', 'created_by', existing_type=sa.INTEGER(), nullable=False)
