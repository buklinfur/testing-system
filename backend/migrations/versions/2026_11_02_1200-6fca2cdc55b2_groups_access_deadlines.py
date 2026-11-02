"""groups, access modes, deadlines, extensions, time zone (module 4a.3a)

Revision ID: 6fca2cdc55b2
Revises: 4d8a1c2e9f57
Create Date: 2026-11-02 12:00:00

Written by hand (docs/PLAN_4A_DELTA_4V.md, section 3.2; D-020..D-024). Data is carried over:
* contests.join_policy -> access_mode (open -> open, code -> code); the CHECK on the invite code now covers both
  modes with a code;
* participations.joined_via = code in contests with a code, else open (until now joining was the only way in);
* contest problems get no deadlines (after-due policies: statement visible, submissions late), submissions are not
  late, users have no time zone; groups, contest groups and extensions start empty.
The old rules cannot produce a contest with a code but no invite code (a CHECK forbids it); the upgrade still
refuses such rows, before changing anything, with a list.
The downgrade refuses (nothing changed) while there is anything the old schema cannot hold: see downgrade().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '6fca2cdc55b2'
down_revision: Union[str, Sequence[str], None] = '4d8a1c2e9f57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCESS_MODE_T = postgresql.ENUM('open', 'code', 'groups', 'code_or_groups', name='access_mode', create_type=False)
JOINED_VIA_T = postgresql.ENUM('code', 'group', 'open', name='joined_via', create_type=False)
AFTER_DUE_STATEMENT_T = postgresql.ENUM('visible', 'hidden', name='after_due_statement', create_type=False)
AFTER_DUE_SUBMIT_T = postgresql.ENUM('no', 'late', 'yes', name='after_due_submit', create_type=False)
NEW_ENUM_TYPES = (ACCESS_MODE_T, JOINED_VIA_T, AFTER_DUE_STATEMENT_T, AFTER_DUE_SUBMIT_T)
JOIN_POLICY_T = postgresql.ENUM('open', 'code', name='join_policy', create_type=False)

OLD_INVITE_CHECK = "join_policy <> 'code' OR invite_code IS NOT NULL"
NEW_INVITE_CHECK = "access_mode NOT IN ('code', 'code_or_groups') OR invite_code IS NOT NULL"

CONTRADICTIONS = """
SELECT id, slug FROM contests WHERE join_policy = 'code' AND invite_code IS NULL ORDER BY id
"""

DOWNGRADE_CONTRADICTIONS = """
SELECT 'contest ' || slug || ': access mode ' || access_mode::text AS what
  FROM contests WHERE access_mode NOT IN ('open', 'code')
UNION ALL
SELECT 'groups: ' || count(*) FROM groups HAVING count(*) > 0
UNION ALL
SELECT 'deadline extensions: ' || count(*) FROM deadline_extensions HAVING count(*) > 0
UNION ALL
SELECT 'contest ' || c.slug || ' ' || cp.label || ': deadline, after-due policy or note'
  FROM contest_problems cp JOIN contests c ON c.id = cp.contest_id
  WHERE cp.available_from IS NOT NULL OR cp.due_at IS NOT NULL OR cp.note_md IS NOT NULL
     OR cp.after_due_statement <> 'visible' OR cp.after_due_submit <> 'late'
UNION ALL
SELECT 'late submissions: ' || count(*) FROM submissions WHERE late HAVING count(*) > 0
UNION ALL
SELECT 'users with a time zone: ' || count(*) FROM users WHERE timezone IS NOT NULL HAVING count(*) > 0
UNION ALL
SELECT 'contest ' || c.slug || ': ' || count(*) || ' participation(s) that give no access now (joined via '
       || string_agg(DISTINCT p.joined_via::text, ', ') || '); the old schema would let them in again'
  FROM participations p JOIN contests c ON c.id = p.contest_id
  WHERE c.access_mode = 'code' AND p.joined_via <> 'code'
  GROUP BY c.slug
ORDER BY 1
"""


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    bad = bind.execute(sa.text(CONTRADICTIONS)).all()
    if bad:  # first: an exception rolls the whole migration back
        listing = "; ".join(f"id={row.id} {row.slug!r}" for row in bad)
        raise RuntimeError(f"{len(bad)} contest(s) need an invite code but have none; give them one before this "
                           f"migration (nothing was changed): {listing}")
    for enum_type in NEW_ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)

    # contests: join_policy -> access_mode; participations remember how they came in
    op.add_column('contests', sa.Column('access_mode', ACCESS_MODE_T, nullable=True))
    op.execute("UPDATE contests SET access_mode = join_policy::text::access_mode")
    op.alter_column('contests', 'access_mode', nullable=False)
    op.add_column('participations', sa.Column('joined_via', JOINED_VIA_T, nullable=True))
    op.execute("""UPDATE participations p SET joined_via = CASE WHEN c.join_policy = 'code' THEN 'code' ELSE 'open'
                  END::joined_via FROM contests c WHERE c.id = p.contest_id""")
    op.alter_column('participations', 'joined_via', nullable=False)
    op.drop_constraint(op.f('ck_contests_invite_code'), 'contests', type_='check')
    op.create_check_constraint(op.f('ck_contests_invite_code'), 'contests', NEW_INVITE_CHECK)
    op.drop_column('contests', 'join_policy')
    JOIN_POLICY_T.drop(bind, checkfirst=False)

    # deadlines of contest problems
    op.add_column('contest_problems', sa.Column('available_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contest_problems', sa.Column('due_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contest_problems', sa.Column('after_due_statement', AFTER_DUE_STATEMENT_T,
                                                server_default='visible', nullable=False))
    op.add_column('contest_problems', sa.Column('after_due_submit', AFTER_DUE_SUBMIT_T,
                                                server_default='late', nullable=False))
    op.add_column('contest_problems', sa.Column('note_md', sa.Text(), nullable=True))
    op.create_check_constraint(op.f('ck_contest_problems_deadline'), 'contest_problems',
                               'available_from IS NULL OR due_at IS NULL OR available_from < due_at')

    op.add_column('users', sa.Column('timezone', sa.String(length=64), nullable=True))
    op.add_column('submissions', sa.Column('late', sa.Boolean(), server_default=sa.text('false'), nullable=False))

    # groups
    op.create_table(
        'groups',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f('ck_groups_name_not_blank')),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_groups_created_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_groups')),
    )
    op.create_index('uq_groups_live_name', 'groups', [sa.text('lower(name)')], unique=True,
                    postgresql_where=sa.text('archived_at IS NULL'))
    op.create_table(
        'group_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('added_by', sa.Integer(), nullable=True),
        sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('removed_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name=op.f('fk_group_members_group_id_groups')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_group_members_user_id_users')),
        sa.ForeignKeyConstraint(['added_by'], ['users.id'], name=op.f('fk_group_members_added_by_users')),
        sa.ForeignKeyConstraint(['removed_by'], ['users.id'], name=op.f('fk_group_members_removed_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_group_members')),
    )
    op.create_index('uq_group_members_live', 'group_members', ['group_id', 'user_id'], unique=True,
                    postgresql_where=sa.text('removed_at IS NULL'))
    op.create_index('ix_group_members_user_live', 'group_members', ['user_id'],
                    postgresql_where=sa.text('removed_at IS NULL'))
    op.create_table(
        'contest_groups',
        sa.Column('contest_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('added_by', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['contest_id'], ['contests.id'], name=op.f('fk_contest_groups_contest_id_contests'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name=op.f('fk_contest_groups_group_id_groups')),
        sa.ForeignKeyConstraint(['added_by'], ['users.id'], name=op.f('fk_contest_groups_added_by_users')),
        sa.PrimaryKeyConstraint('contest_id', 'group_id', name=op.f('pk_contest_groups')),
    )
    op.create_index('ix_contest_groups_group', 'contest_groups', ['group_id'])
    op.create_table(
        'deadline_extensions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('contest_problem_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('due_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reason', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.CheckConstraint('(user_id IS NULL) <> (group_id IS NULL)', name=op.f('ck_deadline_extensions_one_subject')),
        sa.ForeignKeyConstraint(['contest_problem_id'], ['contest_problems.id'],
                                name=op.f('fk_deadline_extensions_contest_problem_id_contest_problems'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_deadline_extensions_user_id_users')),
        sa.ForeignKeyConstraint(['group_id'], ['groups.id'], name=op.f('fk_deadline_extensions_group_id_groups')),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_deadline_extensions_created_by_users')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_deadline_extensions')),
    )
    op.create_index('uq_deadline_extensions_user', 'deadline_extensions', ['contest_problem_id', 'user_id'],
                    unique=True, postgresql_where=sa.text('user_id IS NOT NULL'))
    op.create_index('uq_deadline_extensions_group', 'deadline_extensions', ['contest_problem_id', 'group_id'],
                    unique=True, postgresql_where=sa.text('group_id IS NOT NULL'))


def downgrade() -> None:
    """Downgrade schema.

    Refused (nothing changed) while the database holds what the old schema cannot: a contest in a group mode,
    any group or extension, a deadline, after-due policy or note on a contest problem, a late submission, a user's
    time zone, or a participation that gives no access now (joined the open way into a contest that has a code
    since): with join_policy every participant is let in. Otherwise access_mode maps back to join_policy.
    """
    bind = op.get_bind()
    bad = bind.execute(sa.text(DOWNGRADE_CONTRADICTIONS)).scalars().all()
    if bad:
        raise RuntimeError("the old schema cannot hold these; remove them before downgrading (nothing was "
                           f"changed): {'; '.join(bad)}")

    op.drop_index('uq_deadline_extensions_group', table_name='deadline_extensions',
                  postgresql_where=sa.text('group_id IS NOT NULL'))
    op.drop_index('uq_deadline_extensions_user', table_name='deadline_extensions',
                  postgresql_where=sa.text('user_id IS NOT NULL'))
    op.drop_table('deadline_extensions')
    op.drop_index('ix_contest_groups_group', table_name='contest_groups')
    op.drop_table('contest_groups')
    op.drop_index('ix_group_members_user_live', table_name='group_members',
                  postgresql_where=sa.text('removed_at IS NULL'))
    op.drop_index('uq_group_members_live', table_name='group_members', postgresql_where=sa.text('removed_at IS NULL'))
    op.drop_table('group_members')
    op.drop_index('uq_groups_live_name', table_name='groups', postgresql_where=sa.text('archived_at IS NULL'))
    op.drop_table('groups')

    op.drop_column('submissions', 'late')
    op.drop_column('users', 'timezone')
    op.drop_constraint(op.f('ck_contest_problems_deadline'), 'contest_problems', type_='check')
    for column in ('note_md', 'after_due_submit', 'after_due_statement', 'due_at', 'available_from'):
        op.drop_column('contest_problems', column)

    JOIN_POLICY_T.create(bind, checkfirst=False)
    op.add_column('contests', sa.Column('join_policy', JOIN_POLICY_T, nullable=True))
    op.execute("UPDATE contests SET join_policy = access_mode::text::join_policy")
    op.alter_column('contests', 'join_policy', nullable=False)
    op.drop_constraint(op.f('ck_contests_invite_code'), 'contests', type_='check')
    op.create_check_constraint(op.f('ck_contests_invite_code'), 'contests', OLD_INVITE_CHECK)
    op.drop_column('contests', 'access_mode')
    op.drop_column('participations', 'joined_via')

    for enum_type in reversed(NEW_ENUM_TYPES):
        enum_type.drop(bind, checkfirst=False)
