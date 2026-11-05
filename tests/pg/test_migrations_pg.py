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

    # ---------------------------------------------------------------- 6bc3b8bd1c68: problems and contests (4a.2)
    LEGACY = """
        INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
          (1, 'dev-teacher', '!', 'Dev Teacher', 'admin', true), (2, 'pupil', '!', 'P', 'student', true);
        INSERT INTO problems (id, slug, owner_id) VALUES (1, 'sum', 1), (2, 'pair', 1);
        INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config, published_at)
        VALUES (1, 1, 1, 'published', 'batch', '{}', '{}', '{}', now()),
               (2, 1, 2, 'draft', 'batch', '{}', '{}', '{}', NULL),
               (3, 2, 1, 'archived', 'batch', '{}', '{}', '{}', NULL);
        INSERT INTO contests (id, slug, title, mode, starts_at, ends_at, duration_minutes, leaderboard_enabled,
                              freeze_at, join_code, plagiarism_check, created_by, created_at, description_md, scoring_mode, icpc_penalty_minutes) VALUES
          (1, 'dev', 'practice', 'practice', NULL, NULL, NULL, false, NULL, NULL, false, 1, '2026-10-01 10:00+00', '', 'points', 20),
          (2, 'olymp', 'personal', 'timed', '2026-11-10 09:00+00', '2026-11-10 18:00+00', 90, true, NULL, 'ABC123',
           true, 1, '2026-10-01 10:00+00', '', 'points', 20),
          (3, 'frozen', 'fixed', 'timed', '2026-11-11 09:00+00', '2026-11-11 12:00+00', NULL, true,
           '2026-11-11 11:00+00', NULL, false, 1, '2026-10-01 10:00+00', '', 'points', 20),
          (4, 'blank', 'blank code', 'practice', '2026-11-01 09:00+00', NULL, NULL, false, NULL, '  ', false, 1,
           '2026-10-01 10:00+00', '', 'points', 20);
        INSERT INTO contest_problems (id, contest_id, problem_version_id, label, position) VALUES
          (1, 1, 1, 'P1', 0), (2, 1, 2, 'P2', 1), (3, 1, 3, 'P3', 2), (4, 2, 1, 'A', 0);
        INSERT INTO participations (contest_id, user_id, started_at) VALUES
          (1, 2, '2026-10-05 08:00+00'), (2, 2, '2026-11-10 09:30+00');
        INSERT INTO languages (key, name, enabled, config) VALUES ('python', 'Python (dev)', true, '{"x": 1}');
    """

    def contests(self, conn, columns):
        return {r[0]: tuple(r[1:]) for r in conn.execute(text(f"SELECT slug, {columns} FROM contests ORDER BY id"))}

    def test_problems_contests_migration_maps_every_case_up_and_down(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "c13b55a3e4d5")
            for statement in self.LEGACY.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
            self.alembic(conn, "upgrade", "6bc3b8bd1c68")

            # version statuses: published -> ready, draft -> pending_check, archived -> ready
            rows = conn.execute(text("SELECT id, status::text FROM problem_versions ORDER BY id")).all()
            self.assertEqual([tuple(r) for r in rows], [(1, "ready"), (2, "pending_check"), (3, "ready")])
            self.assertEqual(conn.execute(text("SELECT count(*) FROM problems WHERE archived")).scalar_one(), 0)
            got = self.contests(conn, "state::text, timing_mode::text, extract(epoch FROM personal_duration)::int, "
                                      "leaderboard::text, join_policy::text, invite_code, plagiarism_enabled, "
                                      "to_char(starts_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI'), "
                                      "freeze_at IS NOT NULL, allow_upsolving, submit_cooldown_sec")
            self.assertEqual(got, {
                # no starts_at -> created_at; practice -> fixed; leaderboard off -> hidden; no code -> open
                "dev": ("published", "fixed", None, "hidden", "open", None, False, "2026-10-01 10:00", False, True, 15),
                # duration -> personal; leaderboard on -> visible; code -> code + invite_code; plagiarism kept
                "olymp": ("published", "personal", 5400, "visible", "code", "ABC123", True, "2026-11-10 09:00", False,
                          True, 15),
                # a freeze without a personal timer stays
                "frozen": ("published", "fixed", None, "visible", "open", None, False, "2026-11-11 09:00", True, True, 15),
                # a blank join code means open
                "blank": ("published", "fixed", None, "hidden", "open", None, False, "2026-11-01 09:00", False, True, 15),
            })
            # contest problems: problem_id from the version; the older version of `sum` in `dev` becomes hidden
            rows = conn.execute(text("SELECT id, problem_id, label, hidden FROM contest_problems ORDER BY id")).all()
            self.assertEqual([tuple(r) for r in rows], [(1, 1, "P1", True), (2, 1, "P2", False), (3, 2, "P3", False),
                                                        (4, 1, "A", False)])
            # participations: joined_at = old started_at; started_at kept only in the personal contest
            rows = conn.execute(text("""SELECT c.slug, to_char(joined_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI'),
                                               to_char(started_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI')
                                        FROM participations p JOIN contests c ON c.id = p.contest_id ORDER BY c.id""")).all()
            self.assertEqual([tuple(r) for r in rows], [("dev", "10-05 08:00", None), ("olymp", "11-10 09:30", "11-10 09:30")])
            # languages: the existing row is kept as it was, the missing ones are added
            rows = conn.execute(text("SELECT key, name FROM languages ORDER BY key")).all()
            self.assertEqual([tuple(r) for r in rows], [("c", "C"), ("cpp", "C++"), ("python", "Python (dev)")])
            self.assertEqual(conn.execute(text("SELECT login, role::text FROM users WHERE id = 1")).one()[:],
                             ("dev-teacher", "admin"))
            # the rules hold for new rows
            for bad in ("UPDATE contests SET freeze_at = starts_at WHERE slug = 'olymp'",
                        "UPDATE contests SET personal_duration = interval '0' WHERE slug = 'olymp'",
                        "UPDATE contests SET timing_mode = 'personal' WHERE slug = 'dev'",
                        "UPDATE contests SET join_policy = 'code' WHERE slug = 'dev'",
                        "UPDATE contest_problems SET hidden = false WHERE id = 1"):
                with self.subTest(bad=bad), self.assertRaises(Exception):
                    with conn.begin_nested():
                        conn.execute(text(bad))

            # cases only the new schema can hold, for the way down
            conn.execute(text("UPDATE problem_versions SET status = 'failed' WHERE id = 3"))
            conn.execute(text("""INSERT INTO contests (id, slug, title, description_md, state, starts_at, timing_mode,
                                   personal_duration, scoring_mode, icpc_penalty_minutes, leaderboard, join_policy,
                                   allow_upsolving, submit_cooldown_sec, tests_after_end, plagiarism_enabled, created_by)
                                 VALUES (5, 'short', 's', '', 'draft', now(), 'personal', interval '90 seconds', 'points', 20,
                                   'after_end', 'open', true, 15, false, false, 1)"""))
            self.alembic(conn, "downgrade", "c13b55a3e4d5")

            rows = conn.execute(text("""SELECT id, status::text, published_at IS NOT NULL FROM problem_versions
                                        ORDER BY id""")).all()
            # ready -> published (published_at = created_at), pending_check -> draft, failed -> archived
            self.assertEqual([tuple(r) for r in rows], [(1, "published", True), (2, "draft", False), (3, "archived", False)])
            got = self.contests(conn, "mode::text, duration_minutes, leaderboard_enabled, join_code, plagiarism_check, "
                                      "to_char(starts_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')")
            self.assertEqual(got, {
                "dev": ("practice", None, False, None, False, "2026-10-01 10:00"),  # starts_at stays filled
                "olymp": ("timed", 90, True, "ABC123", True, "2026-11-10 09:00"),
                "frozen": ("timed", None, True, None, False, "2026-11-11 09:00"),  # fixed with an end -> timed
                "blank": ("practice", None, False, None, False, "2026-11-01 09:00"),
                "short": ("timed", 2, True, None, False, got["short"][5]),  # 90 s -> 2 minutes; after_end -> true
            })
            rows = conn.execute(text("SELECT c.slug, p.started_at IS NOT NULL FROM participations p "
                                     "JOIN contests c ON c.id = p.contest_id ORDER BY c.id")).all()
            self.assertEqual([tuple(r) for r in rows], [("dev", True), ("olymp", True)])  # started_at = joined_at
            self.assertEqual(conn.execute(text("SELECT count(*) FROM contest_problems")).scalar_one(), 4)
            self.alembic(conn, "upgrade", "head")  # and up again

    def test_problems_contests_migration_refuses_contradictions(self):
        """6bc3b8bd1c68 stops before changing anything when contests contradict the new rules."""
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "c13b55a3e4d5")
            conn.execute(text("INSERT INTO users (id, login, password_hash, display_name, role, is_active) "
                              "VALUES (1, 'admin', '!', 'A', 'admin', true)"))
            conn.execute(text("""
                INSERT INTO contests (slug, title, mode, starts_at, ends_at, duration_minutes, leaderboard_enabled,
                                      freeze_at, join_code, plagiarism_check, created_by, created_at, description_md, scoring_mode, icpc_penalty_minutes) VALUES
                  ('fine', 'ok', 'timed', '2026-11-10 09:00+00', '2026-11-10 12:00+00', NULL, false, NULL, 'AAA', false, 1, now(), '', 'points', 20),
                  ('frozen-personal', 'x', 'timed', '2026-11-10 09:00+00', '2026-11-10 12:00+00', 60, true,
                   '2026-11-10 11:00+00', NULL, false, 1, now(), '', 'points', 20),
                  ('zero', 'x', 'timed', '2026-11-10 09:00+00', NULL, 0, false, NULL, NULL, false, 1, now(), '', 'points', 20),
                  ('backwards', 'x', 'timed', '2026-11-10 09:00+00', '2026-11-10 08:00+00', NULL, false, NULL, NULL,
                   false, 1, now(), '', 'points', 20),
                  ('no-start', 'x', 'timed', NULL, '2020-01-01 00:00+00', NULL, false, NULL, NULL, false, 1,
                   '2026-10-01 00:00+00', '', 'points', 20),
                  ('twin-a', 'x', 'practice', NULL, NULL, NULL, false, NULL, 'SAME', false, 1, now(), '', 'points', 20),
                  ('twin-b', 'x', 'practice', NULL, NULL, NULL, false, NULL, ' SAME ', false, 1, now(), '', 'points', 20)"""))
            before = conn.execute(text("SELECT * FROM contests ORDER BY id")).all()
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "upgrade", "6bc3b8bd1c68")
        message = str(caught.exception)
        self.assertIn("6 contest(s) contradict", message)
        for slug, why in (("frozen-personal", "freeze with a personal timer"), ("zero", "not positive"),
                          ("backwards", "not after the start"), ("no-start", "not after the start"),
                          ("twin-a", "used by another contest"), ("twin-b", "used by another contest")):
            self.assertRegex(message, rf"'{slug}': [^;]*{why}")
        self.assertNotIn("'fine'", message)
        self.assertIn("nothing was changed", message)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "c13b55a3e4d5")
            self.assertEqual(conn.execute(text("SELECT * FROM contests ORDER BY id")).all(), before)
            self.assertEqual(conn.execute(text("SELECT enum_range(NULL::version_status)::text")).scalar_one(),
                             "{draft,published,archived}")
            self.assertEqual(conn.execute(text("SELECT to_regtype('timing_mode')")).scalar_one(), None)

    def test_problems_contests_downgrade_refuses_a_label_used_twice(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "c13b55a3e4d5")
            for statement in self.LEGACY.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
            self.alembic(conn, "upgrade", "6bc3b8bd1c68")
            conn.execute(text("UPDATE contest_problems SET label = 'P2' WHERE id = 1"))  # hidden row, same label
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "c13b55a3e4d5")
        self.assertIn("dev P2", str(caught.exception))
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "6bc3b8bd1c68")

    def test_dev_contest_and_dev_teacher_of_module_3_survive_and_dev_seed_reuses_them(self):
        """What dev_seed left in the production database during module 3, migrated, then dev_seed again."""
        import contextlib
        import io
        import json
        import os
        import tempfile
        from unittest import mock

        from sqlalchemy.orm import sessionmaker

        from app import db, dev_seed

        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "c13b55a3e4d5")
            for statement in """
                INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
                  (1, 'dev-teacher', '!', 'Dev Teacher', 'admin', true),
                  (2, 'dev-student-1', '!', 'Dev Student 1', 'student', true);
                INSERT INTO languages (key, name, enabled, config) VALUES ('python', 'Python 3', true, '{}'),
                  ('c', 'C', true, '{}'), ('cpp', 'C++', true, '{}');
                INSERT INTO blobs (sha256, size_bytes) VALUES (repeat('c', 64), 1);
                INSERT INTO problems (id, slug, owner_id) VALUES (1, 'dev-sum', 1);
                INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config,
                                              published_at, manifest_sha256)
                  VALUES (1, 1, 1, 'published', 'batch', '{"ru": "old"}', '{}', '{}', now(), repeat('d', 64));
                INSERT INTO contests (id, slug, title, description_md, mode, scoring_mode, icpc_penalty_minutes,
                                      leaderboard_enabled, plagiarism_check, created_by, created_at)
                  VALUES (1, 'dev', 'Dev contest', '', 'practice', 'points', 20, false, false, 1, '2026-10-20 10:00+00');
                INSERT INTO contest_problems (id, contest_id, problem_version_id, label, position) VALUES (1, 1, 1, 'P1', 0);
                INSERT INTO submissions (id, user_id, contest_problem_id, language_key, source_sha256, is_archive)
                  VALUES (1, 2, 1, 'python', repeat('c', 64), false);
                SELECT setval('users_id_seq', 2); SELECT setval('problems_id_seq', 1);
                SELECT setval('problem_versions_id_seq', 1); SELECT setval('contests_id_seq', 1);
                SELECT setval('contest_problems_id_seq', 1); SELECT setval('submissions_id_seq', 1)
            """.split(";"):
                if statement.strip():
                    conn.execute(text(statement))
            self.alembic(conn, "upgrade", "head")

            row = conn.execute(text("""SELECT id, state::text, timing_mode::text, access_mode::text, ends_at,
                                              to_char(starts_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI')
                                       FROM contests WHERE slug = 'dev'""")).one()
            self.assertEqual(tuple(row), (1, "published", "fixed", "open", None, "2026-10-20 10:00"))
            self.assertEqual(tuple(conn.execute(text("SELECT id, role::text FROM users WHERE login = 'dev-teacher'")).one()),
                             (1, "admin"))
            self.assertEqual(conn.execute(text("SELECT status::text FROM problem_versions WHERE id = 1")).scalar_one(), "ready")

        with tempfile.TemporaryDirectory() as blobs:
            env = {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": blobs}
            out = io.StringIO()
            with mock.patch.dict(os.environ, env), mock.patch.object(db, "get_sessionmaker", lambda: sessionmaker(engine)), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(dev_seed.main(["--count", "1"]), 0)
        made = json.loads(out.getvalue())[0]
        self.assertEqual(made["import"], "created")  # the version of module 3 has no content digest yet
        with engine.connect() as conn:
            # the same contest and the same admin are reused; no second dev contest or dev-teacher
            self.assertEqual(conn.execute(text("SELECT count(*) FROM contests")).scalar_one(), 1)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM users WHERE role = 'admin'")).scalar_one(), 1)
            # dev-sum got a new version (the old one has no tests); its old entry is hidden with its submission kept
            rows = conn.execute(text("""SELECT cp.id, cp.label, cp.hidden, pv.version FROM contest_problems cp
                                        JOIN problem_versions pv ON pv.id = cp.problem_version_id ORDER BY cp.id""")).all()
            self.assertEqual([tuple(r) for r in rows], [(1, "P1", True, 1), (2, "P2", False, 2)])
            self.assertEqual(made["contest_problem_id"], 2)
            self.assertEqual(conn.execute(text("SELECT contest_problem_id FROM submissions WHERE id = 1")).scalar_one(), 1)
            status = conn.execute(text("SELECT status::text FROM judgements WHERE id = :j"), {"j": made["judgement_id"]})
            self.assertEqual(status.scalar_one(), "queued")
            owner = conn.execute(text("SELECT p.owner_id FROM problems p WHERE slug = 'dev-sum'")).scalar_one()
            self.assertEqual(owner, 1)
            counts = lambda: tuple(conn.execute(text(  # noqa: E731
                "SELECT (SELECT count(*) FROM problem_versions), (SELECT count(*) FROM contest_problems),"
                " (SELECT count(*) FROM contest_problems WHERE hidden), (SELECT count(*) FROM blobs)")).one())
            before = counts()

        # dev_seed again: the content is known now -> "exists"; no new version, no new hidden row, no new blob
        with tempfile.TemporaryDirectory() as blobs:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": blobs}), \
                    mock.patch.object(db, "get_sessionmaker", lambda: sessionmaker(engine)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(dev_seed.main(["--count", "1"]), 0)
        again = json.loads(out.getvalue())[0]
        self.assertEqual((again["import"], again["contest_problem_id"], again["problem_version_id"]),
                         ("exists", made["contest_problem_id"], made["problem_version_id"]))
        self.assertIn("dev-sum: already exists as v2", err.getvalue())
        with engine.connect() as conn:
            self.assertEqual(counts(), before)

    # ---------------------------------------------------------------- 6fca2cdc55b2: groups, access, deadlines (4a.3a)
    BEFORE_GROUPS = """
        INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
          (1, 'admin', '!', 'A', 'admin', true), (2, 'pupil', '!', 'P', 'student', true),
          (3, 'other', '!', 'O', 'student', true);
        INSERT INTO problems (id, slug, owner_id) VALUES (1, 'sum', 1);
        INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config)
          VALUES (1, 1, 1, 'ready', 'batch', '{}', '{}', '{}');
        INSERT INTO contests (id, slug, title, description_md, state, starts_at, timing_mode, scoring_mode,
                              icpc_penalty_minutes, leaderboard, join_policy, invite_code, allow_upsolving,
                              submit_cooldown_sec, tests_after_end, plagiarism_enabled, created_by) VALUES
          (1, 'practice', 'p', '', 'published', '2026-10-01 00:00+00', 'fixed', 'points', 20, 'hidden', 'open', NULL,
           true, 15, false, false, 1),
          (2, 'lesson', 'l', '', 'published', '2026-10-01 00:00+00', 'fixed', 'points', 20, 'hidden', 'code', 'ACDEFG',
           true, 15, false, false, 1);
        INSERT INTO contest_problems (id, contest_id, problem_id, problem_version_id, label, position) VALUES
          (1, 1, 1, 1, 'A', 0), (2, 2, 1, 1, 'A', 0);
        INSERT INTO participations (contest_id, user_id, joined_at) VALUES
          (1, 2, '2026-10-05 08:00+00'), (2, 2, '2026-10-05 08:00+00'), (2, 3, '2026-10-06 08:00+00');
        INSERT INTO submissions (id, user_id, contest_problem_id, is_archive) VALUES (1, 2, 2, false)
    """

    def run_sql(self, conn, script):
        for statement in script.split(";"):
            if statement.strip():
                conn.execute(text(statement))

    def test_groups_migration_carries_access_over_and_back(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "4d8a1c2e9f57")
            self.run_sql(conn, self.BEFORE_GROUPS)
            self.alembic(conn, "upgrade", "6fca2cdc55b2")

            self.assertEqual(self.contests(conn, "access_mode::text, invite_code"),
                             {"practice": ("open", None), "lesson": ("code", "ACDEFG")})
            rows = conn.execute(text("""SELECT c.slug, p.user_id, p.joined_via::text FROM participations p
                                        JOIN contests c ON c.id = p.contest_id ORDER BY p.id""")).all()
            self.assertEqual([tuple(r) for r in rows], [("practice", 2, "open"), ("lesson", 2, "code"), ("lesson", 3, "code")])
            rows = conn.execute(text("""SELECT available_from, due_at, after_due_statement::text, after_due_submit::text,
                                               note_md FROM contest_problems ORDER BY id""")).all()
            self.assertEqual([tuple(r) for r in rows], [(None, None, "visible", "late", None)] * 2)
            self.assertEqual(conn.execute(text("SELECT late FROM submissions")).scalar_one(), False)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM users WHERE timezone IS NOT NULL")).scalar_one(), 0)
            self.assertEqual(conn.execute(text("SELECT to_regtype('join_policy')")).scalar_one(), None)

            # the rules hold for new rows
            conn.execute(text("INSERT INTO groups (id, name) VALUES (1, '7Б')"))
            conn.execute(text("INSERT INTO group_members (group_id, user_id) VALUES (1, 2)"))
            for bad in ("UPDATE contests SET access_mode = 'code_or_groups' WHERE slug = 'practice'",  # no code
                        "UPDATE contests SET access_mode = 'code', invite_code = NULL WHERE slug = 'lesson'",
                        "UPDATE contest_problems SET available_from = '2026-11-02 10:00+00', "
                        "due_at = '2026-11-02 10:00+00' WHERE id = 1",
                        "INSERT INTO groups (name) VALUES ('7б')",  # the same name ignoring case
                        "INSERT INTO groups (name) VALUES ('  ')",
                        "INSERT INTO group_members (group_id, user_id) VALUES (1, 2)",  # twice at once
                        "INSERT INTO deadline_extensions (contest_problem_id, due_at) VALUES (1, now())",
                        "INSERT INTO deadline_extensions (contest_problem_id, user_id, group_id, due_at) "
                        "VALUES (1, 2, 1, now())",
                        "INSERT INTO deadline_extensions (contest_problem_id, user_id, due_at) "
                        "VALUES (1, 2, now()), (1, 2, now())"):
                with self.subTest(bad=bad), self.assertRaises(Exception):
                    with conn.begin_nested():
                        conn.execute(text(bad))
            # what is allowed: an archived name is free again, removed members may come back, groups mode
            conn.execute(text("UPDATE groups SET archived_at = now() WHERE id = 1"))
            conn.execute(text("INSERT INTO groups (id, name) VALUES (2, '7б')"))
            conn.execute(text("UPDATE group_members SET removed_at = now()"))
            conn.execute(text("INSERT INTO group_members (group_id, user_id) VALUES (1, 2)"))
            conn.execute(text("UPDATE contests SET access_mode = 'groups' WHERE slug = 'practice'"))

            # nothing the old schema cannot hold -> back to join_policy
            conn.execute(text("UPDATE contests SET access_mode = 'open' WHERE slug = 'practice'"))
            conn.execute(text("DELETE FROM group_members; DELETE FROM groups"))
            self.alembic(conn, "downgrade", "4d8a1c2e9f57")
            self.assertEqual(self.contests(conn, "join_policy::text, invite_code"),
                             {"practice": ("open", None), "lesson": ("code", "ACDEFG")})
            self.assertEqual(conn.execute(text("SELECT count(*) FROM participations")).scalar_one(), 3)
            self.assertEqual(conn.execute(text("SELECT to_regclass('groups')")).scalar_one(), None)
            self.alembic(conn, "upgrade", "head")

    def test_groups_downgrade_refuses_what_the_old_schema_cannot_hold(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "4d8a1c2e9f57")
            self.run_sql(conn, self.BEFORE_GROUPS)
            self.alembic(conn, "upgrade", "6fca2cdc55b2")
        cases = {
            "contest practice: access mode groups": "UPDATE contests SET access_mode = 'groups' WHERE slug = 'practice'",
            "groups: 1": "INSERT INTO groups (name) VALUES ('7Б')",
            "deadline extensions: 1": "INSERT INTO deadline_extensions (contest_problem_id, user_id, due_at) "
                                      "VALUES (1, 2, now())",
            "contest lesson A: deadline": "UPDATE contest_problems SET due_at = now() WHERE id = 2",
            "contest practice A: deadline": "UPDATE contest_problems SET after_due_submit = 'no' WHERE id = 1",
            "late submissions: 1": "UPDATE submissions SET late = true",
            "users with a time zone: 1": "UPDATE users SET timezone = 'Asia/Novosibirsk' WHERE id = 2",
            # open -> code after pupil 3 came in the open way: with join_policy every participant is let in again
            "contest lesson: 1 participation(s) that give no access now (joined via open)":
                "UPDATE participations SET joined_via = 'open' WHERE user_id = 3",
        }
        for expected, change in cases.items():
            with self.subTest(expected):
                with engine.begin() as conn:
                    conn.execute(text(change))
                    before = conn.execute(text("SELECT * FROM contests ORDER BY id")).all()
                with self.assertRaises(RuntimeError) as caught:
                    with engine.begin() as conn:
                        self.alembic(conn, "downgrade", "4d8a1c2e9f57")
                self.assertIn(expected, str(caught.exception))
                self.assertIn("nothing was changed", str(caught.exception))
                with engine.begin() as conn:
                    self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                                     "6fca2cdc55b2")
                    self.assertEqual(conn.execute(text("SELECT * FROM contests ORDER BY id")).all(), before)
                    for undo in ("UPDATE contests SET access_mode = 'open' WHERE slug = 'practice'",
                                 "DELETE FROM deadline_extensions", "DELETE FROM groups",
                                 "UPDATE contest_problems SET due_at = NULL, after_due_submit = 'late'",
                                 "UPDATE submissions SET late = false", "UPDATE users SET timezone = NULL",
                                 "UPDATE participations SET joined_via = 'code' WHERE contest_id = 2"):
                        conn.execute(text(undo))

    def test_groups_upgrade_refuses_a_code_contest_without_a_code(self):
        """Impossible under the CHECK of 4a.2; the guard is formal, so the CHECK is dropped to reach it."""
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "4d8a1c2e9f57")
            self.run_sql(conn, self.BEFORE_GROUPS)
            conn.execute(text("ALTER TABLE contests DROP CONSTRAINT ck_contests_invite_code"))
            conn.execute(text("UPDATE contests SET invite_code = NULL WHERE slug = 'lesson'"))
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "upgrade", "6fca2cdc55b2")
        self.assertIn("id=2 'lesson'", str(caught.exception))
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "4d8a1c2e9f57")
            self.assertEqual(conn.execute(text("SELECT to_regtype('access_mode')")).scalar_one(), None)

    # ---------------------------------------------------------------- b7e3d91a4c62: checks of problem versions (4a.4)
    BEFORE_CHECKS = """
        INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
          (1, 'admin', '!', 'A', 'admin', true);
        INSERT INTO problems (id, slug, owner_id) VALUES (1, 'sum', 1);
        INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config)
          VALUES (1, 1, 1, 'ready', 'batch', '{}', '{}', '{}');
        INSERT INTO worker_tokens (id, name, token_hash, capabilities, revoked_at) VALUES
          (1, 'box', 'h1', '["cpu"]', NULL), (2, 'gpu-box', 'h2', '["cpu", "gpu"]', NULL),
          (3, 'old', 'h3', '["cpu"]', now()), (4, 'gpu-only', 'h4', '["gpu"]', NULL),
          (5, 'new', 'h5', '["cpu", "problem_check"]', NULL);
        INSERT INTO workers (id, name, token_id, capabilities, state, info, stats) VALUES
          (1, 'box-w0', 1, '["cpu"]', 'idle', '{}', '{}')
    """

    def token_caps(self, conn):
        return dict(conn.execute(text("SELECT name, capabilities FROM worker_tokens ORDER BY id")).all())

    def test_checks_migration_allows_the_capability_and_keeps_versions_ready(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "6fca2cdc55b2")
            self.run_sql(conn, self.BEFORE_CHECKS)
            self.alembic(conn, "upgrade", "b7e3d91a4c62")
            self.assertEqual(self.token_caps(conn), {
                "box": ["cpu", "problem_check"], "gpu-box": ["cpu", "gpu", "problem_check"],
                "old": ["cpu"],  # revoked: left alone
                "gpu-only": ["gpu"],  # no cpu: not a judging machine of this kind
                "new": ["cpu", "problem_check"],  # not doubled
            })
            self.assertEqual(conn.execute(text("SELECT status::text FROM problem_versions")).scalar_one(), "ready")
            self.assertEqual(conn.execute(text("SELECT count(*) FROM version_checks")).scalar_one(), 0)

            # the rules hold for new rows
            conn.execute(text("INSERT INTO version_checks (id, problem_version_id, reason, status, attempt, failures) "
                              "VALUES (1, 1, 'import', 'queued', 0, 0)"))
            for bad in ("INSERT INTO version_checks (problem_version_id, reason, status, attempt, failures) "
                        "VALUES (1, 'recheck', 'running', 1, 0)",  # a second unfinished check of the version
                        "UPDATE version_checks SET status = 'done'",  # done without an outcome
                        "UPDATE version_checks SET outcome = 'ready'",  # an outcome while queued
                        "INSERT INTO version_checks (problem_version_id, reason, status, attempt, failures) "
                        "VALUES (1, 'other', 'queued', 0, 0)"):
                with self.subTest(bad=bad), self.assertRaises(Exception):
                    with conn.begin_nested():
                        conn.execute(text(bad))
            conn.execute(text("UPDATE version_checks SET status = 'done', outcome = 'failed'"))
            conn.execute(text("INSERT INTO version_checks (problem_version_id, reason, status, attempt, failures) "
                              "VALUES (1, 'recheck', 'queued', 0, 0)"))  # a finished check does not block a new one

            # down: refused while checks exist, nothing changed
            before = self.token_caps(conn)
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "6fca2cdc55b2")
        self.assertIn("version checks: 2", str(caught.exception))
        self.assertIn("nothing was changed", str(caught.exception))
        with engine.begin() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "b7e3d91a4c62")
            self.assertEqual(self.token_caps(conn), before)
            conn.execute(text("DELETE FROM version_checks"))
            conn.execute(text("""UPDATE workers SET capabilities = '["cpu", "problem_check"]'"""))
            self.alembic(conn, "downgrade", "6fca2cdc55b2")
            self.assertEqual(self.token_caps(conn), {"box": ["cpu"], "gpu-box": ["cpu", "gpu"], "old": ["cpu"],
                                                     "gpu-only": ["gpu"], "new": ["cpu"]})
            self.assertEqual(conn.execute(text("SELECT capabilities FROM workers")).scalar_one(), ["cpu"])
            self.assertEqual(conn.execute(text("SELECT to_regclass('version_checks')")).scalar_one(), None)
            self.assertEqual(conn.execute(text("SELECT to_regtype('check_status')")).scalar_one(), None)
            self.alembic(conn, "upgrade", "head")
