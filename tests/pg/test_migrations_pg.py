"""Alembic migrations == models.py, on a real PostgreSQL (skipped unless JUDGE_TEST_DATABASE_URL is set).

Fails when someone changes models.py without a migration, or edits a migration by hand so that it
no longer builds the same schema. compare_metadata does not look at the WHERE of partial indexes
or at CHECK constraints, so index, enum and constraint definitions are also compared with what
create_all builds (PgCase's schema). Hand-written data steps of migrations have their own tests.
"""
import re

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg, schema_name

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
        schema = schema_name("m")  # with its creation time: pgbase sweeps it if an interrupted run left it
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

    # ---------------------------------------------------------------- b32303b5df22: class feed (4v.1, D-059)
    BEFORE_FEED = BEFORE_CHECKS + """;
        INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
          (2, 'pupil', '!', 'P', 'student', true);
        INSERT INTO groups (id, name) VALUES (1, '7Б');
        INSERT INTO contests (id, slug, title, starts_at, access_mode, state, timing_mode, scoring_mode,
                              icpc_penalty_minutes, leaderboard, allow_upsolving, submit_cooldown_sec, tests_after_end,
                              plagiarism_enabled, description_md)
          VALUES (1, 'lesson', 'L', '2026-11-01 09:00+00', 'open', 'published', 'fixed', 'points', 20, 'hidden', true,
                  15, false, false, '');
        INSERT INTO contest_problems (id, contest_id, problem_id, problem_version_id, label, position, due_at)
          VALUES (1, 1, 1, 1, 'A', 0, '2026-11-10 10:00+00');
        INSERT INTO deadline_extensions (id, contest_problem_id, user_id, due_at, reason) VALUES
          (1, 1, 2, '2026-11-11 10:00+00', 'болел');
        INSERT INTO deadline_extensions (id, contest_problem_id, group_id, due_at) VALUES
          (2, 1, 1, '2026-11-12 10:00+00')
    """
    FEED_TYPES = ("feed_item_kind", "feed_item_state", "grading_scale", "answer_kind", "grades_release", "work_status",
                  "review_action")

    def extensions(self, conn):
        return [tuple(r) for r in conn.execute(text(
            "SELECT id, contest_problem_id, user_id, group_id, due_at, reason FROM deadline_extensions ORDER BY id"))]

    def test_feed_migration_keeps_extensions_of_4a_up_down_up(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b7e3d91a4c62")
            self.run_sql(conn, self.BEFORE_FEED)
            before = self.extensions(conn)
            self.assertEqual(len(before), 2)
            self.alembic(conn, "upgrade", "head")  # through the feed; compare_metadata needs the head
            self.assertEqual(self.extensions(conn), before)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM deadline_extensions WHERE feed_item_id IS NULL")
                                          ).scalar_one(), 2, "existing extensions pass one_target as they are")
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "alembic check after the upgrade: models.py and the migration differ")
            # the old shape still holds: an extension of a contest problem without a feed item
            conn.execute(text("INSERT INTO deadline_extensions (id, contest_problem_id, user_id, due_at) "
                              "VALUES (3, 1, 1, '2026-11-13 10:00+00')"))
            conn.execute(text("DELETE FROM deadline_extensions WHERE id = 3"))

            self.alembic(conn, "downgrade", "b7e3d91a4c62")
            self.assertEqual(self.extensions(conn), before)
            self.assertEqual(conn.execute(text("SELECT to_regclass('feed_items')")).scalar_one(), None)
            for name in self.FEED_TYPES:
                self.assertEqual(conn.execute(text("SELECT to_regtype(:n)"), {"n": name}).scalar_one(), None, name)
            self.assertIsNotNone(conn.execute(text("SELECT to_regtype('after_due_submit')")).scalar_one(),
                                 "the types of contest_problems stay")
            nullable = conn.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_schema = "
                                         "current_schema() AND table_name = 'deadline_extensions' AND column_name = "
                                         "'contest_problem_id'")).scalar_one()
            self.assertEqual(nullable, "NO")

            self.alembic(conn, "upgrade", "b32303b5df22")
            self.assertEqual(self.extensions(conn), before)

    def test_feed_downgrade_refuses_while_items_exist(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b7e3d91a4c62")
            self.run_sql(conn, self.BEFORE_FEED)
            self.alembic(conn, "upgrade", "b32303b5df22")
        cases = {
            "feed items: 1, extensions of feed items: 0":
                ["INSERT INTO feed_items (id, kind, state, title, body_md, contest_id) "
                 "VALUES (1, 'contest', 'draft', 'L', '', 1)"],
            "feed items: 1, extensions of feed items: 1":
                ["INSERT INTO feed_items (id, kind, state, title, body_md, grading, answer_kind, grades_release, due_at) "
                 "VALUES (1, 'assignment', 'draft', 'H', '', 'none', 'mark', 'manual', '2026-11-10 10:00+00')",
                 "INSERT INTO deadline_extensions (id, feed_item_id, user_id, due_at) "
                 "VALUES (10, 1, 2, '2026-11-11 10:00+00')"],
        }
        for expected, changes in cases.items():
            with self.subTest(expected):
                with engine.begin() as conn:
                    for change in changes:
                        conn.execute(text(change))
                with self.assertRaises(RuntimeError) as caught:
                    with engine.begin() as conn:
                        self.alembic(conn, "downgrade", "b7e3d91a4c62")
                self.assertIn(expected, str(caught.exception))
                self.assertIn("nothing was changed", str(caught.exception))
                with engine.begin() as conn:
                    self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                                     "b32303b5df22")
                    self.assertEqual(conn.execute(text("SELECT count(*) FROM feed_items")).scalar_one(), 1)
                    conn.execute(text("DELETE FROM deadline_extensions WHERE feed_item_id IS NOT NULL"))
                    conn.execute(text("DELETE FROM feed_items"))
        with engine.begin() as conn:
            self.alembic(conn, "downgrade", "b7e3d91a4c62")
            self.assertEqual(len(self.extensions(conn)), 2)

    # ---------------------------------------------------------------- e73284028c20: comment threads (4v.2, D-072)
    BEFORE_COMMENTS = BEFORE_FEED + """;
        INSERT INTO feed_items (id, kind, state, title, body_md, contest_id) VALUES (1, 'contest', 'draft', 'L', '', 1);
        INSERT INTO feed_items (id, kind, state, title, body_md, grading, answer_kind, grades_release, published_at)
          VALUES (2, 'assignment', 'published', 'H', '', 'none', 'mark', 'manual', '2026-11-05 10:00+00');
        INSERT INTO feed_item_audience (item_id, user_id) VALUES (2, 2)
    """
    COMMENTS = ["INSERT INTO comment_threads (id, item_id, student_id) VALUES (1, 2, 2)",
                "INSERT INTO comments (id, thread_id, author_id, body) VALUES (1, 1, 2, 'вопрос')",
                "INSERT INTO thread_reads (user_id, thread_id, last_read_comment_id) VALUES (2, 1, 1)"]

    def feed_rows(self, conn):
        return [tuple(r) for r in conn.execute(text("SELECT id, kind::text, state::text, title FROM feed_items "
                                                    "ORDER BY id"))]

    def test_comments_migration_up_down_up_keeps_the_feed(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b32303b5df22")
            self.run_sql(conn, self.BEFORE_COMMENTS)
            before = self.feed_rows(conn)
            self.alembic(conn, "upgrade", "e73284028c20")
            self.assertEqual(self.feed_rows(conn), before)
            self.assertEqual(conn.execute(text("SELECT array_agg(comments_enabled ORDER BY id) FROM feed_items")
                                          ).scalar_one(), [False, False], "existing items get false")
            self.alembic(conn, "upgrade", "head")  # compare_metadata needs the head; nothing later refuses the way back
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "alembic check after the upgrade: models.py and the migrations differ")
            self.alembic(conn, "downgrade", "e73284028c20")
            conn.execute(text("UPDATE feed_items SET comments_enabled = true WHERE id = 2"))

            self.alembic(conn, "downgrade", "b32303b5df22")  # no threads: allowed, only the flags are lost
            self.assertEqual(self.feed_rows(conn), before)
            for table in ("comment_threads", "comments", "thread_reads"):
                self.assertIsNone(conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one(), table)
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'feed_items' AND column_name = 'comments_enabled'")).scalar_one(), 0)

            self.alembic(conn, "upgrade", "e73284028c20")
            self.assertEqual(self.feed_rows(conn), before)
            self.assertEqual(conn.execute(text("SELECT bool_or(comments_enabled) FROM feed_items")).scalar_one(),
                             False, "the flag set before the downgrade is gone: false again")

    def test_comments_downgrade_refuses_while_threads_exist(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b32303b5df22")
            self.run_sql(conn, self.BEFORE_COMMENTS)
            self.alembic(conn, "upgrade", "e73284028c20")
            for change in self.COMMENTS:
                conn.execute(text(change))
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "b32303b5df22")
        self.assertIn("comment threads: 1, comments: 1", str(caught.exception))
        self.assertIn("nothing was changed", str(caught.exception))
        with engine.begin() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "e73284028c20")
            self.assertEqual(conn.execute(text("SELECT count(*) FROM thread_reads")).scalar_one(), 1)

    # ---------------------------------------------------------------- 32cd8723a2bc: attachments and links (4v.3, D-079)
    BEFORE_ATTACHMENTS = BEFORE_COMMENTS + """;
        INSERT INTO blobs (sha256, size_bytes) VALUES (repeat('d', 64), 6), (repeat('e', 64), 7);
        INSERT INTO assignment_works (id, item_id, user_id, status, status_at)
          VALUES (1, 2, 2, 'submitted', '2026-11-06 10:00+00');
        INSERT INTO assignment_submissions (id, work_id, created_at)
          VALUES (1, 1, '2026-11-06 10:00+00'), (2, 1, '2026-11-06 11:00+00');
        INSERT INTO assignment_submission_files (submission_id, path, blob_sha256, size, position)
          VALUES (1, 'answer.md', repeat('d', 64), 6, 0), (2, 'answer.md', repeat('e', 64), 7, 0)
    """
    FILES_TABLE_SQL = ("SELECT relfilenode FROM pg_class WHERE oid = 'assignment_submission_files'::regclass")

    def answers(self, conn):
        return [tuple(r) for r in conn.execute(text(
            "SELECT id, submission_id, path, blob_sha256, size, position FROM assignment_submission_files ORDER BY id"))]

    def test_attachments_migration_up_down_up_keeps_the_answers_of_4v1(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "e73284028c20")
            self.run_sql(conn, self.BEFORE_ATTACHMENTS)
            before = self.answers(conn)
            table_file = conn.execute(text(self.FILES_TABLE_SQL)).scalar_one()
            self.alembic(conn, "upgrade", "32cd8723a2bc")
            self.assertEqual(self.answers(conn), before)
            self.assertEqual(conn.execute(text("SELECT array_agg(kind::text ORDER BY id) FROM assignment_submission_files"
                                               )).scalar_one(), ["text", "text"])
            # no rewrite and no UPDATE: the same file of the table, the kind of the old rows read from the catalog
            self.assertEqual(conn.execute(text(self.FILES_TABLE_SQL)).scalar_one(), table_file)
            missing = conn.execute(text("SELECT atthasmissing, attmissingval::text FROM pg_attribute WHERE attrelid = "
                                        "'assignment_submission_files'::regclass AND attname = 'kind'")).one()
            self.assertEqual(tuple(missing), (True, "{text}"))
            self.alembic(conn, "upgrade", "head")  # compare_metadata needs the head; no links here, 4v.4 goes back
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "alembic check after the upgrade: models.py and the migration differ")
            self.alembic(conn, "downgrade", "32cd8723a2bc")
            # what 4v.3 allows now and the old schema did not: a file named answer.md next to the text answer
            conn.execute(text("INSERT INTO assignment_submission_files (submission_id, kind, path, blob_sha256, size, position) "
                              "VALUES (1, 'file', 'answer.md', repeat('e', 64), 7, 1)"))
            conn.execute(text("DELETE FROM assignment_submission_files WHERE kind = 'file'"))

            self.alembic(conn, "downgrade", "e73284028c20")  # nothing of 4v.3: allowed, nothing is lost
            self.assertEqual(self.answers(conn), before)
            for table in ("attachments", "assignment_submission_links"):
                self.assertIsNone(conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one(), table)
            for name in ("submission_file_kind", "attachment_kind"):
                self.assertIsNone(conn.execute(text("SELECT to_regtype(:n)"), {"n": name}).scalar_one(), name)
            with self.assertRaises(Exception) as caught, conn.begin_nested():
                conn.execute(text("INSERT INTO assignment_submission_files (submission_id, path, blob_sha256, size, position) "
                                  "VALUES (1, 'answer.md', repeat('e', 64), 7, 1)"))
            self.assertIn("uq_assignment_submission_files_submission_path", str(caught.exception))
            with self.assertRaises(Exception) as caught, conn.begin_nested():
                conn.execute(text("UPDATE feed_items SET answer_kind = 'files' WHERE id = 2"))
            self.assertIn("ck_feed_items_answer_kind_before_4v3", str(caught.exception))

            self.alembic(conn, "upgrade", "32cd8723a2bc")
            self.assertEqual(self.answers(conn), before)
            self.assertEqual(conn.execute(text("SELECT count(*) FROM assignment_submission_files WHERE kind = 'text'")
                                          ).scalar_one(), 2)

    def test_attachments_downgrade_refuses_what_the_old_schema_cannot_hold(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "e73284028c20")
            self.run_sql(conn, self.BEFORE_ATTACHMENTS)
            self.alembic(conn, "upgrade", "32cd8723a2bc")
        cases = {
            "uploaded files of hand-ins: 1": (
                "INSERT INTO assignment_submission_files (submission_id, kind, path, blob_sha256, size, position) "
                "VALUES (2, 'file', 'main.py', repeat('d', 64), 6, 1)",
                "DELETE FROM assignment_submission_files WHERE kind = 'file'"),
            "links of hand-ins: 1": (
                "INSERT INTO assignment_submission_links (submission_id, url, position) VALUES (2, 'https://e.ru/', 0)",
                "DELETE FROM assignment_submission_links"),
            "attachments: 1": (
                "INSERT INTO attachments (item_id, kind, url, position) VALUES (1, 'link', 'https://e.ru/', 0)",
                "DELETE FROM attachments"),
            "items answered with files: 1": (
                "UPDATE feed_items SET answer_kind = 'text_or_files' WHERE id = 2",
                "UPDATE feed_items SET answer_kind = 'mark' WHERE id = 2"),
        }
        for expected, (change, undo) in cases.items():
            with self.subTest(expected):
                with engine.begin() as conn:
                    conn.execute(text(change))
                with self.assertRaises(RuntimeError) as caught:
                    with engine.begin() as conn:
                        self.alembic(conn, "downgrade", "e73284028c20")
                self.assertIn(expected, str(caught.exception))
                self.assertIn("nothing was changed", str(caught.exception))
                with engine.begin() as conn:
                    self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                                     "32cd8723a2bc")
                    conn.execute(text(undo))
        with engine.begin() as conn:
            self.alembic(conn, "downgrade", "e73284028c20")

    def test_attachments_upgrade_refuses_a_file_that_is_not_the_text_answer(self):
        """4v.1 writes only answer.md; another row would get a kind nobody knows, so the upgrade stops first."""
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "e73284028c20")
            self.run_sql(conn, self.BEFORE_ATTACHMENTS + """;
                INSERT INTO assignment_submission_files (submission_id, path, blob_sha256, size, position)
                  VALUES (1, 'main.py', repeat('e', 64), 7, 1)""")
        with self.assertRaises(RuntimeError) as caught:
            with engine.begin() as conn:
                self.alembic(conn, "upgrade", "32cd8723a2bc")
        self.assertIn("files of hand-ins that are not answer.md: 1", str(caught.exception))
        self.assertIn("nothing was changed", str(caught.exception))
        with engine.begin() as conn:
            self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(), "e73284028c20")

    # ---------------------------------------------------------------- b8e0cbcc4cda: the library of materials (4v.4, D-084)
    # links in the texts before 4v.4: prefixes in any case, a text instead of the title, the same slug twice and in two
    # languages, and what only looks like a link (upper case slug, one character, spaces, empty text, another prefix)
    BEFORE_MATERIALS = """
        INSERT INTO attachments (item_id, kind, url, position) VALUES (2, 'link', 'https://e.ru/', 0);
        UPDATE problem_versions SET statements = '{"ru": "См. [[памятка:cycles]], [[ПАМЯТКА:io-fast|быстрый ввод]] и снова [[Памятка:cycles]]", "en": "[[памятка:io-fast]] [[памятка:Bad Slug]] [[ памятка:spaced]] [[memo:other]]"}' WHERE id = 1;
        UPDATE feed_items SET body_md = '[[памятка:loops|циклы]] [[памятка:loops]] [[памятка:x]] [[памятка:loops|]]' WHERE id = 2;
        UPDATE feed_items SET body_md = 'Перед контестом прочитайте [[ПаМяТкА:arrays]]' WHERE id = 1
    """
    BACKFILLED = [("io-fast", 1, None), ("cycles", 1, None), ("arrays", None, 1), ("loops", None, 2)]

    def refs(self, conn):
        return [tuple(r) for r in conn.execute(text(
            "SELECT slug, problem_version_id, feed_item_id FROM material_refs ORDER BY id"))]

    def attachment_rows(self, conn):
        return [tuple(r) for r in conn.execute(text("SELECT id, item_id, kind::text, url FROM attachments ORDER BY id"))]

    def test_materials_migration_backfills_links_and_goes_up_down_up(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "e73284028c20")
            self.run_sql(conn, self.BEFORE_ATTACHMENTS)
            self.alembic(conn, "upgrade", "32cd8723a2bc")
            self.run_sql(conn, self.BEFORE_MATERIALS)
            feed, attachments, answers = self.feed_rows(conn), self.attachment_rows(conn), self.answers(conn)
            self.alembic(conn, "upgrade", "b8e0cbcc4cda")
            # one row per source and slug: languages of a statement are one source, malformed links are not links
            self.assertEqual(self.refs(conn), self.BACKFILLED)
            self.assertEqual((self.feed_rows(conn), self.attachment_rows(conn), self.answers(conn)),
                             (feed, attachments, answers))
            self.alembic(conn, "upgrade", "head")  # models.py is the head, not this revision
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "alembic check after the upgrade: models.py and the migration differ")

        with self.assertRaises(RuntimeError) as caught:  # the backfilled links are data of 4v.4
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "32cd8723a2bc")
        self.assertIn("links to materials: 4", str(caught.exception))
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM material_refs"))
            self.alembic(conn, "downgrade", "32cd8723a2bc")  # nothing of 4v.4 left: allowed, nothing is lost
            self.assertEqual((self.feed_rows(conn), self.attachment_rows(conn), self.answers(conn)),
                             (feed, attachments, answers))
            for table in ("materials", "material_versions", "material_groups", "library_sections", "material_sections",
                          "material_refs"):
                self.assertIsNone(conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one(), table)
            self.assertIsNone(conn.execute(text("SELECT to_regtype('material_library')")).scalar_one())
            with self.assertRaises(Exception) as caught, conn.begin_nested():
                conn.execute(text("INSERT INTO feed_items (kind, state, title, body_md) "
                                  "VALUES ('material', 'draft', 'M', '')"))
            self.assertIn("ck_feed_items_material_after_4v4", str(caught.exception))
            with self.assertRaises(Exception) as caught, conn.begin_nested():
                conn.execute(text("INSERT INTO attachments (kind, url, position) VALUES ('link', 'https://e.ru/', 1)"))
            self.assertIn("item_id", str(caught.exception))

            self.alembic(conn, "upgrade", "b8e0cbcc4cda")
            self.assertEqual(self.refs(conn), self.BACKFILLED, "the second upgrade fills the links again")

    def test_materials_migration_without_data_up_down_up(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b8e0cbcc4cda")
            self.assertEqual(self.refs(conn), [])
            self.alembic(conn, "downgrade", "32cd8723a2bc")
            self.alembic(conn, "upgrade", "head")  # models.py is the head
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [])

    def test_materials_downgrade_refuses_what_the_old_schema_cannot_hold(self):
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "e73284028c20")
            self.run_sql(conn, self.BEFORE_ATTACHMENTS)
            self.alembic(conn, "upgrade", "b8e0cbcc4cda")
            conn.execute(text("INSERT INTO materials (id, slug, title) VALUES (9, 'held', 'H')"))
            conn.execute(text("INSERT INTO material_versions (id, material_id, version, body_md) VALUES (9, 9, 1, '')"))
        material = ("INSERT INTO materials (id, slug, title) VALUES (1, 'cycles', 'Циклы')", "DELETE FROM materials WHERE id = 1")
        cases = {
            "materials: 2": material,
            "library sections: 1": ("INSERT INTO library_sections (title, position) VALUES ('Python', 0)",
                                    "DELETE FROM library_sections"),
            "material items: 1": ("INSERT INTO feed_items (id, kind, state, title, body_md, material_id) "
                                  "VALUES (9, 'material', 'draft', 'M', '', 9)", "DELETE FROM feed_items WHERE id = 9"),
            "attachments of material versions: 1": (
                "INSERT INTO attachments (material_version_id, kind, url, position) VALUES (9, 'link', 'https://e.ru/', 0)",
                "DELETE FROM attachments WHERE material_version_id IS NOT NULL"),
            "links to materials: 1": ("INSERT INTO material_refs (slug, feed_item_id) VALUES ('cycles', 2)",
                                      "DELETE FROM material_refs"),
        }
        for expected, (change, undo) in cases.items():
            with self.subTest(expected):
                with engine.begin() as conn:
                    conn.execute(text(change))
                    before = self.feed_rows(conn), self.attachment_rows(conn)
                with self.assertRaises(RuntimeError) as caught:
                    with engine.begin() as conn:
                        self.alembic(conn, "downgrade", "32cd8723a2bc")
                self.assertIn(expected, str(caught.exception))
                self.assertIn("nothing was changed", str(caught.exception))
                with engine.begin() as conn:
                    self.assertEqual(conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                                     "b8e0cbcc4cda")
                    self.assertEqual((self.feed_rows(conn), self.attachment_rows(conn)), before)
                    conn.execute(text(undo))
        with self.assertRaises(RuntimeError) as caught:  # the material 9 itself
            with engine.begin() as conn:
                self.alembic(conn, "downgrade", "32cd8723a2bc")
        self.assertIn("materials: 1, library sections: 0", str(caught.exception))
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM material_versions"))
            conn.execute(text("DELETE FROM materials"))
            self.alembic(conn, "downgrade", "32cd8723a2bc")

    # -------------------------------------------------------------- f2e12ff59f1c: judgements.max_score, gave-up (D-090)
    BEFORE_SCORES = """
        INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
          (1, 'admin', '!', 'A', 'admin', true), (2, 'pupil', '!', 'P', 'student', true);
        INSERT INTO problems (id, slug, owner_id) VALUES (1, 'sum', 1), (2, 'quiz', 1), (3, 'words', 1), (4, 'none', 1);
        INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config) VALUES
          (1, 1, 1, 'ready', 'batch', '{}', '{}', '{}'),
          (2, 2, 1, 'ready', 'choice', '{}', '{}',
           '{"questions": [{"id": "a", "correct": ["x"], "points": 2.5}, {"id": "b", "correct": ["y"]}]}'),
          (3, 3, 1, 'ready', 'text_answer', '{}', '{}',
           '{"answers": [{"id": "c", "accept": ["1"]}, {"id": "d", "accept": ["2"], "points": 3}]}'),
          (4, 4, 1, 'ready', 'batch', '{}', '{}', '{}'),
          (5, 1, 2, 'ready', 'batch', '{}', '{}', '{}');
        INSERT INTO test_groups (version_id, position, name, points, scoring, visibility, show_in_statement, depends_on)
          VALUES (1, 0, 'g1', 30, 'sum', 'open', false, '[]'), (1, 1, 'g2', 30.5, 'sum', 'hidden', false, '[]'),
                 (5, 0, 'g1', 100, 'sum', 'open', false, '[]');
        INSERT INTO contests (id, slug, title, starts_at, access_mode, state, timing_mode, scoring_mode,
                              icpc_penalty_minutes, leaderboard, allow_upsolving, submit_cooldown_sec, tests_after_end,
                              plagiarism_enabled, description_md)
          VALUES (1, 'lesson', 'L', '2026-11-01 09:00+00', 'open', 'published', 'fixed', 'points', 20, 'hidden', true,
                  15, false, false, '');
        INSERT INTO contest_problems (id, contest_id, problem_id, problem_version_id, label, position, points) VALUES
          (1, 1, 1, 1, 'A', 0, 100), (2, 1, 2, 2, 'B', 1, NULL), (3, 1, 3, 3, 'C', 2, NULL), (4, 1, 4, 4, 'D', 3, NULL);
        INSERT INTO submissions (id, user_id, contest_problem_id, is_archive) VALUES
          (1, 2, 1, false), (2, 2, 2, false), (3, 2, 3, false), (4, 2, 4, false), (5, 2, 1, false), (6, 2, 1, false),
          (7, 2, 1, false), (8, 2, 1, false);
        INSERT INTO judgements (id, submission_id, problem_version_id, reason, status, is_current, priority, requires,
                                attempt, failures, verdict, score, error, finished_at) VALUES
          (1, 1, 1, 'initial', 'finished', true, 0, '["cpu"]', 1, 0, 'OK', 60.5, NULL, '2026-11-02 10:00+00'),
          (2, 2, 2, 'initial', 'finished', true, 0, '["cpu"]', 1, 0, 'PARTIAL', 1, NULL, '2026-11-02 10:00+00'),
          (3, 3, 3, 'initial', 'finished', true, 0, '["cpu"]', 1, 0, 'OK', 4, NULL, '2026-11-02 10:00+00'),
          (4, 4, 4, 'initial', 'finished', true, 0, '["cpu"]', 1, 0, 'OK', 0, NULL, '2026-11-02 10:00+00'),
          (5, 5, 1, 'initial', 'internal_error', true, 0, '["cpu"]', 3, 3, 'IE', 0,
           'the server gave up after 3 failed attempts, last reason: worker lost (no heartbeat)', '2026-11-02 11:00+00'),
          (6, 6, 1, 'initial', 'internal_error', true, 0, '["cpu"]', 3, 3, 'IE', 0,
           'worker: the server gave up after 3 (worker text)', '2026-11-02 11:00+00'),
          (7, 7, 1, 'initial', 'internal_error', true, 0, '["cpu"]', 3, 3, 'IE', 0, 'checker crashed',
           '2026-11-02 11:00+00'),
          (8, 8, 1, 'initial', 'queued', true, 0, '["cpu"]', 0, 0, NULL, 0, NULL, NULL),
          (9, 1, 5, 'rejudge', 'running', false, 10, '["cpu"]', 1, 0, NULL, 0, NULL, NULL)
    """
    SCORES_TRANSFERRED = {1: ("60.50", None, None), 2: ("3.50", None, None), 3: ("4.00", None, None),
                          4: ("0.00", None, None),
                          5: ("60.50", "2026-11-02 11:00", "3 failed attempts, last reason: worker lost (no heartbeat)"),
                          6: ("60.50", None, "worker: the server gave up after 3 (worker text)"),
                          7: ("60.50", None, "checker crashed"), 8: (None, None, None), 9: (None, None, None)}

    def judgement_rows(self, conn, upgraded=True):
        if not upgraded:
            return [tuple(r) for r in conn.execute(text("SELECT id, status::text, score, error FROM judgements "
                                                        "ORDER BY id"))]
        rows = conn.execute(text("SELECT id, max_score, to_char(server_gave_up_at AT TIME ZONE 'UTC', "
                                 "'YYYY-MM-DD HH24:MI'), error FROM judgements ORDER BY id"))
        return {r[0]: (None if r[1] is None else str(r[1]), r[2], r[3] if r[0] >= 5 else None) for r in rows}

    def test_scores_migration_fills_max_score_and_moves_the_mark_up_down_up(self):
        from sqlalchemy.orm import Session

        from app import queue as q
        from app import scores
        from app.models import Judgement
        _schema, engine = self.scratch_schema()
        with engine.begin() as conn:
            self.alembic(conn, "upgrade", "b8e0cbcc4cda")
            self.run_sql(conn, self.BEFORE_SCORES)
            before = self.judgement_rows(conn, upgraded=False)
            self.alembic(conn, "upgrade", "f2e12ff59f1c")
            self.assertEqual(self.judgement_rows(conn), self.SCORES_TRANSFERRED)
            self.alembic(conn, "upgrade", "head")  # models.py is the head, not this revision
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
            self.assertEqual(diff, [], "alembic check after the upgrade: models.py and the migration differ")
            # the SQL copy of the rule == scores.version_max_score, for every kind of version
            with Session(bind=conn) as db:
                for version_id, expect in ((1, "60.50"), (2, "3.50"), (3, "4.00"), (4, "0.00"), (5, "100.00")):
                    with self.subTest(version=version_id):
                        self.assertEqual(str(scores.version_max_score(db, version_id)), expect)
                self.assertEqual([q.server_gave_up(db.get(Judgement, i)) for i in (5, 6, 7)], [True, False, False])
            self.assertIsNotNone(conn.execute(text("SELECT to_regclass('ix_participations_user')")).scalar_one())
            for bad in ("UPDATE judgements SET max_score = -1 WHERE id = 1",
                        "UPDATE judgements SET server_gave_up_at = now() WHERE id = 1"):  # finished, not IE
                with self.subTest(bad=bad), self.assertRaises(Exception), conn.begin_nested():
                    conn.execute(text(bad))
            # after the upgrade a worker may write an error that looks like the old mark: the downgrade keeps it apart
            conn.execute(text("UPDATE judgements SET error = 'the server gave up after (worker)' WHERE id = 7"))

            self.alembic(conn, "downgrade", "b8e0cbcc4cda")
            after = dict((r[0], r) for r in self.judgement_rows(conn, upgraded=False))
            self.assertEqual([r for r in before if r[0] != 7], [after[r[0]] for r in before if r[0] != 7])
            self.assertEqual(after[7][3], "worker: the server gave up after (worker)")
            self.assertIsNone(conn.execute(text("SELECT to_regclass('ix_participations_user')")).scalar_one())
            self.assertEqual(conn.execute(text("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                                               "'judgements' AND column_name IN ('max_score', 'server_gave_up_at') "
                                               "AND table_schema = current_schema()")).scalar_one(), 0)

            conn.execute(text("UPDATE judgements SET error = 'checker crashed' WHERE id = 7"))
            self.alembic(conn, "upgrade", "head")
            self.assertEqual(self.judgement_rows(conn), self.SCORES_TRANSFERRED)
