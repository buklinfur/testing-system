"""seq of the events of a work: the order hand-ins and reviews were applied in under the lock of the work (D-102)

Revision ID: d2f4a6b8c0e1
Revises: c5a9e3f1d27b
Create Date: 2026-11-16 10:00:00

Written by hand (docs/DATA_MODEL.md «Изменения 4б.2»). A hand-in over a grade the student has not seen is accepted
since D-102 and takes that grade off the work, so which came first decides the work. The moments cannot tell it: a
request keeps the moment it was received (D-041) and may wait for the lock of the work while another change commits.
So every hand-in (assignment_submissions) and review (assignment_reviews) gets `seq`, the next number of its work
(assignment_works.last_seq), taken under that lock; the events of a work are ordered by seq.

The rows already there are numbered in the order the code read them until now: by moment, a hand-in before a review
at the same moment, then by id (a copy of works.events of before; no hand-in over a grade was possible then, so that
order was the order they were applied in). The downgrade drops the columns: nothing else depends on them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2f4a6b8c0e1'
down_revision: Union[str, Sequence[str], None] = 'c5a9e3f1d27b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKFILL = """
WITH ev AS (
    SELECT 'submission' AS kind, id, work_id, created_at AS at, 0 AS rank FROM assignment_submissions
    UNION ALL
    SELECT 'review', id, work_id, at, 1 FROM assignment_reviews
), numbered AS (
    SELECT kind, id, work_id, row_number() OVER (PARTITION BY work_id ORDER BY at, rank, id) AS seq FROM ev
), s AS (
    UPDATE assignment_submissions t SET seq = n.seq FROM numbered n WHERE n.kind = 'submission' AND n.id = t.id
), r AS (
    UPDATE assignment_reviews t SET seq = n.seq FROM numbered n WHERE n.kind = 'review' AND n.id = t.id
)
UPDATE assignment_works w SET last_seq = m.last FROM (SELECT work_id, max(seq) AS last FROM numbered GROUP BY work_id) m
WHERE m.work_id = w.id
"""


def upgrade() -> None:
    op.add_column('assignment_works', sa.Column('last_seq', sa.Integer(), server_default=sa.text('0'), nullable=False))
    op.create_check_constraint(op.f('ck_assignment_works_last_seq_non_negative'), 'assignment_works', 'last_seq >= 0')
    for table in ('assignment_submissions', 'assignment_reviews'):
        op.add_column(table, sa.Column('seq', sa.Integer(), nullable=True))
    op.execute(BACKFILL)
    for table in ('assignment_submissions', 'assignment_reviews'):
        op.alter_column(table, 'seq', nullable=False)
        op.create_check_constraint(op.f(f'ck_{table}_seq_positive'), table, 'seq > 0')
        op.create_index(f'uq_{table}_work_seq', table, ['work_id', 'seq'], unique=True)


def downgrade() -> None:
    for table in ('assignment_reviews', 'assignment_submissions'):
        op.drop_index(f'uq_{table}_work_seq', table_name=table)
        op.drop_constraint(op.f(f'ck_{table}_seq_positive'), table, type_='check')
        op.drop_column(table, 'seq')
    op.drop_constraint(op.f('ck_assignment_works_last_seq_non_negative'), 'assignment_works', type_='check')
    op.drop_column('assignment_works', 'last_seq')
