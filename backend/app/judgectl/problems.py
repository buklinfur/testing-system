"""judgectl problem ...: import packages (docs/PROBLEM_IMPORT.md), list and show versions, archive."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from .. import problem_checks as checks
from ..auth import audit
from ..blobstore import BlobStore
from ..ids import parse_id
from ..models import Problem, ProblemVersion
from ..problems import catalog
from ..problems.importer import import_package
from .core import CliError, Context, emit_json, show, spool, table


def _problem(db: Session, slug: str) -> Problem:
    try:
        return catalog.get_problem(db, slug)
    except catalog.CatalogError as exc:
        raise CliError(str(exc)) from exc


def _version(db: Session, problem: Problem, number) -> ProblemVersion:
    try:
        return catalog.get_version(db, problem, number)
    except catalog.CatalogError as exc:
        raise CliError(str(exc)) from exc


def problem_import(ctx: Context, args) -> int:
    """A folder, a zip, or a zip on stdin (`-`: tools/pack-problem.sh <folder> | judgectl problem import -)."""
    limits = ctx.settings.imports
    with tempfile.TemporaryDirectory(prefix="judgectl-import-") as tmp:
        if args.source != "-" and Path(args.source).is_dir():
            source = {"directory": Path(args.source), "source_name": Path(args.source).name}
        elif args.source == "-":  # streamed into a file with a counter: never more than the limit in memory or on disk
            spooled = Path(tmp) / "package.zip"
            spool(ctx.stdin, spooled, limits.max_archive_bytes, "a zip (tools/pack-problem.sh <folder>)")
            source = {"archive": spooled, "source_name": "stdin"}
        else:
            path = Path(args.source)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise CliError(f"cannot read {args.source} (judgectl runs as judge: give the file on stdin with `-`)")
            source = {"archive": path, "source_name": path.name}
        with ctx.session() as db:
            result = import_package(db, BlobStore(ctx.settings.blob_dir), limits, ctx.actor, ctx.clock(),
                                    dry_run=args.dry_run, **source)
    for warning in result.warnings:
        ctx.note(f"warning: {warning}")
    if ctx.json:
        emit_json(ctx, {"slug": result.slug, "result": result.kind, "version": result.number,
                        "content_sha256": result.content_sha256, "warnings": result.warnings,
                        "dry_run": args.dry_run, "status": result.status,
                        "check_id": result.check.id if result.check is not None else None})
    else:
        ctx.print(result.describe() + (" (dry run: nothing was written)" if args.dry_run else ""))
    return 0


def _ref(db: Session, ref: str) -> tuple:
    """<slug> or <slug>@<version> (default: the latest version) -> (problem, version)."""
    slug, _, text = ref.partition("@")
    number = parse_id(text) if text else None
    if text and number is None:
        raise CliError(f"not a version: {ref!r} (write {slug}@3)")
    problem = _problem(db, slug)
    return problem, _version(db, problem, number)


def problem_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        query = catalog.problems_query(archived=None if args.all else False).order_by(Problem.slug)
        rows = []
        for p in db.execute(query).scalars():
            summary = catalog.problem_summary(db, p)
            latest = summary["latest"] or {}
            rows.append({**summary, "latest": latest.get("version"), "status": latest.get("status")})
    show(ctx, rows, [("slug", "SLUG"), ("title", "TITLE"), ("type", "TYPE"), ("latest", "LATEST"),
                     ("status", "STATUS"), ("versions", "VERSIONS"), ("archived", "ARCHIVED")])
    return 0


def problem_versions(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = [{**v, "content": (v["content_sha256"] or "")[:12] or None}
                for v in catalog.versions(db, _problem(db, args.slug))]
    show(ctx, rows, [("version", "V"), ("status", "STATUS"), ("imported_at", "IMPORTED"), ("content", "CONTENT"),
                     ("files", "FILES"), ("pinned_in", "PINNED IN"), ("from_zip", "ZIP")])
    return 0


def problem_show(ctx: Context, args) -> int:
    """<slug> or <slug>@<version> (default: the latest version)."""
    with ctx.session() as db:
        problem, version = _ref(db, args.ref)
        data = catalog.version_detail(db, problem, version)
        data["groups"] = [{**g, "points": str(g["points"]), "tests": len(g["tests"])} for g in data["groups"]]
        data["files"] = len(data["files"])
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"{data['slug']} v{data['version']}  {data['status'].value}  {data['type'].value}"
              + ("  ARCHIVED" if data["archived"] else ""))
    for lang, title in data["titles"].items():
        ctx.print(f"  title [{lang}]: {title}")
    ctx.print(f"  imported {ctx.time(data['imported_at'])}; {data['files']} files; content {data['content_sha256'] or '-'}")
    ctx.print("groups:")
    show(ctx, data["groups"], [("name", "GROUP"), ("points", "POINTS"), ("scoring", "SCORING"),
                               ("visibility", "VISIBILITY"), ("tests", "TESTS")])
    ctx.print("programs:")
    show(ctx, data["programs"], [("role", "ROLE"), ("name", "NAME"), ("language", "LANG"), ("expect", "EXPECT")])
    print_check(ctx, data)
    return 0


def print_check(ctx: Context, data: dict) -> None:
    """The check report of a version for people: what failed and why (docs/WORKER_PROTOCOL.md, check report)."""
    active = next((c for c in data["checks"] if c["status"] != "done"), None)
    if active is not None:
        ctx.print(f"check #{active['id']} ({active['reason']}): {active['status']}"
                  + (f", {active['progress']} solutions" if active["progress"] else "")
                  + (f", {active['failures']} failed attempts" if active["failures"] else ""))
    report = data["check_report"]
    if not report:
        if active is None:
            ctx.print("check: none (imported before checks existed)")
        return
    reason = report.get("reason")
    ctx.print(f"check report: {report.get('outcome')}" + (f" ({reason})" if reason else "")
              + (f", by {report['worker']}" if report.get("worker") else "")
              + (f", {report['duration_ms'] / 1000:.1f} s" if report.get("duration_ms") is not None else "")
              + ("  [ended by the server]" if report.get("by") == "server" and reason != "nothing_to_check" else ""))
    if report.get("error"):
        ctx.print(f"  error: {report['error']}")
    for prog in report.get("programs") or []:
        ctx.print(f"  {prog.get('role')} {prog.get('name')}: {'compiled' if prog.get('ok') else 'DOES NOT COMPILE'}")
        if not prog.get("ok") and prog.get("log"):
            for line in str(prog["log"]).splitlines()[:30]:
                ctx.print(f"      {line}")
    rows = []
    for sol in report.get("solutions") or []:
        first = sol.get("first_fail") or {}
        rows.append({"mark": "ok" if sol.get("ok") else "FAIL", "name": sol.get("name"), "expected": sol.get("expected"),
                     "got": sol.get("got"), "score": sol.get("score"),
                     "first_fail": f"{first['group']}/{first['test']} {first['verdict']}" if first else None,
                     "note": (sol.get("error") or sol.get("message") or "").splitlines()[0][:80]
                     if (sol.get("error") or sol.get("message")) else None})
    if rows:
        table(ctx, rows, [("mark", ""), ("name", "SOLUTION"), ("expected", "EXPECTED"), ("got", "GOT"),
                          ("score", "SCORE"), ("first_fail", "FIRST FAIL"), ("note", "NOTE")])
    if len(data["checks"]) > 1 or (data["checks"] and active is None):
        ctx.print("checks: " + "; ".join(f"#{c['id']} {c['reason']} {c['outcome'] or c['status']}"
                                         + (f" ({c['report_reason']})" if c["report_reason"] else "")
                                         for c in data["checks"][:5]))


def problem_recheck(ctx: Context, args) -> int:
    """Check a version again with a worker; its status stays until the check ends (D-054)."""
    with ctx.session() as db:
        problem, version = _ref(db, args.ref)
        try:
            out = checks.recheck(db, ctx.actor, version, ctx.clock())
        except checks.CheckError as exc:
            raise CliError(str(exc)) from exc
        result = {"slug": problem.slug, "version": version.version, "result": out.kind, "check_id": out.check.id,
                  "status": version.status.value}
    if ctx.json:
        emit_json(ctx, result)
        return 0
    what = {"created": "queued", "already_queued": "already queued", "already_running": "already running"}[out.kind]
    ctx.print(f"{problem.slug}@{version.version}: check #{result['check_id']} {what}; the version stays "
              f"{result['status']} until it ends (judgectl problem show {problem.slug}@{version.version})")
    return 0


def problem_checks(ctx: Context, args) -> int:
    """The newest checks of every version."""
    with ctx.session() as db:
        rows = checks.recent(db, args.last)
    if not ctx.json:
        rows = [{**r, "ref": f"{r['problem']}@{r['version']}", "result": r["outcome"] or r["status"]} for r in rows]
    show(ctx, rows, [("id", "ID"), ("ref", "VERSION"), ("reason", "WHY"), ("result", "RESULT"),
                     ("report_reason", "REASON"), ("progress", "SOLUTIONS"), ("failures", "FAILS"),
                     ("created_at", "QUEUED"), ("finished_at", "FINISHED")])
    return 0


def _set_archived(ctx: Context, args, archived: bool) -> int:
    with ctx.session() as db:
        problem = _problem(db, args.slug)
        if problem.archived == archived:
            ctx.print(f"{problem.slug} is {'already archived' if archived else 'not archived'}")
            return 0
        problem.archived = archived
        audit.record(db, ctx.actor, "problem.archive" if archived else "problem.unarchive", ctx.clock(),
                     object_type="problem", object_id=problem.id, details={"slug": problem.slug})
    ctx.print(f"{problem.slug}: {'archived (no imports, no new pins; contests keep it)' if archived else 'unarchived'}")
    return 0


def problem_archive(ctx: Context, args) -> int:
    return _set_archived(ctx, args, True)


def problem_unarchive(ctx: Context, args) -> int:
    return _set_archived(ctx, args, False)
