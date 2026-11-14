"""contest_entry_attempts: attempts of students to enter a contest (4b.1, D-093)

Revision ID: f7d82e85a178
Revises: f2e12ff59f1c
Create Date: 2026-11-14 11:00:00

Written by hand (docs/DATA_MODEL.md «Изменения 4б.1»). One row per attempt to enter a contest, right or wrong: the
limit JUDGE_ENTER_RATE (attempts per 10 minutes of one student) is counted from these rows, so that it holds across
several processes of the public app and survives a restart. Both foreign keys CASCADE (a log of attempts, nothing
refers to it); contest_id is empty when the slug named no contest the student may see. The downgrade drops the table
(rows of a rate limit, nothing else depends on them).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7d82e85a178'
down_revision: Union[str, Sequence[str], None] = 'f2e12ff59f1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'contest_entry_attempts',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('contest_id', sa.Integer(), nullable=True),
        sa.Column('outcome', sa.String(length=40), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], name=op.f('fk_contest_entry_attempts_contest_id_contests'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_contest_entry_attempts_user_id_users'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_contest_entry_attempts')),
    )
    op.create_index('ix_contest_entry_attempts_user_at', 'contest_entry_attempts', ['user_id', 'at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_contest_entry_attempts_user_at', table_name='contest_entry_attempts')
    op.drop_table('contest_entry_attempts')
