"""Data and catalog checks for deploy/dev/check-reset-db.sh (D-073): does recreating a database the way
reset-production.sh does it pass with the data that refuses `alembic downgrade base`, and does the result look like
a fresh installation?

    DATABASE_URL=... python tools/reset_check.py seed       # rows of every kind that used to block the reset
    DATABASE_URL=... FRESH_DATABASE_URL=... python tools/reset_check.py compare
        # the reset database against a freshly created one: owner and privileges of the database and of the schema
        # public, table owners, the same rows as after the migrations (none of the old data), models == migrations; exit 1 and the differences otherwise

URLs come from the environment, never from the command line (it is visible in ps). Dev only: run by the check
script against throw-away databases; tests/pg/test_reset_check_pg.py runs seed and snapshot in a scratch schema.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from sqlalchemy import create_engine, text  # noqa: E402

# One of each thing a teacher and students leave behind, including what the downgrades of 4a.3 (groups, contest
# deadlines), 4a.4 (version checks), 4v.1 (the feed), 4v.2 (comments) and 4v.3 (files, links, attachments) refuse.
SEED_SQL = """
    INSERT INTO users (id, login, password_hash, display_name, role, is_active) VALUES
      (1, 'admin', '!', 'A', 'admin', true), (2, 'pupil', '!', 'P', 'student', true);
    INSERT INTO sessions (token_hash, user_id, state, csrf_token, expires_at)
      VALUES ('s1', 1, 'active', 'c', now() + interval '1 hour');
    INSERT INTO audit_log (actor_user_id, action, object_type, object_id, details)
      VALUES (1, 'auth.login', 'user', '1', '{}');
    INSERT INTO worker_tokens (id, name, token_hash, capabilities) VALUES (1, 'main', 'h1', '["cpu", "problem_check"]');
    INSERT INTO groups (id, name, created_by) VALUES (1, '7Б', 1);
    INSERT INTO group_members (group_id, user_id, added_by) VALUES (1, 2, 1);
    INSERT INTO problems (id, slug, owner_id) VALUES (1, 'sum', 1);
    INSERT INTO problem_versions (id, problem_id, version, status, type, titles, statements, config)
      VALUES (1, 1, 1, 'ready', 'batch', '{}', '{}', '{}');
    INSERT INTO version_checks (problem_version_id, reason, status, attempt, failures)
      VALUES (1, 'import', 'queued', 0, 0);
    INSERT INTO contests (id, slug, title, starts_at, access_mode, state, timing_mode, scoring_mode,
                          icpc_penalty_minutes, leaderboard, allow_upsolving, submit_cooldown_sec, tests_after_end,
                          plagiarism_enabled, description_md, created_by)
      VALUES (1, 'lesson', 'L', '2026-11-01 09:00+00', 'groups', 'published', 'fixed', 'points', 20, 'hidden', true,
              15, false, false, '', 1);
    INSERT INTO contest_groups (contest_id, group_id, added_by) VALUES (1, 1, 1);
    INSERT INTO contest_problems (id, contest_id, problem_id, problem_version_id, label, position, due_at)
      VALUES (1, 1, 1, 1, 'A', 0, '2026-11-10 10:00+00');
    INSERT INTO deadline_extensions (contest_problem_id, user_id, due_at, reason)
      VALUES (1, 2, '2026-11-11 10:00+00', 'болел');
    INSERT INTO participations (contest_id, user_id, joined_via) VALUES (1, 2, 'group');
    INSERT INTO submissions (id, user_id, contest_problem_id, is_archive) VALUES (1, 2, 1, false);
    INSERT INTO feed_items (id, kind, state, title, body_md, contest_id, published_at)
      VALUES (1, 'contest', 'published', 'L', '', 1, '2026-11-05 10:00+00');
    INSERT INTO feed_items (id, kind, state, title, body_md, grading, max_points, answer_kind, grades_release,
                            due_at, published_at, comments_enabled, created_by)
      VALUES (2, 'assignment', 'published', 'H', '', 'points', 5, 'text', 'manual', '2026-11-10 10:00+00',
              '2026-11-05 10:00+00', true, 1);
    INSERT INTO feed_item_audience (item_id, group_id) VALUES (1, 1), (2, 1);
    INSERT INTO deadline_extensions (feed_item_id, user_id, due_at) VALUES (2, 2, '2026-11-11 10:00+00');
    INSERT INTO blobs (sha256, size_bytes) VALUES ('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 3);
    INSERT INTO assignment_works (id, item_id, user_id, status, status_at) VALUES (1, 2, 2, 'graded', now());
    INSERT INTO assignment_submissions (id, work_id) VALUES (1, 1);
    INSERT INTO assignment_submission_files (submission_id, kind, path, blob_sha256, size, position)
      VALUES (1, 'text', 'answer.md', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 3, 0);
    INSERT INTO assignment_reviews (work_id, action, score, reviewer_id) VALUES (1, 'graded', 4, 1);
    INSERT INTO comment_threads (id, item_id, student_id) VALUES (1, 2, 2), (2, 2, NULL);
    INSERT INTO comments (id, thread_id, author_id, body) VALUES (1, 1, 2, 'вопрос'), (2, 2, 1, 'объявление');
    INSERT INTO thread_reads (user_id, thread_id, last_read_comment_id) VALUES (1, 1, 1);
    INSERT INTO blobs (sha256, size_bytes) VALUES ('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 5);
    INSERT INTO feed_items (id, kind, state, title, body_md, grading, answer_kind, grades_release, published_at,
                            created_by)
      VALUES (3, 'assignment', 'published', 'Проект', '', 'pass_fail', 'files', 'immediate', '2026-11-11 10:00+00', 1);
    INSERT INTO feed_item_audience (item_id, user_id) VALUES (3, 2);
    INSERT INTO attachments (item_id, kind, name, blob_sha256, size, position, created_by)
      VALUES (3, 'file', 'схема.pdf', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 5, 0, 1);
    INSERT INTO attachments (item_id, kind, url, title, position, created_by)
      VALUES (3, 'link', 'https://scratch.mit.edu/projects/1', 'Пример', 1, 1);
    INSERT INTO assignment_works (id, item_id, user_id, status, status_at) VALUES (2, 3, 2, 'submitted', now());
    INSERT INTO assignment_submissions (id, work_id) VALUES (2, 2);
    INSERT INTO assignment_submission_files (submission_id, kind, path, blob_sha256, size, position)
      VALUES (2, 'file', 'project.sb3', 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 5, 0);
    INSERT INTO assignment_submission_links (submission_id, url, position)
      VALUES (2, 'https://scratch.mit.edu/projects/2', 0)
"""


def seed(conn) -> None:
    for statement in SEED_SQL.split(";"):
        if statement.strip():
            conn.execute(text(statement))


def snapshot(conn, schema: str = "public") -> dict:
    """What a fresh installation is compared by: the owner and the privileges of the database and of the schema,
    the owner of every table, the rows (all tables but alembic_version) and models.py against the schema."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.models import Base

    database = conn.execute(text("SELECT pg_get_userbyid(datdba), datacl::text FROM pg_database "
                                 "WHERE datname = current_database()")).one()
    namespace = conn.execute(text("SELECT pg_get_userbyid(nspowner), nspacl::text FROM pg_namespace "
                                  "WHERE nspname = :s"), {"s": schema}).one()
    tables = dict(conn.execute(text("SELECT tablename, tableowner FROM pg_tables WHERE schemaname = :s"),
                               {"s": schema}).all())
    rows = {t: conn.execute(text(f'SELECT count(*) FROM "{schema}"."{t}"')).scalar_one()
            for t in sorted(tables) if t != "alembic_version"}
    diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    return {"database": {"owner": database[0], "acl": database[1]},
            "schema": {"owner": namespace[0], "acl": namespace[1]},
            "tables": tables, "rows": {t: n for t, n in rows.items() if n},
            "migrations_vs_models": [str(d) for d in diff]}


def differences(reset: dict, fresh: dict) -> list:
    found = [f"{key}: reset {reset[key]!r}, fresh {fresh[key]!r}"
             for key in ("database", "schema", "tables") if reset[key] != fresh[key]]
    if reset["rows"] != fresh["rows"]:  # the migrations fill a few tables themselves (languages)
        found.append(f"rows: reset {reset['rows']}, fresh {fresh['rows']}")
    for name, snap in (("reset", reset), ("fresh", fresh)):
        if snap["migrations_vs_models"]:
            found.append(f"{name}: models.py and the migrated schema differ: {snap['migrations_vs_models']}")
    return found


def main(argv) -> int:
    command = argv[1] if len(argv) == 2 else None
    if command == "seed":
        with create_engine(os.environ["DATABASE_URL"]).begin() as conn:
            seed(conn)
        print("seeded")
        return 0
    if command == "compare":
        with create_engine(os.environ["DATABASE_URL"]).connect() as conn:
            reset = snapshot(conn)
        with create_engine(os.environ["FRESH_DATABASE_URL"]).connect() as conn:
            fresh = snapshot(conn)
        print(json.dumps({"reset": reset, "fresh": fresh}, ensure_ascii=False, indent=1, sort_keys=True))
        found = differences(reset, fresh)
        for line in found:
            print("DIFFERENT:", line, file=sys.stderr)
        print("same as a fresh installation" if not found else f"{len(found)} differences", file=sys.stderr)
        return 1 if found else 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
