"""problems and contests (module 4a.2)

Revision ID: 6bc3b8bd1c68
Revises: c13b55a3e4d5
Create Date: 2026-10-28 01:57:35.834949

Written by hand on top of autogenerate (docs/MODULE_4A.md, section 5.6). Data is carried over:
* version_status draft/published/archived -> pending_check/ready/ready (archiving moves to problems.archived);
  published_at is dropped;
* contests: duration_minutes set -> timing_mode personal with personal_duration, else fixed; leaderboard_enabled
  -> visible/hidden; join_code set -> join_policy code with it as invite_code, else open; plagiarism_check ->
  plagiarism_enabled; state published; an empty starts_at gets created_at;
* contest_problems get problem_id from their version; when one contest holds several versions of one problem
  (dev_seed did that), the newest row stays visible and the older ones become hidden;
* participations: joined_at = started_at, started_at cleared in fixed contests;
* the languages of protocol/languages.py are inserted when missing.
Contests that cannot satisfy the new rules (a freeze with a personal timer, a non-positive duration, an end not
after the start, two contests with one join code) stop the migration before anything changes, with a list.
The downgrade maps back what the old schema can hold (see downgrade()).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from protocol.languages import SYSTEM_LANGUAGES

# revision identifiers, used by Alembic.
revision: str = '6bc3b8bd1c68'
down_revision: Union[str, Sequence[str], None] = 'c13b55a3e4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONTEST_STATE_T = postgresql.ENUM('draft', 'published', 'archived', name='contest_state', create_type=False)
TIMING_MODE_T = postgresql.ENUM('fixed', 'personal', name='timing_mode', create_type=False)
LEADERBOARD_MODE_T = postgresql.ENUM('hidden', 'visible', 'after_end', name='leaderboard_mode', create_type=False)
JOIN_POLICY_T = postgresql.ENUM('open', 'code', name='join_policy', create_type=False)
NEW_ENUM_TYPES = (CONTEST_STATE_T, TIMING_MODE_T, LEADERBOARD_MODE_T, JOIN_POLICY_T)
CONTEST_MODE_T = postgresql.ENUM('practice', 'timed', name='contest_mode', create_type=False)

CONTEST_CHECKS = (
    ("personal_duration", "(timing_mode = 'personal') = (personal_duration IS NOT NULL)"),
    ("personal_duration_positive", "personal_duration IS NULL OR personal_duration > interval '0'"),
    ("no_freeze_with_personal", "timing_mode <> 'personal' OR freeze_at IS NULL"),
    ("window", "ends_at IS NULL OR ends_at > starts_at"),
    ("invite_code", "join_policy <> 'code' OR invite_code IS NOT NULL"),
    ("non_negative", "submit_cooldown_sec >= 0 AND icpc_penalty_minutes >= 0"),
    ("max_submissions", "max_submissions_per_problem IS NULL OR max_submissions_per_problem > 0"),
)

CONTRADICTIONS = """
SELECT id, slug, 'a leaderboard freeze with a personal timer (duration_minutes)' AS why
  FROM contests WHERE duration_minutes IS NOT NULL AND freeze_at IS NOT NULL
UNION ALL
SELECT id, slug, 'duration_minutes is not positive' FROM contests WHERE duration_minutes <= 0
UNION ALL
SELECT id, slug, 'ends_at is not after the start (starts_at, or created_at when empty)'
  FROM contests WHERE ends_at IS NOT NULL AND ends_at <= coalesce(starts_at, created_at)
UNION ALL
SELECT c.id, c.slug, 'its join_code is used by another contest too'
  FROM contests c WHERE nullif(btrim(c.join_code), '') IS NOT NULL AND EXISTS (
    SELECT 1 FROM contests o WHERE o.id <> c.id AND btrim(o.join_code) = btrim(c.join_code))
ORDER BY 1
"""


def check_contests(bind) -> None:
    """Refuse to migrate contests the new rules cannot hold: fix them by hand first (nothing is changed)."""
    bad = bind.execute(sa.text(CONTRADICTIONS)).all()
    if bad:
        listing = "; ".join(f"id={row.id} {row.slug!r}: {row.why}" for row in bad)
        raise RuntimeError(f"{len(bad)} contest(s) contradict the rules of module 4a.2; fix them before this "
                           f"migration (nothing was changed): {listing}")


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    check_contests(bind)  # first: an exception rolls the whole migration back
    for enum_type in NEW_ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)

    # problems, problem_versions
    op.add_column('problems', sa.Column('archived', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.execute("CREATE TYPE version_status_new AS ENUM ('pending_check', 'ready', 'failed')")
    op.execute("""ALTER TABLE problem_versions ALTER COLUMN status TYPE version_status_new USING
                  (CASE status::text WHEN 'draft' THEN 'pending_check' ELSE 'ready' END)::version_status_new""")
    op.execute("DROP TYPE version_status")
    op.execute("ALTER TYPE version_status_new RENAME TO version_status")
    op.add_column('problem_versions', sa.Column('content_sha256', sa.String(length=64), nullable=True))
    op.add_column('problem_versions', sa.Column('files', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('problem_versions', sa.Column('imported_by', sa.Integer(), nullable=True))
    op.add_column('problem_versions', sa.Column('check_report', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_unique_constraint('uq_problem_versions_problem_content', 'problem_versions', ['problem_id', 'content_sha256'])
    op.create_foreign_key(op.f('fk_problem_versions_imported_by_users'), 'problem_versions', 'users', ['imported_by'], ['id'])
    op.drop_column('problem_versions', 'published_at')

    # contests: new columns nullable first, filled from the old ones, then NOT NULL
    op.add_column('contests', sa.Column('state', CONTEST_STATE_T, nullable=True))
    op.add_column('contests', sa.Column('timing_mode', TIMING_MODE_T, nullable=True))
    op.add_column('contests', sa.Column('personal_duration', sa.Interval(), nullable=True))
    op.add_column('contests', sa.Column('leaderboard', LEADERBOARD_MODE_T, nullable=True))
    op.add_column('contests', sa.Column('join_policy', JOIN_POLICY_T, nullable=True))
    op.add_column('contests', sa.Column('invite_code', sa.String(length=32), nullable=True))
    op.add_column('contests', sa.Column('allow_upsolving', sa.Boolean(), nullable=True))
    op.add_column('contests', sa.Column('languages', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('contests', sa.Column('submit_cooldown_sec', sa.Integer(), nullable=True))
    op.add_column('contests', sa.Column('max_submissions_per_problem', sa.Integer(), nullable=True))
    op.add_column('contests', sa.Column('tests_after_end', sa.Boolean(), nullable=True))
    op.add_column('contests', sa.Column('plagiarism_enabled', sa.Boolean(), nullable=True))
    op.execute("""
        UPDATE contests SET
          state = 'published',
          timing_mode = CASE WHEN duration_minutes IS NOT NULL THEN 'personal' ELSE 'fixed' END::timing_mode,
          personal_duration = CASE WHEN duration_minutes IS NOT NULL THEN make_interval(mins => duration_minutes) END,
          leaderboard = CASE WHEN leaderboard_enabled THEN 'visible' ELSE 'hidden' END::leaderboard_mode,
          invite_code = nullif(btrim(join_code), ''),
          join_policy = CASE WHEN nullif(btrim(join_code), '') IS NOT NULL THEN 'code' ELSE 'open' END::join_policy,
          allow_upsolving = true,
          submit_cooldown_sec = 15,
          tests_after_end = false,
          plagiarism_enabled = plagiarism_check,
          starts_at = coalesce(starts_at, created_at)
    """)
    for column in ('state', 'timing_mode', 'leaderboard', 'join_policy', 'allow_upsolving', 'submit_cooldown_sec',
                   'tests_after_end', 'plagiarism_enabled', 'starts_at'):
        op.alter_column('contests', column, nullable=False)
    op.create_unique_constraint(op.f('uq_contests_invite_code'), 'contests', ['invite_code'])
    for name, condition in CONTEST_CHECKS:
        op.create_check_constraint(op.f(f'ck_contests_{name}'), 'contests', condition)
    op.drop_column('contests', 'mode')
    op.drop_column('contests', 'plagiarism_check')
    op.drop_column('contests', 'join_code')
    op.drop_column('contests', 'leaderboard_enabled')
    op.drop_column('contests', 'duration_minutes')
    CONTEST_MODE_T.drop(bind, checkfirst=False)

    # contest_problems: identified by the problem; older versions of one problem in one contest become hidden
    op.add_column('contest_problems', sa.Column('problem_id', sa.Integer(), nullable=True))
    op.add_column('contest_problems', sa.Column('points', sa.Numeric(precision=8, scale=2), nullable=True))
    op.add_column('contest_problems', sa.Column('hidden', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.execute("""UPDATE contest_problems cp SET problem_id = pv.problem_id
                  FROM problem_versions pv WHERE pv.id = cp.problem_version_id""")
    op.execute("""UPDATE contest_problems cp SET hidden = true WHERE EXISTS (
                    SELECT 1 FROM contest_problems o
                    WHERE o.contest_id = cp.contest_id AND o.problem_id = cp.problem_id AND o.id > cp.id)""")
    op.alter_column('contest_problems', 'problem_id', nullable=False)
    op.create_foreign_key(op.f('fk_contest_problems_problem_id_problems'), 'contest_problems', 'problems', ['problem_id'], ['id'])
    op.drop_constraint(op.f('uq_contest_problems_contest_label'), 'contest_problems', type_='unique')
    op.create_index('uq_contest_problems_contest_label', 'contest_problems', ['contest_id', 'label'], unique=True,
                    postgresql_where=sa.text('NOT hidden'))
    op.create_index('uq_contest_problems_contest_problem', 'contest_problems', ['contest_id', 'problem_id'], unique=True,
                    postgresql_where=sa.text('NOT hidden'))
    op.create_check_constraint(op.f('ck_contest_problems_points_positive'), 'contest_problems',
                               'points IS NULL OR points > 0')

    # participations: joined_at; started_at only for the personal start
    op.add_column('participations', sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE participations SET joined_at = started_at")
    op.alter_column('participations', 'joined_at', nullable=False, server_default=sa.text('now()'))
    op.alter_column('participations', 'started_at', existing_type=postgresql.TIMESTAMP(timezone=True),
                    nullable=True, server_default=None)
    op.execute("""UPDATE participations p SET started_at = NULL FROM contests c
                  WHERE c.id = p.contest_id AND c.timing_mode = 'fixed'""")

    # submissions
    op.drop_index(op.f('ix_submissions_contest_problem'), table_name='submissions')
    op.create_index('ix_submissions_contest_problem_user_created', 'submissions',
                    ['contest_problem_id', 'user_id', 'created_at'], unique=False)

    # languages: the rows jury programs refer to (until now only dev_seed created them)
    for key, name in SYSTEM_LANGUAGES:
        bind.execute(sa.text("INSERT INTO languages (key, name, enabled, config) VALUES (:k, :n, true, '{}') "
                             "ON CONFLICT (key) DO NOTHING"), {"k": key, "n": name})


DOWNGRADE_CONTRADICTIONS = """
SELECT DISTINCT c.slug || ' ' || a.label AS what FROM contest_problems a JOIN contest_problems b
    ON b.contest_id = a.contest_id AND b.label = a.label AND b.id > a.id
  JOIN contests c ON c.id = a.contest_id
ORDER BY 1
"""


def downgrade() -> None:
    """Downgrade schema.

    Mapped back: personal -> mode timed + duration_minutes (rounded up to minutes); fixed -> timed if it has an
    end, else practice; leaderboard hidden -> false, visible/after_end -> true; join_policy code -> join_code;
    pending_check -> draft, ready -> published (published_at = created_at), failed -> archived; started_at =
    started_at or joined_at. Lost: contest state, upsolving, languages, cooldown, submission limit,
    tests_after_end, problem points and hidden marks (hidden rows become visible), archived problems, content
    digests, file lists, importers and check reports. Refused (nothing changed) if one label is used twice in a
    contest (possible with hidden rows): the old schema has a plain unique label.
    """
    bind = op.get_bind()
    dupes = bind.execute(sa.text(DOWNGRADE_CONTRADICTIONS)).scalars().all()
    if dupes:
        raise RuntimeError("these contest labels are used twice (hidden and visible rows); relabel them before "
                           f"downgrading (nothing was changed): {', '.join(dupes)}")

    op.drop_index('ix_submissions_contest_problem_user_created', table_name='submissions')
    op.create_index(op.f('ix_submissions_contest_problem'), 'submissions', ['contest_problem_id'], unique=False)

    op.execute("UPDATE participations SET started_at = coalesce(started_at, joined_at)")
    op.alter_column('participations', 'started_at', existing_type=postgresql.TIMESTAMP(timezone=True),
                    nullable=False, server_default=sa.text('now()'))
    op.drop_column('participations', 'joined_at')

    op.drop_constraint(op.f('ck_contest_problems_points_positive'), 'contest_problems', type_='check')
    op.drop_index('uq_contest_problems_contest_problem', table_name='contest_problems', postgresql_where=sa.text('NOT hidden'))
    op.drop_index('uq_contest_problems_contest_label', table_name='contest_problems', postgresql_where=sa.text('NOT hidden'))
    op.create_unique_constraint(op.f('uq_contest_problems_contest_label'), 'contest_problems', ['contest_id', 'label'])
    op.drop_constraint(op.f('fk_contest_problems_problem_id_problems'), 'contest_problems', type_='foreignkey')
    op.drop_column('contest_problems', 'hidden')
    op.drop_column('contest_problems', 'points')
    op.drop_column('contest_problems', 'problem_id')

    CONTEST_MODE_T.create(bind, checkfirst=False)
    op.add_column('contests', sa.Column('duration_minutes', sa.INTEGER(), nullable=True))
    op.add_column('contests', sa.Column('leaderboard_enabled', sa.BOOLEAN(), nullable=True))
    op.add_column('contests', sa.Column('join_code', sa.VARCHAR(length=32), nullable=True))
    op.add_column('contests', sa.Column('plagiarism_check', sa.BOOLEAN(), nullable=True))
    op.add_column('contests', sa.Column('mode', CONTEST_MODE_T, nullable=True))
    op.execute("""
        UPDATE contests SET
          mode = CASE WHEN timing_mode = 'personal' OR ends_at IS NOT NULL THEN 'timed' ELSE 'practice' END::contest_mode,
          duration_minutes = CASE WHEN timing_mode = 'personal'
                                  THEN ceil(extract(epoch FROM personal_duration) / 60)::int END,
          leaderboard_enabled = leaderboard <> 'hidden',
          join_code = CASE WHEN join_policy = 'code' THEN invite_code END,
          plagiarism_check = plagiarism_enabled
    """)
    for column in ('mode', 'leaderboard_enabled', 'plagiarism_check'):
        op.alter_column('contests', column, nullable=False)
    for name, _condition in reversed(CONTEST_CHECKS):
        op.drop_constraint(op.f(f'ck_contests_{name}'), 'contests', type_='check')
    op.drop_constraint(op.f('uq_contests_invite_code'), 'contests', type_='unique')
    op.alter_column('contests', 'starts_at', existing_type=postgresql.TIMESTAMP(timezone=True), nullable=True)
    for column in ('plagiarism_enabled', 'tests_after_end', 'max_submissions_per_problem', 'submit_cooldown_sec',
                   'languages', 'allow_upsolving', 'invite_code', 'join_policy', 'leaderboard', 'personal_duration',
                   'timing_mode', 'state'):
        op.drop_column('contests', column)

    op.add_column('problem_versions', sa.Column('published_at', postgresql.TIMESTAMP(timezone=True), nullable=True))
    op.execute("CREATE TYPE version_status_old AS ENUM ('draft', 'published', 'archived')")
    op.execute("""ALTER TABLE problem_versions ALTER COLUMN status TYPE version_status_old USING
                  (CASE status::text WHEN 'pending_check' THEN 'draft' WHEN 'ready' THEN 'published'
                   ELSE 'archived' END)::version_status_old""")
    op.execute("DROP TYPE version_status")
    op.execute("ALTER TYPE version_status_old RENAME TO version_status")
    op.execute("UPDATE problem_versions SET published_at = created_at WHERE status = 'published'")
    op.drop_constraint(op.f('fk_problem_versions_imported_by_users'), 'problem_versions', type_='foreignkey')
    op.drop_constraint('uq_problem_versions_problem_content', 'problem_versions', type_='unique')
    for column in ('check_report', 'imported_by', 'files', 'content_sha256'):
        op.drop_column('problem_versions', column)
    op.drop_column('problems', 'archived')

    for enum_type in reversed(NEW_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=False)
