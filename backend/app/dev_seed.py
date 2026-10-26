"""Development only: put submissions into the queue without the public API (that arrives in module 4).

    JUDGE_DEV=1 python -m app.dev_seed [--package DIR] [--source FILE] [--language python|c|cpp]
                                       [--archive] [--entry main.py] [--user N] [--count 1]

* `--package DIR`: a problem package (problems-examples/...). It becomes a published problem version;
  importing the same content again reuses the latest version, changed content creates a new one
  (published versions are never modified). Without `--package` a built-in batch problem "dev-sum"
  (a + b, two hidden tests) is used.
* Each problem gets its own pinned entry in the practice contest "dev".
* `--user N`: the submissions belong to student `dev-student-N` (created on demand), default 1.
* `--source` / `--archive` / `--entry`: the solution (a zip for `--archive`); without `--source` a correct
  Python a + b solution is sent. Code-less problems (choice, text_answer) are not supported here.

Prints a JSON list of {submission_id, judgement_id, problem_version_id, contest_problem_id}. Files go to
JUDGE_BLOB_DIR. Refuses to run without JUDGE_DEV=1.

The package is read with yaml and the same test rules as runner/package.py (glob patterns, `<name>.in`
with an optional `<name>.out`): the backend must not import the runner, and the real importer belongs to
module 4a. Keep the two in line until then.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import queue as q
from .blobstore import BlobStore
from .db import session_scope
from .models import (
    Blob, Contest, ContestMode, ContestProblem, Feedback, Language, Problem, ProblemType, ProblemVersion, Program,
    ProgramRole, Role, Scoring, Submission, TestCase, TestGroup, User, VersionStatus, Visibility,
)
from .settings import Settings

CONTEST_SLUG = "dev"
LANGUAGES = (("python", "Python 3"), ("c", "C"), ("cpp", "C++"))
DEFAULT_SOURCE = b"a, b = map(int, input().split())\nprint(a + b)\n"


@dataclass
class TestData:
    __test__ = False
    name: str
    input: bytes
    answer: Optional[bytes]


@dataclass
class GroupData:
    name: str
    points: Decimal = Decimal(0)
    scoring: str = "sum"
    visibility: str = "hidden"
    feedback: Optional[str] = None
    show_in_statement: bool = False
    depends_on: list = field(default_factory=list)
    tests: list = field(default_factory=list)


@dataclass
class PackageData:
    slug: str
    type: str
    titles: dict
    statements: dict
    config: dict  # problem.yaml without test_groups / solutions (docs/WORKER_PROTOCOL.md)
    groups: list
    programs: list  # (role, file name, language, source bytes)


BUILTIN = PackageData(
    slug="dev-sum", type="batch", titles={"ru": "Сумма (dev)"}, statements={"ru": "Выведите a + b."},
    config={"format_version": 1, "slug": "dev-sum", "type": "batch", "io": {"mode": "stdio"},
            "limits": {"time_ms": 1000, "memory_mb": 256}, "languages": ["python", "c", "cpp"],
            "checker": {"type": "tokens"}, "feedback": "first_fail"},
    groups=[GroupData("main", points=Decimal(100), tests=[TestData("01", b"1 2\n", b"3\n"),
                                                           TestData("02", b"-5 1000000000\n", b"999999995\n")])],
    programs=[],
)


def load_package(root: Path) -> PackageData:
    """problem.yaml + files -> PackageData (test rules as in runner/package.py)."""
    data = yaml.safe_load((root / "problem.yaml").read_text(encoding="utf-8"))
    if data["type"] in ("choice", "text_answer"):
        raise SystemExit(f"{data['type']} problems take answers, not code: not supported by dev_seed")
    groups = []
    for raw in data.get("test_groups", []):
        patterns = raw["tests"] if isinstance(raw["tests"], list) else [raw["tests"]]
        inputs = sorted({p for pattern in patterns for p in root.glob(pattern) if p.is_file()})
        tests = []
        for path in inputs:
            answer = path.with_suffix(".out")
            tests.append(TestData(path.stem, path.read_bytes(), answer.read_bytes() if answer.is_file() else None))
        groups.append(GroupData(
            name=raw["name"], points=Decimal(str(raw.get("points", 0))), scoring=raw.get("scoring", "sum"),
            visibility=raw.get("visibility", "hidden"), feedback=raw.get("feedback"),
            show_in_statement=bool(raw.get("show_in_statement", False)), depends_on=list(raw.get("depends_on", [])),
            tests=tests))
    programs = []
    checker = data.get("checker") or {}
    if checker.get("type") == "custom":
        path = root / checker["path"]
        programs.append(("checker", path.name, checker.get("language", "cpp"), path.read_bytes()))
    if data["type"] == "interactive":
        path = root / data["interactor"]["path"]
        programs.append(("interactor", path.name, data["interactor"].get("language", "cpp"), path.read_bytes()))
    statements = {}
    for lang in data["title"]:
        md = root / "statement" / f"{lang}.md"
        if md.is_file():
            statements[lang] = md.read_text(encoding="utf-8")
    config = {k: v for k, v in data.items() if k not in ("test_groups", "solutions")}
    return PackageData(data["slug"], data["type"], dict(data["title"]), statements, config, groups, programs)


# ----------------------------------------------------------------------------- fingerprints
def _fingerprint(ptype, titles, config, groups, programs) -> str:
    """Content of a problem version without database ids; equal content -> equal fingerprint."""
    body = {"type": ptype, "titles": titles, "config": config, "groups": groups, "programs": sorted(programs)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _package_fingerprint(pkg: PackageData) -> str:
    sha = lambda b: hashlib.sha256(b).hexdigest() if b is not None else None  # noqa: E731
    groups = [[g.name, i, f"{g.points:.2f}", g.scoring, g.visibility, g.feedback, g.show_in_statement, g.depends_on,
               [[t.name, j, sha(t.input), sha(t.answer)] for j, t in enumerate(g.tests)]]
              for i, g in enumerate(pkg.groups)]
    programs = [[role, name, lang, sha(src)] for role, name, lang, src in pkg.programs]
    return _fingerprint(pkg.type, pkg.titles, pkg.config, groups, programs)


def _version_fingerprint(v: ProblemVersion) -> str:
    groups = [[g.name, g.position, f"{g.points:.2f}", g.scoring.value, g.visibility.value,
               g.feedback.value if g.feedback else None, g.show_in_statement, g.depends_on,
               [[t.name, t.position, t.input_sha256, t.answer_sha256] for t in sorted(g.tests, key=lambda t: t.position)]]
              for g in sorted(v.groups, key=lambda g: g.position)]
    programs = [[p.role.value, p.name, p.language_key, p.source_sha256] for p in v.programs]
    return _fingerprint(v.type.value, v.titles, v.config, groups, programs)


# ----------------------------------------------------------------------------- database
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


def _student(session: Session, n: int) -> User:
    return _get_or_create(session, User, {"login": f"dev-student-{n}"}, password_hash="!",
                          display_name=f"Dev Student {n}", role=Role.student)


def ensure_version(session: Session, blobs: BlobStore, pkg: PackageData, owner: User) -> ProblemVersion:
    """The latest published version with this content, or a new published version."""
    problem = _get_or_create(session, Problem, {"slug": pkg.slug}, owner_id=owner.id)
    latest = session.execute(
        select(ProblemVersion).where(ProblemVersion.problem_id == problem.id,
                                     ProblemVersion.status == VersionStatus.published)
        .options(selectinload(ProblemVersion.groups).selectinload(TestGroup.tests), selectinload(ProblemVersion.programs))
        .order_by(ProblemVersion.version.desc()).limit(1)).scalar_one_or_none()
    if latest is not None and _version_fingerprint(latest) == _package_fingerprint(pkg):
        return latest
    number = (session.execute(select(func.max(ProblemVersion.version))
                              .where(ProblemVersion.problem_id == problem.id)).scalar() or 0) + 1
    version = ProblemVersion(problem_id=problem.id, version=number, status=VersionStatus.published,
                             type=ProblemType(pkg.type), titles=pkg.titles, statements=pkg.statements,
                             config=pkg.config, published_at=datetime.now(timezone.utc))
    session.add(version)
    session.flush()
    for position, g in enumerate(pkg.groups):
        group = TestGroup(version_id=version.id, position=position, name=g.name, points=g.points,
                          scoring=Scoring(g.scoring), visibility=Visibility(g.visibility),
                          feedback=Feedback(g.feedback) if g.feedback else None,
                          show_in_statement=g.show_in_statement, depends_on=g.depends_on)
        session.add(group)
        session.flush()
        for tpos, t in enumerate(g.tests):
            session.add(TestCase(group_id=group.id, position=tpos, name=t.name, input_sha256=_blob(session, blobs, t.input),
                                 answer_sha256=_blob(session, blobs, t.answer) if t.answer is not None else None))
    for role, name, lang, src in pkg.programs:
        session.add(Program(version_id=version.id, role=ProgramRole(role), name=name, language_key=lang,
                            source_sha256=_blob(session, blobs, src)))
    session.flush()
    return version


def ensure_contest_problem(session: Session, version: ProblemVersion, teacher: User) -> ContestProblem:
    """An entry of the "dev" contest pinned to this version (one per version)."""
    contest = _get_or_create(session, Contest, {"slug": CONTEST_SLUG}, title="Dev contest", mode=ContestMode.practice,
                             created_by=teacher.id)
    cp = session.execute(select(ContestProblem).where(ContestProblem.contest_id == contest.id,
                                                      ContestProblem.problem_version_id == version.id)).scalars().first()
    if cp is None:
        count = session.execute(select(func.count()).select_from(ContestProblem)
                                .where(ContestProblem.contest_id == contest.id)).scalar_one()
        cp = ContestProblem(contest_id=contest.id, problem_version_id=version.id, label=f"P{count + 1}", position=count)
        session.add(cp)
        session.flush()
    return cp


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="dev_seed")
    parser.add_argument("--package", type=Path, help="problem package directory (default: built-in dev-sum)")
    parser.add_argument("--source", type=Path, help="solution file (default: a correct Python a + b)")
    parser.add_argument("--language", default="python", choices=["python", "c", "cpp"])
    parser.add_argument("--archive", action="store_true", help="--source is a zip with several files")
    parser.add_argument("--entry", help="entry file inside the archive (interpreted languages)")
    parser.add_argument("--user", type=int, default=1, help="student number: dev-student-N")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args(argv)
    if os.environ.get("JUDGE_DEV") != "1":
        print("dev_seed writes test data: set JUDGE_DEV=1", file=sys.stderr)
        return 2
    if args.archive and not args.source:
        parser.error("--archive needs --source")
    pkg = load_package(args.package) if args.package else BUILTIN
    blobs = BlobStore(Settings.from_env().blob_dir)
    source = args.source.read_bytes() if args.source else DEFAULT_SOURCE
    out = []
    with session_scope() as session:
        for key, name in LANGUAGES:
            _get_or_create(session, Language, {"key": key}, name=name, config={})
        teacher = _get_or_create(session, User, {"login": "dev-teacher"}, password_hash="!",
                                 display_name="Dev Teacher", role=Role.teacher)
        version = ensure_version(session, blobs, pkg, teacher)
        cp = ensure_contest_problem(session, version, teacher)
        student = _student(session, args.user)
        source_sha = _blob(session, blobs, source)
        for _ in range(args.count):
            sub = Submission(user_id=student.id, contest_problem_id=cp.id, language_key=args.language,
                             source_sha256=source_sha, is_archive=args.archive, entry_file=args.entry)
            session.add(sub)
            session.flush()
            j = q.enqueue(session, sub)
            out.append({"submission_id": sub.id, "judgement_id": j.id, "problem_version_id": version.id,
                        "contest_problem_id": cp.id})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
