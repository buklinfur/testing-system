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
  a student will through the API of 4b: the student enters (with the contest's invite code), presses "start" in a
  personal contest, and every submission goes through service.submission_decision (access, window, upsolving,
  deadline and extensions, cooldown, limit, language) and keeps its `late`. A refusal stops dev_seed with its
  reason and exit code 1. Not with --package.
* `--item ID [--text-file F]`: hand in work on manual assignment ID (the class feed, 4v.1) as student
  `dev-student-N` would through the API of 4b.2: classroom.works.submit_work with the rules of
  classroom/access.py (audience, available_from, deadline and extensions, already graded, the answer and its
  limit); the text of F is the answer (none: just «сдано», for answer_kind mark). Prints {submission_id, work_id,
  late, status}; a refusal stops with its code and exit code 1. Not with --package / --contest.
* `--item ID [--file F]... [--link URL [TITLE]]...`: files and links of the hand-in (answer_kind files /
  text_or_files), through the same service, rules, limits and quota as a student's (JUDGE_SUBMISSION_*,
  JUDGE_STUDENT_QUOTA_MB): a file is written into <JUDGE_BLOB_DIR>/tmp and taken into the store only when the
  hand-in is accepted. The output adds `files` (id, kind, name, size, sha256; the text answer is kind text) and
  `links`: the ids for `judgectl work file`.
* `--item ID --comment TEXT [--private]`: a comment of student `dev-student-N` as through the API of 4b.2:
  classroom.comments.post_shared (the shared thread of the item) or, with --private, post_private (their own
  private thread of an assignment), with the rules of comment_rules.py and the rate JUDGE_COMMENT_RATE_PER_MIN.
  Prints {comment_id, thread_id, kind, login}; a refusal stops with its code and exit code 1. Not with --text-file.
* `--user N`: the submissions belong to student `dev-student-N` (created on demand), default 1.
* `--source` / `--archive` / `--entry`: the solution (a zip for `--archive`); without `--source` a correct
  Python a + b solution is sent. Code-less problems (choice, text_answer) are not supported here.

Prints a JSON list of {submission_id, judgement_id, problem_version_id, contest_problem_id, import, counted}
(import: "created" or "exists", null with --contest; counted: false for upsolving). Files go to JUDGE_BLOB_DIR. Refuses to run without JUDGE_DEV=1.
"""
from __future__ import annotations

import argparse
import contextlib
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
from .classroom import items as feed
from .classroom import attachments as works_links
from .classroom import comments, works
from .contests import access, service
from .db import session_scope
from .models import AccessMode, Contest, ContestProblem, ContestState, Language, ProblemType, ProblemVersion, Role, \
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
                             access_mode=AccessMode.open, created_by=admin.id)
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
    part, in_group = service.access_inputs(session, contest, student)
    if part is None:
        part = service.enter(session, student, contest, now, contest.invite_code)
        print(f"dev_seed: {student.login} entered {contest.slug} ({part.joined_via.value})", file=sys.stderr)
    if access.can_start(student, contest, part, now, in_group=in_group):
        service.start(session, student, contest, now)
        print(f"dev_seed: {student.login} pressed start", file=sys.stderr)
    out = []
    for i in range(args.count):
        if i:  # each submission is its own request; the first one was received at `now` above
            now = datetime.now(timezone.utc)
        decision = service.submission_decision(session, student, contest, cp, now, language=args.language)
        if not decision:
            wait = f" (retry in {decision.retry_after_s} s)" if decision.retry_after_s else ""
            print(f"dev_seed: refused by the access rules: {decision.reason}{wait}", file=sys.stderr)
            break
        source_sha = store_blob(session, blobs, source)  # "reading the source" happens after the receipt time
        sub = Submission(user_id=student.id, contest_problem_id=cp.id, language_key=args.language,
                         source_sha256=source_sha, is_archive=args.archive, entry_file=args.entry, created_at=now,
                         late=decision.late)
        session.add(sub)
        session.flush()
        j = q.enqueue(session, sub)
        out.append({"submission_id": sub.id, "judgement_id": j.id, "problem_version_id": cp.problem_version_id,
                    "contest_problem_id": cp.id, "import": None, "counted": decision.counted,
                    "late": decision.late})
    return out


def hand_in(blobs: BlobStore, settings: Settings, args) -> int:
    """--item: one hand-in as the student would make it (the receipt time is taken before the text and the files are
    read): the text, the files (each written into the blob store's tmp as it would be received, taken into the store
    only when the hand-in is accepted) and the links, under the limits and the quota of JUDGE_* (Settings.files)."""
    now = datetime.now(timezone.utc)
    text = args.text_file.read_text(encoding="utf-8") if args.text_file else None
    with contextlib.ExitStack() as stack:
        uploads = []
        for path in args.file or []:
            upload = stack.enter_context(blobs.incoming(path.name))
            with open(path, "rb") as source:
                upload.copy_from(source)
            uploads.append(upload)
        links = [works_links.LinkIn(link[0], link[1] if len(link) > 1 else None) for link in args.link or []]
        with session_scope() as session:
            student = _student(session, args.user)
            try:
                item = feed.get_item(session, args.item)
                sub, decision = works.submit_work(session, student, item, now, blobs=blobs, text=text,
                                                  text_limit=settings.assignment_text_max_bytes, files=uploads,
                                                  links=links, limits=works_links.hand_in_limits(settings.files),
                                                  quota=settings.files.student_quota_bytes)
            except feed.FeedError as exc:
                print(f"dev_seed: refused: {exc.code} ({exc})", file=sys.stderr)
                session.rollback()
                return 1
            work = works.work_of(session, item, student)
            print(json.dumps({"submission_id": sub.id, "work_id": work.id, "late": decision.late,
                              "status": work.status.value, "login": student.login,
                              **works.submission_files(session, sub)}, ensure_ascii=False))
    return 0


def comment(settings: Settings, args) -> int:
    """--item --comment: one comment as the student would write it."""
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        student = _student(session, args.user)
        try:
            item = feed.get_item(session, args.item)
            post = comments.post_private if args.private else comments.post_shared
            row = post(session, student, item, now, args.comment, limit=settings.comment_rate_per_min)
        except feed.FeedError as exc:
            print(f"dev_seed: refused: {exc.code} ({exc})", file=sys.stderr)
            session.rollback()
            return 1
        print(json.dumps({"comment_id": row.id, "thread_id": row.thread_id,
                          "kind": "private" if args.private else "shared", "login": student.login}))
    return 0


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
    parser.add_argument("--item", type=int, help="id of a manual assignment of the class feed: hand in work")
    parser.add_argument("--text-file", type=Path, help="with --item: the text answer")
    parser.add_argument("--file", type=Path, action="append",
                        help="with --item: a file handed in (its base name is the name); repeat for more")
    parser.add_argument("--link", nargs="+", action="append", metavar=("URL", "TITLE"),
                        help="with --item: a link handed in, with an optional title; repeat for more")
    parser.add_argument("--comment", help="with --item: write this comment instead of handing in work")
    parser.add_argument("--private", action="store_true", help="with --comment: in the student's private thread")
    args = parser.parse_args(argv)
    if args.comment is not None and (args.item is None or args.text_file or args.file or args.link):
        parser.error("--comment goes with --item, not with --text-file, --file or --link")
    if any(len(link) > 2 for link in args.link or []):
        parser.error("--link takes a URL and at most one title")
    if (args.file or args.link) and args.item is None:
        parser.error("--file and --link go with --item")
    if args.private and args.comment is None:
        parser.error("--private goes with --comment")
    if args.text_file and args.item is None:
        parser.error("--text-file goes with --item")
    if args.item is not None and (args.contest or args.package):
        parser.error("--item hands in work on an assignment: not with --contest or --package")
    if (args.contest is None) != (args.label is None):
        parser.error("--contest and --label go together")
    if args.contest and args.package:
        parser.error("--contest submits to a pinned version: not with --package")
    if os.environ.get("JUDGE_DEV") != "1":
        print("dev_seed writes test data: set JUDGE_DEV=1", file=sys.stderr)
        return 2
    if args.archive and not args.source:
        parser.error("--archive needs --source")
    settings = Settings.from_env()
    blobs = BlobStore(settings.blob_dir)
    if args.comment is not None:
        return comment(settings, args)
    if args.item is not None:
        return hand_in(blobs, settings, args)
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
        if version.status.value != "ready":  # dev only: pinned anyway, a worker checks it in the meantime
            print(f"dev_seed: v{version.version} is {version.status.value}: pinned in the dev contest without "
                  "waiting for its check (judgectl problem show)", file=sys.stderr)
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
                        "contest_problem_id": cp.id, "import": result.kind, "counted": True,
                        "version_status": version.status.value})
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
