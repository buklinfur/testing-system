"""Development only: put a submission into the queue without the public API (that arrives in module 4).

    JUDGE_DEV=1 python -m app.dev_seed [--source sol.py] [--language python] [--count 1]

Creates once (by fixed slugs/logins): a teacher and a student, a published batch problem "dev-sum"
(a + b, two hidden tests, tokens checker), a practice contest "dev". Every run then adds `--count` new
submissions of the student and enqueues them; prints their ids as JSON. Files go to JUDGE_BLOB_DIR.
Refuses to run without JUDGE_DEV=1, so it is never pointed at the production database by accident.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import queue as q
from .blobstore import BlobStore
from .db import session_scope
from .models import (
    Blob, Contest, ContestMode, ContestProblem, Language, Problem, ProblemType, ProblemVersion, Role, Scoring,
    Submission, TestCase, TestGroup, User, VersionStatus, Visibility,
)
from .settings import Settings

PROBLEM_SLUG, CONTEST_SLUG = "dev-sum", "dev"
TESTS = [("01", b"1 2\n", b"3\n"), ("02", b"-5 1000000000\n", b"999999995\n")]
DEFAULT_SOURCE = b"a, b = map(int, input().split())\nprint(a + b)\n"
# problem.yaml of the dev problem, normalized and without test_groups (see docs/WORKER_PROTOCOL.md)
CONFIG = {
    "format_version": 1, "slug": PROBLEM_SLUG, "type": "batch", "io": {"mode": "stdio"},
    "limits": {"time_ms": 1000, "memory_mb": 256}, "languages": ["python", "c", "cpp"],
    "checker": {"type": "tokens"}, "feedback": "first_fail",
}


def _blob(session: Session, blobs: BlobStore, data: bytes) -> str:
    sha = blobs.put(data)
    if session.get(Blob, sha) is None:
        session.add(Blob(sha256=sha, size_bytes=len(data)))
    return sha


def _get_or_create(session: Session, model, where: dict, **values):
    obj = session.execute(select(model).filter_by(**where)).scalar_one_or_none()
    if obj is None:
        obj = model(**where, **values)
        session.add(obj)
        session.flush()
    return obj


def ensure_fixtures(session: Session, blobs: BlobStore) -> tuple:
    """(student, contest_problem); creates them on the first run."""
    teacher = _get_or_create(session, User, {"login": "dev-teacher"}, password_hash="!", display_name="Dev Teacher",
                             role=Role.teacher)
    student = _get_or_create(session, User, {"login": "dev-student"}, password_hash="!", display_name="Dev Student",
                             role=Role.student)
    for key, name in (("python", "Python 3"), ("c", "C"), ("cpp", "C++")):
        _get_or_create(session, Language, {"key": key}, name=name, config={})
    problem = _get_or_create(session, Problem, {"slug": PROBLEM_SLUG}, owner_id=teacher.id)
    version = session.execute(select(ProblemVersion).filter_by(problem_id=problem.id, version=1)).scalar_one_or_none()
    if version is None:
        version = ProblemVersion(problem_id=problem.id, version=1, status=VersionStatus.published, type=ProblemType.batch,
                                 titles={"ru": "Сумма (dev)"}, statements={"ru": "Выведите a + b."}, config=CONFIG,
                                 published_at=datetime.now(timezone.utc))
        session.add(version)
        session.flush()
        group = TestGroup(version_id=version.id, position=0, name="main", points=100, scoring=Scoring.sum,
                          visibility=Visibility.hidden)
        session.add(group)
        session.flush()
        for position, (name, inp, ans) in enumerate(TESTS):
            session.add(TestCase(group_id=group.id, position=position, name=name,
                                 input_sha256=_blob(session, blobs, inp), answer_sha256=_blob(session, blobs, ans)))
    contest = _get_or_create(session, Contest, {"slug": CONTEST_SLUG}, title="Dev contest", mode=ContestMode.practice,
                             created_by=teacher.id)
    cp = _get_or_create(session, ContestProblem, {"contest_id": contest.id, "label": "A"},
                        problem_version_id=version.id, position=0)
    return student, cp


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dev_seed")
    parser.add_argument("--source", type=Path, help="solution file (default: a correct Python solution)")
    parser.add_argument("--language", default="python", choices=["python", "c", "cpp"])
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args(argv)
    if os.environ.get("JUDGE_DEV") != "1":
        print("dev_seed writes test data: set JUDGE_DEV=1 (never on the production database)", file=sys.stderr)
        return 2
    blobs = BlobStore(Settings.from_env().blob_dir)
    source = args.source.read_bytes() if args.source else DEFAULT_SOURCE
    out = []
    with session_scope() as session:
        student, cp = ensure_fixtures(session, blobs)
        for _ in range(args.count):
            sub = Submission(user_id=student.id, contest_problem_id=cp.id, language_key=args.language,
                             source_sha256=_blob(session, blobs, source))
            session.add(sub)
            session.flush()
            j = q.enqueue(session, sub)
            out.append({"submission_id": sub.id, "judgement_id": j.id, "problem_version_id": j.problem_version_id})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
