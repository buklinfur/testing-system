"""judgements: max_score and the server's gave-up mark as columns; participations by user (4b.1, D-090)

Revision ID: f2e12ff59f1c
Revises: b8e0cbcc4cda
Create Date: 2026-11-13 15:41:00

Written by hand (docs/DATA_MODEL.md «Изменения 4б.1», docs/QUEUE.md):
* judgements.max_score Numeric(8, 2), null only while a judgement is not finished. Filled here for every finished
  one (finished and internal_error) from the problem version of the judgement: the points of the quiz questions of a
  choice / text_answer version (1 when not given, as runner/quiz.py counts them), otherwise the sum of the points of
  its test groups. The rule is copied below in SQL, not imported (a migration must keep doing what it did);
  tests/pg/test_migrations_pg.py checks it against app/scores.version_max_score. CHECK max_score >= 0.
* judgements.server_gave_up_at: the server ended the judgement itself (D-069). Until now the mark was the prefix
  "the server gave up after " of judgements.error; such rows (internal_error) get server_gave_up_at = finished_at and
  lose the prefix ("3 failed attempts; last reason: ..."). An error of a worker that only looked like the mark was
  stored with "worker: " in front of it (queue.finish); it stays as it is. CHECK: only an internal_error carries it.
* ix_participations_user: the contests of one student.

The downgrade loses nothing the old schema can hold: the prefix goes back into error for the rows the server gave up
on; an error of a worker written after the upgrade that starts like the old mark gets "worker: " in front of it, as
the old queue.finish did, so that the old code does not read it as the server's mark; max_score is dropped (the old
code never had it).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2e12ff59f1c'
down_revision: Union[str, Sequence[str], None] = 'b8e0cbcc4cda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MARK = "the server gave up after "

# the maximum of a problem version (copy of app/scores.version_max_score)
VERSION_MAX_SQL = """
    CASE pv.type::text
      WHEN 'choice' THEN (SELECT round(coalesce(sum(coalesce((q ->> 'points')::numeric, 1)), 0), 2)
                          FROM jsonb_array_elements(coalesce(pv.config -> 'questions', '[]'::jsonb)) q)
      WHEN 'text_answer' THEN (SELECT round(coalesce(sum(coalesce((a ->> 'points')::numeric, 1)), 0), 2)
                               FROM jsonb_array_elements(coalesce(pv.config -> 'answers', '[]'::jsonb)) a)
      ELSE (SELECT coalesce(sum(g.points), 0) FROM test_groups g WHERE g.version_id = pv.id)
    END
"""


def upgrade() -> None:
    op.add_column('judgements', sa.Column('max_score', sa.Numeric(precision=8, scale=2), nullable=True))
    op.add_column('judgements', sa.Column('server_gave_up_at', sa.DateTime(timezone=True), nullable=True))
    op.execute(f"""
        UPDATE judgements j SET max_score = {VERSION_MAX_SQL}
        FROM problem_versions pv
        WHERE pv.id = j.problem_version_id AND j.status IN ('finished', 'internal_error')
    """)
    op.execute(sa.text("""
        UPDATE judgements SET server_gave_up_at = coalesce(finished_at, now()), error = substr(error, :n + 1)
        WHERE status = 'internal_error' AND left(error, :n) = :mark
    """).bindparams(n=len(MARK), mark=MARK))
    op.create_check_constraint(op.f('ck_judgements_max_score_non_negative'), 'judgements',
                               'max_score IS NULL OR max_score >= 0')
    op.create_check_constraint(op.f('ck_judgements_gave_up_is_internal_error'), 'judgements',
                               "server_gave_up_at IS NULL OR status = 'internal_error'")
    op.create_index('ix_participations_user', 'participations', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_participations_user', table_name='participations')
    op.drop_constraint(op.f('ck_judgements_gave_up_is_internal_error'), 'judgements', type_='check')
    op.drop_constraint(op.f('ck_judgements_max_score_non_negative'), 'judgements', type_='check')
    op.execute(sa.text("""
        UPDATE judgements SET error = 'worker: ' || error
        WHERE server_gave_up_at IS NULL AND left(error, :n) = :mark
    """).bindparams(n=len(MARK), mark=MARK))
    op.execute(sa.text("""
        UPDATE judgements SET error = :mark || coalesce(error, '')
        WHERE server_gave_up_at IS NOT NULL
    """).bindparams(mark=MARK))
    op.drop_column('judgements', 'server_gave_up_at')
    op.drop_column('judgements', 'max_score')
