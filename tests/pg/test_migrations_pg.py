"""Alembic migrations == models.py, on a real PostgreSQL (skipped unless JUDGE_TEST_DATABASE_URL is set).

Fails when someone changes models.py without a migration, or edits a migration by hand so that it
no longer builds the same schema. compare_metadata does not look at the WHERE of partial indexes,
so index definitions are also compared with what create_all builds (PgCase's schema).
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
        return sorted(re.sub(rf"\b{schema}\.", "", d) for d in indexes), sorted(types)

    def test_upgrade_matches_models_and_downgrade_removes_everything(self):
        schema = "m_" + uuid.uuid4().hex[:10]
        admin = create_engine(URL)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
        try:
            with engine.begin() as conn:
                self.alembic(conn, "upgrade", "head")
                diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
                self.assertEqual(diff, [], "models.py and the migrations differ: generate a migration")
                migrated = self.schema_objects(conn, schema)
            with self.engine.connect() as conn:
                built = self.schema_objects(conn, self.schema)
            self.assertEqual(migrated[0], built[0], "index definitions (incl. partial WHERE) differ from create_all")
            self.assertEqual(migrated[1], built[1], "enum types differ from create_all")
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "base")
                self.assertEqual(self.schema_objects(conn, schema), ([], []), "downgrade leaves indexes or types")
                left = conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = :s"), {"s": schema}).scalars().all()
                self.assertEqual(left, ["alembic_version"])
                self.alembic(conn, "upgrade", "head")  # a second upgrade after downgrade works (enum types were dropped)
        finally:
            engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()
