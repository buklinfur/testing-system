"""Shared setup for the PostgreSQL tests (they are SKIPPED unless JUDGE_TEST_DATABASE_URL is set).

    createdb judge_test
    export JUDGE_TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5432/judge_test
    python -m unittest discover -s tests/pg -t . -v

Every test class works in its own throw-away schema, dropped afterwards; the database itself is not touched.

A run that is interrupted (Ctrl-C, a killed process, a hang) cannot drop its schema. Schema names carry their creation
time (t_<unixtime>_<hex>, m_<unixtime>_<hex> for the scratch schemas of the migration tests), and the first test
class of every run drops the ones older than STALE_SCHEMA_AGE_S (6 h): a run that is still going is never touched,
schemas of any other name never. Schemas of the older format without a time are left alone: docs/DEPLOY_WSL2.md §9.
"""
import os
import re
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.settings import QueueSettings  # noqa: E402  (pure module, no database needed)
from protocol.languages import SYSTEM_LANGUAGES  # noqa: E402

URL = os.environ.get("JUDGE_TEST_DATABASE_URL")

try:
    import sqlalchemy  # noqa: F401
    HAVE_SA = True
except ImportError:
    HAVE_SA = False

requires_pg = unittest.skipUnless(URL and HAVE_SA, "set JUDGE_TEST_DATABASE_URL and install sqlalchemy+psycopg")

if URL and HAVE_SA:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from app import queue as q
    from app.models import (
        AccessMode, Base, Blob, Contest, ContestProblem, ContestState, Language, Problem, ProblemType, ProblemVersion, Role,
        Submission, TestCase, TestGroup, User, VersionStatus,
    )


STALE_SCHEMA_AGE_S = 6 * 3600
SCHEMA_NAME = re.compile(r"^(t|m)_([0-9]{10})_[0-9a-f]{10}$")  # the only names the sweep may drop
_swept = False  # once per process


def schema_name(prefix: str = "t", now: float = None) -> str:
    """A throw-away schema name with its creation time: <prefix>_<unixtime>_<10 hex>."""
    return f"{prefix}_{int(time.time() if now is None else now)}_{uuid.uuid4().hex[:10]}"


def sweep_stale_schemas(conn, now: float = None, max_age_s: float = STALE_SCHEMA_AGE_S) -> list:
    """Drop the throw-away schemas of this format older than max_age_s; returns their names. Other names - public,
    the older format without a time, anything else - are never touched."""
    now = time.time() if now is None else now
    dropped = []
    for name in conn.execute(text("SELECT schema_name FROM information_schema.schemata ORDER BY 1")).scalars():
        m = SCHEMA_NAME.match(name)
        if m and now - int(m.group(2)) > max_age_s:
            conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
            dropped.append(name)
    return dropped


class PgCase(unittest.TestCase):
    settings = QueueSettings(heartbeat_timeout_s=30, max_attempts=3, max_judging_s=1800, recent_window_s=300)

    @classmethod
    def setUpClass(cls):
        if not (URL and HAVE_SA):
            raise unittest.SkipTest("PostgreSQL tests are not configured")
        global _swept
        cls.schema = schema_name("t")
        admin = create_engine(URL)
        with admin.begin() as conn:
            if not _swept:  # what interrupted runs left behind (see the module docstring)
                dropped = sweep_stale_schemas(conn)
                if dropped:
                    print(f"pgbase: dropped {len(dropped)} stale test schemas: {', '.join(dropped)}", file=sys.stderr)
                _swept = True
            conn.execute(text(f'CREATE SCHEMA "{cls.schema}"'))
        admin.dispose()
        cls.engine = create_engine(URL, connect_args={"options": f"-csearch_path={cls.schema}"}, pool_size=20, max_overflow=20)
        Base.metadata.create_all(cls.engine)  # first real execution of models.py
        cls.Session = sessionmaker(cls.engine, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        admin = create_engine(URL)
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{cls.schema}" CASCADE'))
        admin.dispose()

    def setUp(self):
        with self.engine.begin() as conn:  # clean tables between tests, keep the schema
            names = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        self.s = self.Session()
        self.addCleanup(self.s.close)
        self.seed = self.make_seed()

    # ---------------------------------------------------------------- seed helpers
    def make_seed(self):
        s = self.s
        admin = User(login="admin", password_hash="x", display_name="A", role=Role.admin)
        s.add_all([admin] + [Language(key=k, name=n, config={}) for k, n in SYSTEM_LANGUAGES])  # as the migration
        s.flush()
        s.add_all([Blob(sha256="a" * 64, size_bytes=1), Blob(sha256="b" * 64, size_bytes=1), Blob(sha256="c" * 64, size_bytes=1)])
        problem = Problem(slug="sum", owner_id=admin.id)
        s.add(problem)
        s.flush()
        version = ProblemVersion(problem_id=problem.id, version=1, status=VersionStatus.ready, type=ProblemType.batch,
                                 titles={"ru": "S"}, statements={"ru": ""}, config={"limits": {"time_ms": 1000}})
        s.add(version)
        s.flush()
        group = TestGroup(version_id=version.id, position=0, name="main", points=100)
        s.add(group)
        s.flush()
        tests = [TestCase(group_id=group.id, position=i, name=f"t{i}", input_sha256="a" * 64, answer_sha256="b" * 64) for i in range(2)]
        s.add_all(tests)
        contest = Contest(slug="c", title="C", starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                          state=ContestState.published, access_mode=AccessMode.open, created_by=admin.id)
        s.add(contest)
        s.flush()
        cp = ContestProblem(contest_id=contest.id, problem_id=problem.id, problem_version_id=version.id, label="A",
                            position=0)
        s.add(cp)
        s.commit()
        return {"admin": admin, "version": version, "cp": cp, "tests": tests}

    def student(self, n: int):
        u = User(login=f"student{n}", password_hash="x", display_name=f"S{n}", role=Role.student)
        self.s.add(u)
        self.s.flush()
        return u

    def submission(self, user, enqueue=True, **kw):
        sub = Submission(user_id=user.id, contest_problem_id=self.seed["cp"].id, language_key="python", source_sha256="c" * 64)
        self.s.add(sub)
        self.s.flush()
        j = q.enqueue(self.s, sub, **kw) if enqueue else None
        self.s.flush()
        return sub, j

    def token_and_worker(self, name="w0", caps=("cpu",), token_caps=("cpu", "gpu")):
        tok_plain = q.create_worker_token(self.s, f"tok-{name}", list(token_caps))
        token = q.authenticate(self.s, tok_plain)
        worker = q.register_worker(self.s, token, name, list(caps), {}, self.settings)
        self.s.commit()
        return token, worker

    def finish_checks(self, outcome="ready", reason=None, report=None):
        """Bring every queued check of a problem version to an end through the real code path (claim_check, then
        finish, as the worker API does), the way a worker would: the versions become ready (or failed). Commits in a
        session of its own. Returns the number of checks ended."""
        from app import problem_checks as checks
        from app.models import Worker
        n, qs = 0, PgCase.settings  # not self.settings: API test classes use that name for the app's Settings
        with self.Session() as s:
            worker = s.execute(text("SELECT id FROM workers WHERE name = 'test-checker'")).scalar()
            if worker is None:
                token = q.authenticate(s, q.create_worker_token(s, "tok-test-checker", ["cpu", "problem_check"]))
                worker = q.register_worker(s, token, "test-checker", ["cpu", "problem_check"], {}, qs).id
                s.commit()
            worker = s.get(Worker, worker)
            while True:
                check = checks.claim_check(s, worker, qs)  # alone here: no claim lock needed
                if check is None:
                    break
                body = report or {"outcome": outcome,
                                  "reason": reason or (None if outcome == "ready" else "solution_mismatch"),
                                  "worker": "test-checker", "programs": [], "solutions": []}
                checks.finish(s, worker, check.id, {"attempt": check.attempt, "outcome": body["outcome"],
                                                    "report": body}, qs)
                s.commit()
                n += 1
        self.s.expire_all()
        return n

    def ok_payload(self, j, tests=None):
        tests = tests if tests is not None else [{"test_case_id": t.id, "verdict": "OK", "time_ms": 5, "memory_kb": 100,
                                                  "visible_to_student": i == 0} for i, t in enumerate(self.seed["tests"])]
        return {"attempt": j.attempt, "verdict": "OK", "score": "100.00", "max_score": "100.00",
                "summary": {"groups": []}, "tests": tests}
