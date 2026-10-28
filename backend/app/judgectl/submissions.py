"""judgectl submission ...: the admin view of one submission, and rejudges (R33; docs/QUEUE.md for what a rejudge is)."""
from __future__ import annotations

from sqlalchemy import select

from .. import queue as q
from ..auth import audit
from ..contests import service
from ..models import (Contest, ContestProblem, Judgement, Problem, ProblemVersion, Submission, TestCase, TestResult,
                      User)
from .core import CliError, Context, emit_json, show

SELECTORS = ("id", "contest", "contest_problem_id", "problem_version")


def submission_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        found = db.execute(select(Submission, User.login, Contest.slug, ContestProblem.label, Problem.slug)
                           .join(User, User.id == Submission.user_id)
                           .join(ContestProblem, ContestProblem.id == Submission.contest_problem_id)
                           .join(Contest, Contest.id == ContestProblem.contest_id)
                           .join(Problem, Problem.id == ContestProblem.problem_id)
                           .where(Submission.id == args.id)).first()
        if found is None:
            raise CliError(f"no submission {args.id}")
        sub, login, contest, label, problem = found
        judgements = []
        for j, version in db.execute(select(Judgement, ProblemVersion.version)
                                     .join(ProblemVersion, ProblemVersion.id == Judgement.problem_version_id)
                                     .where(Judgement.submission_id == sub.id).order_by(Judgement.id)).all():
            judgements.append({"id": j.id, "reason": j.reason, "status": j.status, "verdict": j.verdict,
                               "score": str(j.score), "version": version, "current": j.is_current,
                               "attempt": j.attempt, "created_at": j.created_at, "error": j.error})
        current = next((j["id"] for j in judgements if j["current"]), None)
        tests = [{"test": name, "verdict": r.verdict, "time_ms": r.time_ms, "memory_kb": r.memory_kb,
                  "tl_kind": r.tl_kind, "visible_to_student": r.visible_to_student,
                  "checker_message": (r.checker_message or "")[:60] or None}
                 for r, name in db.execute(select(TestResult, TestCase.name)
                                           .join(TestCase, TestCase.id == TestResult.test_case_id)
                                           .where(TestResult.judgement_id == current).order_by(TestResult.id)).all()] \
            if current else []
        data = {"id": sub.id, "user": login, "contest": contest, "label": label, "problem": problem,
                "language": sub.language_key, "archive": sub.is_archive, "created_at": sub.created_at,
                "judgements": judgements, "tests_of_current": tests}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"submission {data['id']}: {data['user']}, {data['contest']} {data['label']} ({data['problem']}), "
              f"{data['language'] or 'answers'}{' archive' if data['archive'] else ''}, {ctx.time(data['created_at'])}")
    show(ctx, judgements, [("id", "JUDGEMENT"), ("reason", "REASON"), ("status", "STATUS"), ("verdict", "VERDICT"),
                           ("score", "SCORE"), ("version", "V"), ("current", "CURRENT"), ("attempt", "ATT"),
                           ("error", "ERROR")])
    if tests:
        ctx.print("tests of the current judgement (admin view: hidden tests included):")
        show(ctx, tests, [("test", "TEST"), ("verdict", "VERDICT"), ("time_ms", "MS"), ("memory_kb", "KB"),
                          ("tl_kind", "TL"), ("visible_to_student", "OPEN"), ("checker_message", "CHECKER")])
    return 0


def submission_rejudge(ctx: Context, args) -> int:
    """One submission (--id), one problem of a contest (--contest + --problem, or --contest-problem-id), or every
    contest problem pinned to a version (--problem-version <slug>@<v>: visible and hidden ones)."""
    given = [n for n in SELECTORS if getattr(args, n, None) is not None]
    if len(given) != 1 or (args.contest is None) != (args.problem is None):
        raise CliError("give one of: --id N | --contest <slug> --problem <label> | --contest-problem-id N | "
                       "--problem-version <slug>@<version>")
    verdicts = [v.strip().upper() for v in (args.verdicts or "").split(",") if v.strip()] or None
    if verdicts and args.id is not None:
        raise CliError("--verdicts selects among many submissions: not with --id")
    with ctx.session() as db:
        now = ctx.clock()
        if args.id is not None:
            if db.get(Submission, args.id) is None:
                raise CliError(f"no submission {args.id}")
            outcome = q.request_rejudge(db, args.id, requested_by=ctx.actor.user_id)
            result = {"submission": args.id, "kind": outcome.kind, "judgement": outcome.judgement.id}
            audit.record(db, ctx.actor, "submission.rejudge", now, object_type="submission", object_id=args.id,
                         details={"kind": outcome.kind})
        else:
            if args.contest is not None:
                rows = [service.visible_row(db, service.get_contest(db, args.contest), args.problem)]
            elif args.contest_problem_id is not None:
                row = db.get(ContestProblem, args.contest_problem_id)
                if row is None:
                    raise CliError(f"no contest problem {args.contest_problem_id}")
                rows = [row]
            else:
                slug, _, number = args.problem_version.partition("@")
                version = db.execute(select(ProblemVersion).join(Problem, Problem.id == ProblemVersion.problem_id)
                                     .where(Problem.slug == slug, ProblemVersion.version == int(number))
                                     ).scalar_one_or_none() if number.isdigit() else None
                if version is None:
                    raise CliError(f"no problem version {args.problem_version!r} (write <slug>@<version>, e.g. sum@2)")
                rows = list(db.execute(select(ContestProblem).where(
                    ContestProblem.problem_version_id == version.id)).scalars())
            result = {"contest_problems": [r.id for r in rows], "created": 0, "retargeted": 0, "already_running": 0}
            for row in rows:
                counts = q.rejudge_contest_problem(db, row.id, requested_by=ctx.actor.user_id,
                                                   only_current_verdicts=verdicts)
                for key, n in counts.items():
                    result[key] += n
                audit.record(db, ctx.actor, "contest_problem.rejudge", now, object_type="contest_problem",
                             object_id=row.id, details={"verdicts": verdicts, "result": counts})
    if ctx.json:
        emit_json(ctx, result)
    elif args.id is not None:
        ctx.print(f"submission {args.id}: {result['kind']} (judgement {result['judgement']})")
    else:
        ctx.print(f"{len(result['contest_problems'])} contest problem(s): {result['created']} rejudges queued, "
                  f"{result['retargeted']} queued ones retargeted, {result['already_running']} already running")
    return 0
