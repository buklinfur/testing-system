"""Shared setup for the PostgreSQL tests (they are SKIPPED unless JUDGE_TEST_DATABASE_URL is set).

    createdb judge_test
    export JUDGE_TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:5432/judge_test
    python -m unittest discover -s tests/pg -t . -v

Every test class works in its own throw-away schema, dropped afterwards; the database itself is not touched.
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.settings import QueueSettings  # noqa: E402  (pure module, no database needed)

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
        Base, Blob, Contest, ContestMode, ContestProblem, Language, Problem, ProblemType, ProblemVersion, Role,
        Submission, TestCase, TestGroup, User, VersionStatus,
    )


class PgCase(unittest.TestCase):
    settings = QueueSettings(heartbeat_timeout_s=30, max_attempts=3, max_judging_s=1800, recent_window_s=300)

    @classmethod
    def setUpClass(cls):
        if not (URL and HAVE_SA):
            raise unittest.SkipTest("PostgreSQL tests are not configured")
        cls.schema = "t_" + uuid.uuid4().hex[:10]
        admin = create_engine(URL)
        with admin.begin() as conn:
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
        teacher = User(login="teacher", password_hash="x", display_name="T", role=Role.teacher)
        s.add_all([teacher, Language(key="python", name="Python", config={})])
        s.flush()
        s.add_all([Blob(sha256="a" * 64, size_bytes=1), Blob(sha256="b" * 64, size_bytes=1), Blob(sha256="c" * 64, size_bytes=1)])
        problem = Problem(slug="sum", owner_id=teacher.id)
        s.add(problem)
        s.flush()
        version = ProblemVersion(problem_id=problem.id, version=1, status=VersionStatus.published, type=ProblemType.batch,
                                 titles={"ru": "S"}, statements={"ru": ""}, config={"limits": {"time_ms": 1000}})
        s.add(version)
        s.flush()
        group = TestGroup(version_id=version.id, position=0, name="main", points=100)
        s.add(group)
        s.flush()
        tests = [TestCase(group_id=group.id, position=i, name=f"t{i}", input_sha256="a" * 64, answer_sha256="b" * 64) for i in range(2)]
        s.add_all(tests)
        contest = Contest(slug="c", title="C", mode=ContestMode.practice, created_by=teacher.id)
        s.add(contest)
        s.flush()
        cp = ContestProblem(contest_id=contest.id, problem_version_id=version.id, label="A", position=0)
        s.add(cp)
        s.commit()
        return {"teacher": teacher, "version": version, "cp": cp, "tests": tests}

    def student(self, n: int):
        u = User(login=f"s{n}", password_hash="x", display_name=f"S{n}", role=Role.student)
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

    def ok_payload(self, j, tests=None):
        tests = tests if tests is not None else [{"test_case_id": t.id, "verdict": "OK", "time_ms": 5, "memory_kb": 100,
                                                  "visible_to_student": i == 0} for i, t in enumerate(self.seed["tests"])]
        return {"attempt": j.attempt, "verdict": "OK", "score": "100.00", "max_score": "100.00",
                "summary": {"groups": []}, "tests": tests}
