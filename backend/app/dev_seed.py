"""Development only: put submissions into the queue without the public API (that arrives in module 4b).

    JUDGE_DEV=1 python -m app.dev_seed [--package DIR] [--source FILE] [--language python|c|cpp]
                                       [--archive] [--entry main.py] [--user N] [--count 1]

* `--package DIR`: a problem package (problems-examples/...), imported through the importer of session 4a.2
  (app.problems.importer: the same checks and the same identity as `judgectl problem import`): the same
  content again is "already exists as vN", changed content becomes vN+1. Without `--package` a built-in
  batch problem "dev-sum" (a + b, two hidden tests) is written to a temporary folder and imported the same way.
* Each problem gets one entry in the practice contest "dev" (open, no end), pinned to the newest version seeded;
  an entry pinned to an older version is hidden (its submissions stay).
* `--contest SLUG --label L`: instead of the "dev" contest, submit to problem L of a real (published) contest the way
  a student will through the API of 4b: the student joins (with the contest's invite code), presses "start" in a
  personal contest, and every submission goes through access.can_submit (window, upsolving, cooldown, limit,
  language). A refusal stops dev_seed with its reason and exit code 1. Not with --package.
* `--user N`: the submissions belong to student `dev-student-N` (created on demand), default 1.
* `--source` / `--archive` / `--entry`: the solution (a zip for `--archive`); without `--source` a correct
  Python a + b solution is sent. Code-less problems (choice, text_answer) are not supported here.

Prints a JSON list of {submission_id, judgement_id, problem_version_id, contest_problem_id, import, counted}
(import: "created" or "exists", null with --contest; counted: false for upsolving). Files go to JUDGE_BLOB_DIR. Refuses to run without JUDGE_DEV=1.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from protocol.languages import SYSTEM_LANGUAGES
from protocol.problem_package import PackageError

from . import queue as q
from .auth import audit
from .blobstore import BlobStore
from .contests import access, service
from .db import session_scope
from .models import Contest, ContestProblem, ContestState, JoinPolicy, Language, ProblemType, ProblemVersion, Role, \
    Submission, User
from .problems.importer import ImportResult, import_package, store_blob
from .settings import Settings

CONTEST_SLUG = "dev"
DEFAULT_SOURCE = b"a, b = map(int, input().split())\nprint(a + b)\n"


def _get_or_create(session: Session, model, where: dict, **values):
    obj = session.execute(select(model).filter_by(**where)).scalar_one_or_none()
    if obj is None:
        obj = model(**where, **values)
        session.add(obj)
        session.flush()
    return obj


def _student(session: Session, n: int) -> User:
    return _get_or_create(session, User, {"login": f"dev-student-{n}"}, password_hash="!",
                          display_name=f"Dev Student {n}", role=Role.student)


BUILTIN_FILES = {
    "problem.yaml": """format_version: 1
slug: dev-sum
title: {ru: "Сумма (dev)"}
type: batch
io: {mode: stdio}
limits: {time_ms: 1000, memory_mb: 256}
languages: [python, c, cpp]
checker: {type: tokens}
feedback: first_fail
test_groups:
  - {name: main, tests: "tests/*.in", points: 100}
""",
    "statement/ru.md": "Выведите a + b.\n",
    "tests/01.in": "1 2\n", "tests/01.out": "3\n",
    "tests/02.in": "-5 1000000000\n", "tests/02.out": "999999995\n",
}


def import_dir(session: Session, blobs: BlobStore, root: Path, admin: User) -> ImportResult:
    """The package folder through the importer (owner: dev-teacher)."""
    return import_package(session, blobs, Settings.from_env().imports, audit.cli_actor(), datetime.now(timezone.utc),
                          directory=root, source_name=f"dev_seed:{root.name}", owner_id=admin.id)


def import_builtin(session: Session, blobs: BlobStore, admin: User) -> ImportResult:
    with tempfile.TemporaryDirectory(prefix="dev-sum-") as tmp:
        for rel, body in BUILTIN_FILES.items():
            (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(tmp) / rel).write_text(body, encoding="utf-8")
        return import_dir(session, blobs, Path(tmp), admin)


def ensure_contest_problem(session: Session, version: ProblemVersion, admin: User) -> ContestProblem:
    """The entry of this problem in the "dev" contest, pinned to this version. If the visible entry is pinned to
    another version, it is hidden (its submissions keep their version) and a new one takes its place."""
    contest = _get_or_create(session, Contest, {"slug": CONTEST_SLUG}, title="Dev contest",
                             starts_at=datetime.now(timezone.utc), state=ContestState.published,
                             join_policy=JoinPolicy.open, created_by=admin.id)
    cp = session.execute(select(ContestProblem).where(ContestProblem.contest_id == contest.id,
                                                      ContestProblem.problem_id == version.problem_id,
                                                      ContestProblem.hidden.is_(False))).scalar_one_or_none()
    if cp is not None and cp.problem_version_id == version.id:
        return cp
    if cp is not None:
        cp.hidden = True
        session.flush()
    count = session.execute(select(func.count()).select_from(ContestProblem)
                            .where(ContestProblem.contest_id == contest.id)).scalar_one()
    cp = ContestProblem(contest_id=contest.id, problem_id=version.problem_id, problem_version_id=version.id,
                        label=f"P{count + 1}", position=count)
    session.add(cp)
    session.flush()
    return cp


def submit_to_contest(session: Session, blobs: BlobStore, args, student: User, source: bytes) -> list:
    """Join, start (personal timing) and submit as a student would; stops at the first refusal of the rules."""
    contest = service.get_contest(session, args.contest)
    cp = service.visible_row(session, contest, args.label)
    now = datetime.now(timezone.utc)
    part = service.participation_of(session, contest, student)
    if part is None:
        part = service.join(session, student, contest, now, contest.invite_code)
        print(f"dev_seed: {student.login} joined {contest.slug}", file=sys.stderr)
    if access.can_start(student, contest, part, now):
        service.start(session, student, contest, now)
        print(f"dev_seed: {student.login} pressed start", file=sys.stderr)
    source_sha = store_blob(session, blobs, source)
    out = []
    for _ in range(args.count):
        now = datetime.now(timezone.utc)
        last = session.execute(select(func.max(Submission.created_at)).where(
            Submission.user_id == student.id, Submission.contest_problem_id == cp.id)).scalar()
        window = access.contest_window_for(contest, part)
        counted = service.counted_submissions(session, cp.id, student.id, window.start, window.end) if window else 0
        decision = access.can_submit(student, contest, cp, part, now, language=args.language,
                                     last_submission_at=last, counted_submissions=counted)
        if not decision:
            wait = f" (retry in {decision.retry_after_s} s)" if decision.retry_after_s else ""
            print(f"dev_seed: refused by the access rules: {decision.reason}{wait}", file=sys.stderr)
            break
        sub = Submission(user_id=student.id, contest_problem_id=cp.id, language_key=args.language,
                         source_sha256=source_sha, is_archive=args.archive, entry_file=args.entry, created_at=now)
        session.add(sub)
        session.flush()
        j = q.enqueue(session, sub)
        out.append({"submission_id": sub.id, "judgement_id": j.id, "problem_version_id": cp.problem_version_id,
                    "contest_problem_id": cp.id, "import": None, "counted": decision.counted})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dev_seed")
    parser.add_argument("--package", type=Path, help="problem package directory (default: built-in dev-sum)")
    parser.add_argument("--source", type=Path, help="solution file (default: a correct Python a + b)")
    parser.add_argument("--language", default="python", choices=["python", "c", "cpp"])
    parser.add_argument("--archive", action="store_true", help="--source is a zip with several files")
    parser.add_argument("--entry", help="entry file inside the archive (interpreted languages)")
    parser.add_argument("--user", type=int, default=1, help="student number: dev-student-N")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--contest", help="slug of a published contest (with --label): through the access rules")
    parser.add_argument("--label", help="the problem's label in --contest")
    args = parser.parse_args(argv)
    if (args.contest is None) != (args.label is None):
        parser.error("--contest and --label go together")
    if args.contest and args.package:
        parser.error("--contest submits to a pinned version: not with --package")
    if os.environ.get("JUDGE_DEV") != "1":
        print("dev_seed writes test data: set JUDGE_DEV=1", file=sys.stderr)
        return 2
    if args.archive and not args.source:
        parser.error("--archive needs --source")
    blobs = BlobStore(Settings.from_env().blob_dir)
    source = args.source.read_bytes() if args.source else DEFAULT_SOURCE
    out = []
    with session_scope() as session:
        for key, name in SYSTEM_LANGUAGES:
            _get_or_create(session, Language, {"key": key}, name=name, config={})
        # the login predates the admin role; kept so that existing dev databases reuse the same row
        admin = _get_or_create(session, User, {"login": "dev-teacher"}, password_hash="!",
                               display_name="Dev Teacher", role=Role.admin)
        if args.contest:
            student = _student(session, args.user)
            try:
                out = submit_to_contest(session, blobs, args, student, source)
            except service.ContestError as exc:
                print(f"dev_seed: {exc}", file=sys.stderr)
                return 1
            print(json.dumps(out))
            return 0 if len(out) == args.count else 1
        try:
            result = import_dir(session, blobs, args.package, admin) if args.package else import_builtin(session, blobs, admin)
        except PackageError as exc:
            print(f"dev_seed: {exc}", file=sys.stderr)
            return 1
        print(f"dev_seed: {result.describe()}", file=sys.stderr)
        version = result.version
        if version.type in (ProblemType.choice, ProblemType.text_answer):
            print(f"dev_seed: {version.type.value} problems take answers, not code: not supported", file=sys.stderr)
            return 1
        cp = ensure_contest_problem(session, version, admin)
        student = _student(session, args.user)
        source_sha = store_blob(session, blobs, source)
        for _ in range(args.count):
            sub = Submission(user_id=student.id, contest_problem_id=cp.id, language_key=args.language,
                             source_sha256=source_sha, is_archive=args.archive, entry_file=args.entry)
            session.add(sub)
            session.flush()
            j = q.enqueue(session, sub)
            out.append({"submission_id": sub.id, "judgement_id": j.id, "problem_version_id": version.id,
                        "contest_problem_id": cp.id, "import": result.kind, "counted": True})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
