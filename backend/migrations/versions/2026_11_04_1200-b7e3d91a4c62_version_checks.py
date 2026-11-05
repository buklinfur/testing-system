"""checks of problem versions by workers (module 4a.4)

Revision ID: b7e3d91a4c62
Revises: 6fca2cdc55b2
Create Date: 2026-11-04 12:00:00

Written by hand (docs/MODULE_4A.md 5.9, D-048..D-055):
* the table version_checks and its enum types check_reason, check_status, check_outcome;
* every worker token that is not revoked and allows "cpu" also allows "problem_check" (the workers that run with
  it announce the capability once JUDGE_CAPABILITIES says so; a worker without it never gets a check).
The status of existing versions does not change: they stay ready (nothing was checked, nothing is queued).
The downgrade refuses (nothing changed) while version_checks has rows: their history would be lost, and the
versions they left pending_check or failed would mean nothing to the old code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7e3d91a4c62'
down_revision: Union[str, Sequence[str], None] = '6fca2cdc55b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK_REASON_T = postgresql.ENUM('import', 'recheck', name='check_reason', create_type=False)
CHECK_STATUS_T = postgresql.ENUM('queued', 'running', 'done', name='check_status', create_type=False)
CHECK_OUTCOME_T = postgresql.ENUM('ready', 'failed', name='check_outcome', create_type=False)
NEW_ENUM_TYPES = (CHECK_REASON_T, CHECK_STATUS_T, CHECK_OUTCOME_T)

ADD_CAPABILITY = """
UPDATE worker_tokens SET capabilities = capabilities || '["problem_check"]'::jsonb
 WHERE revoked_at IS NULL AND capabilities @> '["cpu"]'::jsonb AND NOT capabilities @> '["problem_check"]'::jsonb
"""
REMOVE_CAPABILITY = """
UPDATE worker_tokens SET capabilities = capabilities - 'problem_check' WHERE capabilities @> '["problem_check"]'::jsonb
"""
STRIP_WORKERS = """
UPDATE workers SET capabilities = capabilities - 'problem_check' WHERE capabilities @> '["problem_check"]'::jsonb
"""


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for enum_type in NEW_ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)
    op.create_table(
        'version_checks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('problem_version_id', sa.Integer(), nullable=False),
        sa.Column('reason', CHECK_REASON_T, nullable=False),
        sa.Column('status', CHECK_STATUS_T, nullable=False),
        sa.Column('outcome', CHECK_OUTCOME_T, nullable=True),
        sa.Column('worker_id', sa.Integer(), nullable=True),
        sa.Column('attempt', sa.Integer(), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('progress_done', sa.Integer(), nullable=True),
        sa.Column('progress_total', sa.Integer(), nullable=True),
        sa.Column('report', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('requested_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("(status = 'done') = (outcome IS NOT NULL)", name=op.f('ck_version_checks_outcome_when_done')),
        sa.ForeignKeyConstraint(['problem_version_id'], ['problem_versions.id'],
                                name=op.f('fk_version_checks_problem_version_id_problem_versions')),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id'], name=op.f('fk_version_checks_requested_by_users')),
        sa.ForeignKeyConstraint(['worker_id'], ['workers.id'], name=op.f('fk_version_checks_worker_id_workers')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_version_checks')),
    )
    op.create_index('ix_version_checks_queue', 'version_checks', ['created_at', 'id'], unique=False,
                    postgresql_where=sa.text("status = 'queued'"))
    op.create_index('ix_version_checks_version', 'version_checks', ['problem_version_id', 'id'], unique=False)
    op.create_index('uq_version_checks_active', 'version_checks', ['problem_version_id'], unique=True,
                    postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.execute(ADD_CAPABILITY)


def downgrade() -> None:
    """Downgrade schema.

    Refused (nothing changed) while any check exists. Otherwise problem_check is taken off every token and worker
    row (the old code knows no such capability), and the table and its types go.
    """
    bind = op.get_bind()
    n = bind.execute(sa.text("SELECT count(*) FROM version_checks")).scalar_one()
    if n:
        raise RuntimeError(f"version checks: {n}; the old schema cannot hold their history, delete them before "
                           "downgrading (nothing was changed)")
    op.execute(REMOVE_CAPABILITY)
    op.execute(STRIP_WORKERS)
    op.drop_index('uq_version_checks_active', table_name='version_checks',
                  postgresql_where=sa.text("status IN ('queued', 'running')"))
    op.drop_index('ix_version_checks_version', table_name='version_checks')
    op.drop_index('ix_version_checks_queue', table_name='version_checks', postgresql_where=sa.text("status = 'queued'"))
    op.drop_table('version_checks')
    for enum_type in reversed(NEW_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=False)
