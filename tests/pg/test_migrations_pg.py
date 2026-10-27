"""Alembic migrations == models.py, on a real PostgreSQL (skipped unless JUDGE_TEST_DATABASE_URL is set).

Fails when someone changes models.py without a migration, or edits a migration by hand so that it
no longer builds the same schema. compare_metadata does not look at the WHERE of partial indexes
or at CHECK constraints, so index, enum and constraint definitions are also compared with what
create_all builds (PgCase's schema). Hand-written data steps of migrations have their own tests.
"""
import re
import uuid

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from alembic import command
    from alembic.autogenerate import compare_metadata
    from alembic.config import Config
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine, text

    from app.models import Base


@requires_pg
class MigrationTests(PgCase):
    def setUp(self):  # no seed data needed
        pass

    def alembic(self, conn, *args):
        cfg = Config()  # no ini file: keeps alembic's logging config out of the test output
        cfg.set_main_option("script_location", str(ROOT / "backend/migrations"))
        cfg.attributes["connection"] = conn
        getattr(command, args[0])(cfg, *args[1:])

    def schema_objects(self, conn, schema):
        indexes = conn.execute(text("SELECT indexdef FROM pg_indexes WHERE schemaname = :s AND tablename <> 'alembic_version'"),
                               {"s": schema}).scalars().all()
        types = conn.execute(text("""SELECT t.typname || ':' || string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder)
                                     FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                                     JOIN pg_enum e ON e.enumtypid = t.oid WHERE n.nspname = :s GROUP BY t.typname"""),
                             {"s": schema}).scalars().all()
        constraints = conn.execute(text("""SELECT c.conrelid::regclass::text || ' ' || c.conname || ' ' || pg_get_constraintdef(c.oid)
                                           FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
                                           WHERE n.nspname = :s AND c.conrelid <> 0
                                             AND c.conrelid::regclass::text NOT LIKE '%alembic_version'"""),
                                   {"s": schema}).scalars().all()
        strip = lambda rows: sorted(re.sub(rf"\b{schema}\.", "", d) for d in rows)  # noqa: E731
        return strip(indexes), sorted(types), strip(constraints)

    def scratch_schema(self):
        """A fresh empty schema and an engine bound to it; dropped at cleanup."""
        schema = "m_" + uuid.uuid4().hex[:10]
        admin = create_engine(URL)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})

        def drop():
            engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
        self.addCleanup(drop)
        return schema, engine

    def test_upgrade_matches_models_and_downgrade_removes_everything(self):
        schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "head")
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "models.py and the migrations differ: generate a migration")
            migrated = self.schema_objects(conn, schema)
        with self.engine.connect() as conn:
            built = self.schema_objects(conn, self.schema)
        self.assertEqual(migrated[0], built[0], "index definitions (incl. partial WHERE) differ from create_all")
        self.assertEqual(migrated[1], built[1], "enum types differ from create_all")
        self.assertEqual(migrated[2], built[2], "constraints (incl. CHECK) differ from create_all")
        with engine.begin() as conn:
            self.alembic(conn, "downgrade", "base")
            self.assertEqual(self.schema_objects(conn, schema), ([], [], []), "downgrade leaves indexes, types or constraints")
            left = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}).scalars().all()
            self.assertEqual(left, ["alembic_version"])
            self.alembic(conn, "upgrade", "head")  # a second upgrade after downgrade works (enum types were dropped)

    def test_accounts_migration_keeps_existing_rows(self):
        """c13b55a3e4d5: role teacher -> admin, logins lowered, audit_log columns renamed; and back."""
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "4499f58e60ca")
            conn.execute(text("""INSERT INTO users (login, password_hash, display_name, role, is_active)
                                 VALUES (' Dev-Teacher', '!', 'T', 'teacher', true), ('pupil', '!', 'P', 'student', true)"""))
            conn.execute(text("""INSERT INTO audit_log (user_id, action, ip_address, details)
                                 SELECT id, 'rejudge', '10.0.0.1', '{"n": 1}' FROM users WHERE role = 'teacher'"""))
            self.alembic(conn, "upgrade", "c13b55a3e4d5")
            users = conn.execute(text("SELECT login, role::text FROM users ORDER BY id")).all()
            self.assertEqual([tuple(u) for u in users], [("dev-teacher", "admin"), ("pupil", "student")])
            row = conn.execute(text("""SELECT u.login, a.action, a.ip, a.details, a.at IS NOT NULL
                                       FROM audit_log a JOIN users u ON u.id = a.actor_user_id""")).one()
            self.assertEqual(tuple(row), ("dev-teacher", "rejudge", "10.0.0.1", {"n": 1}, True))
            with self.assertRaises(Exception):  # the login format is now enforced
                with conn.begin_nested():
                    conn.execute(text("UPDATE users SET login = 'Pupil' WHERE login = 'pupil'"))
            self.alembic(conn, "downgrade", "4499f58e60ca")
            users = conn.execute(text("SELECT login, role::text FROM users ORDER BY id")).all()
            self.assertEqual([tuple(u) for u in users], [("dev-teacher", "teacher"), ("pupil", "student")])
            row = conn.execute(text("SELECT a.action, a.ip_address FROM audit_log a JOIN users u ON u.id = a.user_id")).one()
            self.assertEqual(tuple(row), ("rejudge", "10.0.0.1"))

    def test_accounts_migration_refuses_bad_logins(self):
        """c13b55a3e4d5 stops before changing anything if a login cannot be made valid by trim + lower."""
        _schema, engine = self.scratch_schema()
        long_login = "a" * 33
        logins = [" Fine.Login ", "ab", "иванов", long_login, "-dash"]
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "4499f58e60ca")
            for login in logins:
                conn.execute(text("INSERT INTO users (login, password_hash, display_name, role, is_active) "
                                  "VALUES (:l, '!', 'X', 'student', true)"), {"l": login})
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "upgrade", "c13b55a3e4d5")
        message = str(caught.exception)
        self.assertIn("4 login(s)", message)
        for login in ("'ab'", "'иванов'", repr(long_login), "'-dash'"):
            self.assertIn(login, message)
        self.assertNotIn("Fine.Login", message)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "4499f58e60ca")
            self.assertEqual(conn.execute(text("SELECT login FROM users ORDER BY id")).scalars().all(), logins)
            role_labels = conn.execute(text("SELECT enum_range(NULL::role)::text")).scalar_one()
            self.assertEqual(role_labels, "{teacher,student}")
