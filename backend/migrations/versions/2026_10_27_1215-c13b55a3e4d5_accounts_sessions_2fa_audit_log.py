"""accounts, sessions, 2fa, audit log (module 4a.1)

Revision ID: c13b55a3e4d5
Revises: 4499f58e60ca
Create Date: 2026-10-27 12:15:32.972165

Autogenerate was adjusted by hand:
* role 'teacher' is renamed to 'admin' in place (existing rows keep their role);
* logins are lowered and trimmed before the CHECK of the login format is added;
* audit_log columns are renamed, not dropped and re-added, so existing rows survive;
* the new enum types are created and dropped explicitly, as in the initial migration.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c13b55a3e4d5'
down_revision: Union[str, Sequence[str], None] = '4499f58e60ca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SESSION_STATE_T = postgresql.ENUM('mfa_pending', 'active', name='session_state', create_type=False)
AUTH_FAILURE_KIND_T = postgresql.ENUM('password', 'totp', 'qr', name='auth_failure_kind', create_type=False)
ENUM_TYPES = (SESSION_STATE_T, AUTH_FAILURE_KIND_T)
LOGIN_CHECK = "login ~ '^[a-z0-9][a-z0-9._-]{2,31}$'"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for enum_type in ENUM_TYPES:
        enum_type.create(bind, checkfirst=False)
    op.execute("ALTER TYPE role RENAME VALUE 'teacher' TO 'admin'")

    # users
    op.add_column('users', sa.Column('password_changed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('last_login_ip', sa.String(length=45), nullable=True))
    op.add_column('users', sa.Column('totp_secret_enc', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('totp_enabled_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('totp_last_step', sa.BigInteger(), nullable=True))
    # the old unique index on lower(login) guarantees that lowering creates no duplicates
    op.execute("UPDATE users SET login = lower(btrim(login)) WHERE login <> lower(btrim(login))")
    op.drop_index('uq_users_login_lower', table_name='users')
    op.alter_column('users', 'login', existing_type=sa.VARCHAR(length=64), type_=sa.String(length=32),
                    existing_nullable=False)
    op.create_unique_constraint(op.f('uq_users_login'), 'users', ['login'])
    op.create_check_constraint(op.f('ck_users_login_format'), 'users', LOGIN_CHECK)

    # audit_log: rename to the module 4a names, then add the new columns
    op.alter_column('audit_log', 'user_id', new_column_name='actor_user_id')
    op.alter_column('audit_log', 'created_at', new_column_name='at')
    op.alter_column('audit_log', 'ip_address', new_column_name='ip')
    op.execute('ALTER TABLE audit_log RENAME CONSTRAINT fk_audit_log_user_id_users TO fk_audit_log_actor_user_id_users')
    op.drop_index('ix_audit_log_user_created', table_name='audit_log')
    op.add_column('audit_log', sa.Column('actor_cli', sa.String(length=64), nullable=True))
    op.add_column('audit_log', sa.Column('object_type', sa.String(length=32), nullable=True))
    op.add_column('audit_log', sa.Column('object_id', sa.String(length=64), nullable=True))
    op.create_index('ix_audit_log_actor_at', 'audit_log', ['actor_user_id', 'at'], unique=False)
    op.create_index('ix_audit_log_at', 'audit_log', ['at'], unique=False)
    op.create_index('ix_audit_log_object', 'audit_log', ['object_type', 'object_id', 'at'], unique=False)

    # new tables
    op.create_table('auth_failures',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('login', sa.String(length=64), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=False),
    sa.Column('kind', AUTH_FAILURE_KIND_T, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_failures'))
    )
    op.create_index('ix_auth_failures_ip_at', 'auth_failures', ['ip', 'at'], unique=False)
    op.create_index('ix_auth_failures_login_at', 'auth_failures', ['login', 'at'], unique=False)
    op.create_table('auth_lockouts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('login', sa.String(length=64), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('until', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cleared_by', sa.String(length=64), nullable=True),
    sa.CheckConstraint('(login IS NULL) <> (ip IS NULL)', name=op.f('ck_auth_lockouts_one_subject')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_auth_lockouts'))
    )
    op.create_index('ix_auth_lockouts_ip_until', 'auth_lockouts', ['ip', 'until'], unique=False)
    op.create_index('ix_auth_lockouts_login_until', 'auth_lockouts', ['login', 'until'], unique=False)
    op.create_table('login_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_login_tokens_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_login_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_login_tokens_token_hash'))
    )
    op.create_index('ix_login_tokens_user_live', 'login_tokens', ['user_id'], unique=False, postgresql_where=sa.text('used_at IS NULL AND revoked_at IS NULL'))
    op.create_table('sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('state', SESSION_STATE_T, nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('absolute_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.String(length=256), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_sessions_token_hash'))
    )
    op.create_index('ix_sessions_user_live', 'sessions', ['user_id'], unique=False, postgresql_where=sa.text('revoked_at IS NULL'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_sessions_user_live', table_name='sessions', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_table('sessions')
    op.drop_index('ix_login_tokens_user_live', table_name='login_tokens', postgresql_where=sa.text('used_at IS NULL AND revoked_at IS NULL'))
    op.drop_table('login_tokens')
    op.drop_index('ix_auth_lockouts_login_until', table_name='auth_lockouts')
    op.drop_index('ix_auth_lockouts_ip_until', table_name='auth_lockouts')
    op.drop_table('auth_lockouts')
    op.drop_index('ix_auth_failures_login_at', table_name='auth_failures')
    op.drop_index('ix_auth_failures_ip_at', table_name='auth_failures')
    op.drop_table('auth_failures')

    op.drop_index('ix_audit_log_object', table_name='audit_log')
    op.drop_index('ix_audit_log_at', table_name='audit_log')
    op.drop_index('ix_audit_log_actor_at', table_name='audit_log')
    op.drop_column('audit_log', 'object_id')
    op.drop_column('audit_log', 'object_type')
    op.drop_column('audit_log', 'actor_cli')
    op.execute('ALTER TABLE audit_log RENAME CONSTRAINT fk_audit_log_actor_user_id_users TO fk_audit_log_user_id_users')
    op.alter_column('audit_log', 'ip', new_column_name='ip_address')
    op.alter_column('audit_log', 'at', new_column_name='created_at')
    op.alter_column('audit_log', 'actor_user_id', new_column_name='user_id')
    op.create_index('ix_audit_log_user_created', 'audit_log', ['user_id', 'created_at'], unique=False)

    op.drop_constraint(op.f('ck_users_login_format'), 'users', type_='check')
    op.drop_constraint(op.f('uq_users_login'), 'users', type_='unique')
    op.alter_column('users', 'login', existing_type=sa.String(length=32), type_=sa.VARCHAR(length=64),
                    existing_nullable=False)
    op.create_index('uq_users_login_lower', 'users', [sa.literal_column('lower(login)')], unique=True)
    op.drop_column('users', 'totp_last_step')
    op.drop_column('users', 'totp_enabled_at')
    op.drop_column('users', 'totp_secret_enc')
    op.drop_column('users', 'last_login_ip')
    op.drop_column('users', 'last_login_at')
    op.drop_column('users', 'password_changed_at')

    op.execute("ALTER TYPE role RENAME VALUE 'admin' TO 'teacher'")
    bind = op.get_bind()
    for enum_type in reversed(ENUM_TYPES):
        enum_type.drop(bind, checkfirst=False)
