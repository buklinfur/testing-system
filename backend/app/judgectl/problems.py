"""judgectl problem ...: import packages (docs/PROBLEM_IMPORT.md), list and show versions, archive."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..auth import audit
from ..blobstore import BlobStore
from ..models import Problem, ProblemVersion
from ..problems import catalog
from ..problems.importer import import_package
from .core import CliError, Context, emit_json, show, spool


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
                        "dry_run": args.dry_run})
    else:
        ctx.print(result.describe() + (" (dry run: nothing was written)" if args.dry_run else ""))
    return 0


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
    slug, _, number = args.ref.partition("@")
    if number and not number.isdigit():
        raise CliError(f"not a version: {args.ref!r} (write {slug}@3)")
    with ctx.session() as db:
        problem = _problem(db, slug)
        data = catalog.version_detail(db, problem, _version(db, problem, int(number) if number else None))
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
