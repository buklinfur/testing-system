"""work_draft_files: files a student uploads to the draft of an assignment before handing it in (4b.2, D-097, D-098)

Revision ID: c5a9e3f1d27b
Revises: f7d82e85a178
Create Date: 2026-11-15 12:00:00

Written by hand (docs/DATA_MODEL.md «Изменения 4б.2»). One row per uploaded file of the pair (item, student), private
to the student until a hand-in names it; then the row goes and its blob becomes a file of the hand-in. A file the
student deletes keeps its row (deleted_at) until the reaper removes it JUDGE_DRAFT_TTL_DAYS after the upload: the
quota counts it until then. Names are unique among the live files of the pair, case ignored (lower(): the databases
are C.UTF-8, D-080). item and user CASCADE (the item is deleted only as a draft, which has no student drafts; the user
is deleted only without traces, and drafts are none), the blob RESTRICT, as every other file. The downgrade is refused
while any draft file exists: they are students' work not handed in yet.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a9e3f1d27b'
down_revision: Union[str, Sequence[str], None] = 'f7d82e85a178'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'work_draft_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('blob_sha256', sa.String(length=64), nullable=False),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("btrim(name, E' \\t\\r\\n') <> '' AND char_length(name) <= 150",
                           name=op.f('ck_work_draft_files_name_valid')),
        sa.CheckConstraint('size > 0', name=op.f('ck_work_draft_files_size_positive')),
        sa.CheckConstraint('deleted_at IS NULL OR deleted_at >= created_at',
                           name=op.f('ck_work_draft_files_deleted_after_created')),
        sa.ForeignKeyConstraint(['blob_sha256'], ['blobs.sha256'], name=op.f('fk_work_draft_files_blob_sha256_blobs'),
                                ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['item_id'], ['feed_items.id'], name=op.f('fk_work_draft_files_item_id_feed_items'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_work_draft_files_user_id_users'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_work_draft_files')),
    )
    op.create_index('ix_work_draft_files_owner', 'work_draft_files', ['user_id', 'item_id', 'id'], unique=False)
    op.create_index('ix_work_draft_files_created', 'work_draft_files', ['created_at'], unique=False)
    op.create_index('uq_work_draft_files_name', 'work_draft_files', ['item_id', 'user_id', sa.text('lower(name)')],
                    unique=True, postgresql_where=sa.text('deleted_at IS NULL'))


def downgrade() -> None:
    """Refused (nothing changed) while any draft file exists, deleted ones included."""
    n = op.get_bind().execute(sa.text("SELECT count(*) FROM work_draft_files")).scalar_one()
    if n:
        raise RuntimeError(f"draft files of students: {n}; the old schema cannot hold them, wait for the reaper or "
                           "delete them before downgrading (nothing was changed)")
    op.drop_index('uq_work_draft_files_name', table_name='work_draft_files',
                  postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('ix_work_draft_files_created', table_name='work_draft_files')
    op.drop_index('ix_work_draft_files_owner', table_name='work_draft_files')
    op.drop_table('work_draft_files')
