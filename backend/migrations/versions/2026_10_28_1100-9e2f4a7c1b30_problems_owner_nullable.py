"""problems.owner_id nullable: a problem imported from the console has no owning web user (module 4a.2)

Revision ID: 9e2f4a7c1b30
Revises: 6bc3b8bd1c68
Create Date: 2026-10-28 11:00:00

The downgrade gives ownerless problems to the first admin and refuses (nothing changed) if there is none.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9e2f4a7c1b30'
down_revision: Union[str, Sequence[str], None] = '6bc3b8bd1c68'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('problems', 'owner_id', existing_type=sa.INTEGER(), nullable=True)


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT count(*) FROM problems WHERE owner_id IS NULL")).scalar_one():
        admin = bind.execute(sa.text("SELECT min(id) FROM users WHERE role = 'admin'")).scalar()
        if admin is None:
            raise RuntimeError("problems without an owner exist and there is no admin to give them to; "
                               "create an admin first (nothing was changed)")
        bind.execute(sa.text("UPDATE problems SET owner_id = :a WHERE owner_id IS NULL"), {"a": admin})
    op.alter_column('problems', 'owner_id', existing_type=sa.INTEGER(), nullable=False)
