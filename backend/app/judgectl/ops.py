"""judgectl queue ..., judgectl worker ... (what app.admin_cli did in module 3, R33) and judgectl status."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

from .. import queue as q
from .. import system
from ..auth import audit
from ..blobstore import valid_sha256
from ..models import (Blob, Judgement, Problem, ProblemVersion, QueueEvent, Submission, TestResult, User, Worker,
                      WorkerToken)
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
    """Deprecated alias (module 4a.1 flags) of `judgectl submission rejudge`."""
    new = ["submission", "rejudge"]
    if args.submission is not None:
        new += ["--id", str(args.submission)]
    if args.contest_problem is not None:
        new += ["--contest-problem-id", str(args.contest_problem)]
    if args.verdicts:
        new += ["--verdicts", args.verdicts]
    ctx.note("queue rejudge is deprecated, use: judgectl " + " ".join(new))
    if (args.submission is None) == (args.contest_problem is None):
        raise CliError("give --submission ID or --contest-problem ID")
    from argparse import Namespace

    from .submissions import submission_rejudge

    return submission_rejudge(ctx, Namespace(id=args.submission, contest=None, problem=None,
                                             contest_problem_id=args.contest_problem, problem_version=None,
                                             verdicts=args.verdicts or None, dry_run=False))


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
        q.read_one_moment(db)  # the heartbeat ages are of the database's moment, like /system
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


# ----------------------------------------------------------------------------- status
UNREFERENCED_BLOBS = """
SELECT count(*) FROM blobs b
WHERE NOT EXISTS (SELECT 1 FROM test_cases t WHERE t.input_sha256 = b.sha256 OR t.answer_sha256 = b.sha256)
  AND NOT EXISTS (SELECT 1 FROM programs p WHERE p.source_sha256 = b.sha256)
  AND NOT EXISTS (SELECT 1 FROM submissions s WHERE s.source_sha256 = b.sha256)
  AND NOT EXISTS (SELECT 1 FROM problem_versions v WHERE v.package_sha256 = b.sha256)
  AND NOT EXISTS (SELECT 1 FROM problem_versions v, jsonb_each_text(coalesce(v.files, '{}'::jsonb)) f
                  WHERE f.value = b.sha256)
"""


# who refers to a blob: one row per reference, as (sha256, text)
BLOB_REFERENCES = """
SELECT t.input_sha256 AS sha, p.slug || '@' || v.version || ' test ' || g.name || '/' || t.name || ' (input)' AS what
  FROM test_cases t JOIN test_groups g ON g.id = t.group_id JOIN problem_versions v ON v.id = g.version_id
  JOIN problems p ON p.id = v.problem_id WHERE t.input_sha256 = ANY(:shas)
UNION ALL
SELECT t.answer_sha256, p.slug || '@' || v.version || ' test ' || g.name || '/' || t.name || ' (answer)'
  FROM test_cases t JOIN test_groups g ON g.id = t.group_id JOIN problem_versions v ON v.id = g.version_id
  JOIN problems p ON p.id = v.problem_id WHERE t.answer_sha256 = ANY(:shas)
UNION ALL
SELECT pr.source_sha256, p.slug || '@' || v.version || ' program ' || pr.role || ' ' || pr.name
  FROM programs pr JOIN problem_versions v ON v.id = pr.version_id JOIN problems p ON p.id = v.problem_id
  WHERE pr.source_sha256 = ANY(:shas)
UNION ALL
SELECT v.package_sha256, p.slug || '@' || v.version || ' uploaded zip'
  FROM problem_versions v JOIN problems p ON p.id = v.problem_id WHERE v.package_sha256 = ANY(:shas)
UNION ALL
SELECT f.value, p.slug || '@' || v.version || ' file ' || f.key
  FROM problem_versions v JOIN problems p ON p.id = v.problem_id, jsonb_each_text(coalesce(v.files, '{}'::jsonb)) f
  WHERE f.value = ANY(:shas)
UNION ALL
SELECT s.source_sha256, 'submission ' || s.id || ' (' || u.login || ', ' || c.slug || ' ' || cp.label || ')'
  FROM submissions s JOIN users u ON u.id = s.user_id JOIN contest_problems cp ON cp.id = s.contest_problem_id
  JOIN contests c ON c.id = cp.contest_id WHERE s.source_sha256 = ANY(:shas)
ORDER BY 1, 2
"""


def blob_references(db, shas) -> dict:
    """sha256 -> what refers to it (empty list: nothing)."""
    found = {sha: [] for sha in shas}
    for sha, what in db.execute(text(BLOB_REFERENCES), {"shas": list(shas)}).all():
        found[sha].append(what)
    return found


def blob_files(root: Path) -> set:
    """sha256 names of the files in the blob store (<root>/ab/<sha256>)."""
    if not root.is_dir():
        return set()
    return {p.name for sub in root.iterdir() if sub.is_dir() for p in sub.iterdir() if valid_sha256(p.name)}


migration_head = system.migration_head  # kept here for older imports


def status(ctx: Context, args) -> int:
    """One look at the installation: database and migrations, workers and queue, configuration, the blob store.
    Exit code 1 if the database is unreachable or not at the newest migration."""
    out, bad = {}, False
    try:
        with ctx.session() as db:
            snap = system.snapshot(db, ctx.settings)  # the same as GET /api/admin/system
            out["migration"] = snap["migration"]["current"]
            out["queue"] = {k: snap["load"][k] for k in ("level", "queue_length", "workers_online", "workers_busy")}
            out["workers_known"] = snap["workers_known"]
            out["checks"] = snap["checks"]
            config = snap["config_problems"]
            rows = set(db.execute(select(Blob.sha256)).scalars())
            out["blob_rows_unreferenced"] = db.execute(text(UNREFERENCED_BLOBS)).scalar_one()
        out["database"] = "ok"
    except SQLAlchemyError as exc:
        out["database"] = f"unreachable: {exc.__class__.__name__}: {str(exc).splitlines()[0][:160]}"
        rows, bad, config = None, True, system.config_problems(ctx.settings)
    out["migration_head"] = system.migration_head()
    if out["database"] == "ok" and out["migration"] != out["migration_head"]:
        bad = True
    out["config_problems"] = config
    out["timezone"] = ctx.settings.timezone
    if rows is not None:
        files = blob_files(Path(ctx.settings.blob_dir))
        out["blobs"] = {"files": len(files), "rows": len(rows), "files_without_row": len(files - rows),
                        "rows_without_file": len(rows - files), "rows_unreferenced": out.pop("blob_rows_unreferenced")}
        if getattr(args, "details", False) and rows - files:
            with ctx.session() as db:
                refs = blob_references(db, sorted(rows - files))
            out["blobs"]["missing_files"] = [{"sha256": sha, "referenced_by": what} for sha, what in refs.items()]
    if ctx.json:
        emit_json(ctx, out)
        return 1 if bad else 0
    ctx.print(f"database      {out['database']}")
    if "migration" in out:
        same = out["migration"] == out["migration_head"]
        ctx.print(f"migration     {out['migration'] or 'none'}" + ("" if same else f"  (code expects "
                                                                                  f"{out['migration_head']}: run the migrations)"))
        ctx.print(f"workers       {out['queue']['workers_online']} online, {out['queue']['workers_busy']} busy "
                  f"({out['workers_known']} known)")
        ctx.print(f"queue         {out['queue']['queue_length']} waiting, load {out['queue']['level']}")
        c = out["checks"]
        ctx.print(f"checks        {c['queued']} queued, {c['running']} running; {c['pending_versions']} versions "
                  "pending_check (judgectl problem checks)")
        for row in c["recent_failed"]:
            ctx.print(f"  failed      {row['problem']}@{row['version']} check #{row['id']} ({row['report_reason']}), "
                      f"{ctx.time(row['finished_at'])}")
        for row in c["pinned_failed"]:
            ctx.print(f"  warning     contest {row['contest']} {row['label']}: {row['warning']}")
    ctx.print("config        " + ("ok" if not out["config_problems"] else "; ".join(out["config_problems"])))
    ctx.print(f"timezone      {out['timezone']} (judgectl tables; the API is UTC)")
    if "blobs" in out:
        b = out["blobs"]
        ctx.print(f"blobs         {b['files']} files, {b['rows']} rows")
        ctx.print(f"  files without a row     {b['files_without_row']}  (left by imports that did not commit; "
                  "not deleted: a collector is module 6)")
        ctx.print(f"  rows without a file     {b['rows_without_file']}" + ("  (!) files are missing from "
                                                                         f"{ctx.settings.blob_dir}" if b["rows_without_file"] else ""))
        ctx.print(f"  rows referenced by nothing {b['rows_unreferenced']}")
        if b.get("missing_files"):
            ctx.print("rows without a file (--details):")
            for item in b["missing_files"]:
                refs = item["referenced_by"] or ["nothing refers to it"]
                more = f" (+{len(refs) - 5} more)" if len(refs) > 5 else ""
                ctx.print(f"  {item['sha256'][:12]}  " + "; ".join(refs[:5]) + more)
        elif b["rows_without_file"]:
            ctx.print("  (judgectl status --details: what refers to them)")
    return 1 if bad else 0
