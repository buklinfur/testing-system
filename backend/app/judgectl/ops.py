"""judgectl queue ... and judgectl worker ...: what app.admin_cli did in module 3 (R33)."""
from __future__ import annotations

from sqlalchemy import select, update

from .. import queue as q
from ..auth import audit
from ..models import Judgement, Problem, ProblemVersion, QueueEvent, Submission, TestResult, User, Worker, WorkerToken
from .core import CliError, Context, emit_json, show


# ----------------------------------------------------------------------------- queue
def queue_status(ctx: Context, args) -> int:
    with ctx.session() as db:
        q.read_one_moment(db)
        snap = q.load_snapshot(db, ctx.settings.queue).to_dict()
        metrics = q.collect_metrics(db, ctx.settings.queue)
    if ctx.json:
        emit_json(ctx, {"load": snap, "metrics": metrics})
    else:
        for key, value in snap.items():
            ctx.print(f"{key:<18} {'-' if value is None else value}")
    return 0


def queue_reap(ctx: Context, args) -> int:
    with ctx.session() as db:
        result = q.reap(db, ctx.settings.queue)
    if ctx.json:
        emit_json(ctx, result)
    else:
        ctx.print(result)
    return 0


def queue_rejudge(ctx: Context, args) -> int:
    """Old flags of admin_cli for now; `judgectl submission rejudge` arrives in session 4a.2."""
    if (args.submission is None) == (args.contest_problem is None):
        raise CliError("give --submission ID or --contest-problem ID")
    with ctx.session() as db:
        now = ctx.clock()
        if args.submission is not None:
            outcome = q.request_rejudge(db, args.submission)
            result = {"submission": args.submission, "kind": outcome.kind, "judgement": outcome.judgement.id}
            audit.record(db, ctx.actor, "submission.rejudge", now, object_type="submission",
                         object_id=args.submission, details={"kind": outcome.kind})
        else:
            verdicts = [v for v in args.verdicts.split(",") if v] or None
            result = q.rejudge_contest_problem(db, args.contest_problem, only_current_verdicts=verdicts)
            audit.record(db, ctx.actor, "contest_problem.rejudge", now, object_type="contest_problem",
                         object_id=args.contest_problem, details={"verdicts": verdicts, "result": result})
    if ctx.json:
        emit_json(ctx, result)
    else:
        ctx.print(result["kind"] if args.submission is not None else result)
    return 0


def queue_judgements(ctx: Context, args) -> int:
    """Newest judgements first (admin view: internal errors included)."""
    with ctx.session() as db:
        rows = db.execute(
            select(Judgement, Submission, User.login, Problem.slug, ProblemVersion.version, Worker.name)
            .join(Submission, Submission.id == Judgement.submission_id)
            .join(User, User.id == Submission.user_id)
            .join(ProblemVersion, ProblemVersion.id == Judgement.problem_version_id)
            .join(Problem, Problem.id == ProblemVersion.problem_id)
            .outerjoin(Worker, Worker.id == Judgement.worker_id)
            .order_by(Judgement.id.desc()).limit(args.last)).all()
        out = []
        for j, sub, login, slug, version, worker in rows:
            kinds = db.execute(select(TestResult.tl_kind).where(TestResult.judgement_id == j.id,
                                                                TestResult.tl_kind.is_not(None))).scalars().all()
            took = (j.finished_at - j.started_at).total_seconds() if j.finished_at and j.started_at else None
            out.append({"id": j.id, "submission": sub.id, "user": login, "problem": f"{slug}@{version}",
                        "language": (sub.language_key or "-") + ("+zip" if sub.is_archive else ""),
                        "status": j.status.value, "verdict": j.verdict.value if j.verdict else None,
                        "score": j.score, "attempt": j.attempt, "failures": j.failures,
                        "first_fail": (j.summary or {}).get("first_fail"),
                        "tl_kind": ",".join(sorted(set(kinds))) or None,
                        "judge_s": None if took is None else round(took, 2), "worker": worker,
                        "error": (j.error or "").replace("\n", " ")[:60] or None})
    show(ctx, out, [("id", "ID"), ("submission", "SUB"), ("user", "USER"), ("problem", "PROBLEM"),
                    ("language", "LANG"), ("status", "STATUS"), ("verdict", "VERDICT"), ("score", "SCORE"),
                    ("attempt", "ATT"), ("failures", "FAIL"), ("first_fail", "FIRST"), ("tl_kind", "TL"),
                    ("judge_s", "JUDGE_S"), ("worker", "WORKER"), ("error", "ERROR")])
    return 0


def queue_events(ctx: Context, args) -> int:
    """Newest queue events first (kept JUDGE_EVENT_RETENTION_DAYS, 14 by default)."""
    with ctx.session() as db:
        rows = db.execute(select(QueueEvent, Worker.name).outerjoin(Worker, Worker.id == QueueEvent.worker_id)
                          .order_by(QueueEvent.id.desc()).limit(args.last)).all()
        out = [{"id": e.id, "at": e.at, "kind": e.kind, "judgement": e.judgement_id, "worker": worker,
                "details": e.details} for e, worker in rows]
    show(ctx, out, [("id", "ID"), ("at", "AT"), ("kind", "KIND"), ("judgement", "JUDGEMENT"), ("worker", "WORKER"),
                    ("details", "DETAILS")])
    return 0


# ----------------------------------------------------------------------------- worker
def worker_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = q.worker_table(db, ctx.settings.queue)
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    view = [{"name": w["name"], "state": w["state"],
             "heartbeat": None if w["heartbeat_age_s"] is None else f"{w['heartbeat_age_s']:.0f}s",
             "judgement": w["judgement_id"], "cpus": w["info"].get("cpus"), "jobs": w["stats"].get("jobs_done", 0),
             "capabilities": ",".join(w["capabilities"] or [])} for w in rows]
    show(ctx, view, [("name", "NAME"), ("state", "STATE"), ("heartbeat", "HEARTBEAT"), ("judgement", "JUDGEMENT"),
                     ("cpus", "CPUS"), ("jobs", "JOBS"), ("capabilities", "CAPS")])
    return 0


def worker_token_create(ctx: Context, args) -> int:
    caps = [c.strip() for c in args.caps.split(",") if c.strip()]
    with ctx.session() as db:
        token = q.create_worker_token(db, args.name, caps)
        audit.record(db, ctx.actor, "worker.token_create", ctx.clock(), object_type="worker_token",
                     object_id=args.name, details={"capabilities": caps})
    if ctx.json:
        emit_json(ctx, {"name": args.name, "capabilities": caps, "token": token})
    else:  # the token stays the LAST line: deploy scripts take it with `| tail -1`
        ctx.print("Token (shown once, put it into /etc/judge/worker.env as JUDGE_WORKER_TOKEN):")
        ctx.print(token)
    return 0


def worker_token_revoke(ctx: Context, args) -> int:
    with ctx.session() as db:
        now = ctx.clock()
        n = db.execute(update(WorkerToken).where(WorkerToken.name == args.name, WorkerToken.revoked_at.is_(None))
                       .values(revoked_at=now)).rowcount
        if not n:
            raise CliError(f"no live worker token named {args.name!r}")
        audit.record(db, ctx.actor, "worker.token_revoke", now, object_type="worker_token", object_id=args.name)
    ctx.print(f"revoked: {args.name}")
    return 0
