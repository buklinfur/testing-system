"""tools/reset_check.py, the data and catalog half of deploy/dev/check-reset-db.sh (D-073), in scratch schemas:
its seed holds everything that refuses `alembic downgrade base` (the reason reset-production.sh recreates the
database instead), and the snapshot comparison sees left rows, a different owner and a migration gap. The other
half - dropping and creating a real database as postgres - needs root and runs only in the check script.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import importlib.util

import unittest

from tests.pg.pgbase import HAVE_SA, ROOT, URL, requires_pg, schema_name

if URL and HAVE_SA:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

spec = importlib.util.spec_from_file_location("reset_check", ROOT / "tools" / "reset_check.py")
reset_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_check)


@requires_pg
class ResetCheckTests(unittest.TestCase):
    def alembic(self, conn, *args):
        cfg = Config()
        cfg.set_main_option("script_location", str(ROOT / "backend/migrations"))
        cfg.attributes["connection"] = conn
        getattr(command, args[0])(cfg, *args[1:])

    def migrated(self):
        """A fresh schema at the head of the migrations, as a fresh installation has; dropped at cleanup."""
        schema = schema_name("m")
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
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "head")
        return schema, engine

    def test_reset_check_seed_is_what_downgrade_base_refuses(self):
        schema, engine = self.migrated()
        with engine.begin() as conn:
            reset_check.seed(conn)
            rows = reset_check.snapshot(conn, schema)["rows"]
        for table in ("users", "sessions", "audit_log", "worker_tokens", "groups", "contest_problems",
                      "deadline_extensions", "version_checks", "feed_items", "assignment_works", "comment_threads",
                      "comments", "thread_reads", "attachments", "assignment_submission_links"):
            self.assertIn(table, rows)
        self.assertEqual(rows["attachments"], 2, "a file and a link of a teacher")
        with engine.connect() as conn:
            kinds = conn.execute(text("SELECT kind::text, count(*) FROM assignment_submission_files GROUP BY kind"))
            self.assertEqual(dict(kinds.all()), {"text": 1, "file": 1})
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "base")
        self.assertIn("nothing was changed", str(caught.exception))
        self.assertIn("uploaded files of hand-ins: 1, links of hand-ins: 1, attachments: 2, items answered with "
                      "files: 1", str(caught.exception), "the first downgrade on the way down is the one of 4v.3")

    def test_reset_check_snapshot_tells_a_reset_from_a_fresh_one(self):
        schema, engine = self.migrated()
        other, fresh_engine = self.migrated()
        with engine.connect() as conn:
            reset = reset_check.snapshot(conn, schema)
        with fresh_engine.connect() as conn:
            fresh = reset_check.snapshot(conn, other)
        self.assertEqual(reset_check.differences(reset, fresh), [])
        self.assertEqual(reset["migrations_vs_models"], [])
        self.assertEqual(set(reset["tables"].values()), {reset["schema"]["owner"]})

        with engine.begin() as conn:
            reset_check.seed(conn)
            conn.execute(text("DROP TABLE thread_reads"))
            dirty = reset_check.snapshot(conn, schema)
        found = reset_check.differences(dirty, fresh)
        self.assertTrue(any(line.startswith("tables:") for line in found), found)
        self.assertTrue(any(line.startswith("rows:") for line in found), found)
        self.assertTrue(any("models.py and the migrated schema differ" in line for line in found), found)
        self.assertEqual(reset_check.locale_problems(reset), [], "judge_test: UTF8, C.UTF-8, lower('Ё') = 'ё'")
        self.assertEqual({k: reset["database"][k] for k in ("encoding", "collate", "ctype", "lower_yo")},
                         {"encoding": "UTF8", "collate": "C.UTF-8", "ctype": "C.UTF-8", "lower_yo": "ё"})
        c_locale = dict(fresh, database=dict(fresh["database"], collate="C", ctype="C", lower_yo="Ё"))
        self.assertEqual(reset_check.locale_problems(c_locale),
                         ["collate is 'C', not 'C.UTF-8'", "ctype is 'C', not 'C.UTF-8'",
                          "lower('Ё') is 'Ё', not 'ё'"])
        self.assertTrue(any(line.startswith("fresh: locale:") for line in reset_check.differences(fresh, c_locale)))
        self.assertEqual(reset_check.differences(fresh, dict(fresh, schema={"owner": "x", "acl": None})),
                         [f"schema: reset {fresh['schema']!r}, fresh {{'owner': 'x', 'acl': None}}"])
