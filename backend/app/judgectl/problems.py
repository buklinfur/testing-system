"""judgectl problem ...: import packages (docs/PROBLEM_IMPORT.md), list and show versions, archive."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..auth import audit
from ..blobstore import BlobStore
from ..models import ContestProblem, Problem, ProblemVersion, Program, TestGroup
from ..problems.importer import import_package
from .core import CliError, Context, emit_json, read_input, show


def _problem(db: Session, slug: str) -> Problem:
    problem = db.execute(select(Problem).where(Problem.slug == slug)).scalar_one_or_none()
    if problem is None:
        raise CliError(f"no problem {slug!r}")
    return problem


def _version(db: Session, problem: Problem, number) -> ProblemVersion:
    query = select(ProblemVersion).where(ProblemVersion.problem_id == problem.id)
    query = query.where(ProblemVersion.version == number) if number is not None \
        else query.order_by(ProblemVersion.version.desc()).limit(1)
    version = db.execute(query.options(selectinload(ProblemVersion.groups).selectinload(TestGroup.tests))).scalar_one_or_none()
    if version is None:
        raise CliError(f"{problem.slug} has no version {number}" if number is not None else f"{problem.slug} has no versions")
    return version


def problem_import(ctx: Context, args) -> int:
    """A folder, a zip, or a zip on stdin (`-`: tools/pack-problem.sh <folder> | judgectl problem import -)."""
    limits = ctx.settings.imports
    if args.source != "-" and Path(args.source).is_dir():
        source = {"directory": Path(args.source), "source_name": Path(args.source).name}
    else:
        data = read_input(ctx, args.source, limits.max_archive_bytes, "a zip (tools/pack-problem.sh <folder>)")
        source = {"archive": data, "source_name": "stdin" if args.source == "-" else Path(args.source).name}
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
        query = select(Problem).order_by(Problem.slug)
        if not args.all:
            query = query.where(Problem.archived.is_(False))
        rows = []
        for p in db.execute(query).scalars():
            latest = db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == p.id)
                                .order_by(ProblemVersion.version.desc()).limit(1)).scalar_one_or_none()
            count = db.execute(select(func.count()).select_from(ProblemVersion)
                               .where(ProblemVersion.problem_id == p.id)).scalar_one()
            rows.append({"slug": p.slug, "title": (latest.titles.get("ru") or next(iter(latest.titles.values()), ""))
                         if latest else "", "latest": latest.version if latest else None,
                         "status": latest.status if latest else None, "type": latest.type if latest else None,
                         "versions": count, "archived": p.archived})
    show(ctx, rows, [("slug", "SLUG"), ("title", "TITLE"), ("type", "TYPE"), ("latest", "LATEST"),
                     ("status", "STATUS"), ("versions", "VERSIONS"), ("archived", "ARCHIVED")])
    return 0


def problem_versions(ctx: Context, args) -> int:
    with ctx.session() as db:
        problem = _problem(db, args.slug)
        pinned = dict(db.execute(select(ContestProblem.problem_version_id, func.count())
                                 .where(ContestProblem.problem_id == problem.id, ContestProblem.hidden.is_(False))
                                 .group_by(ContestProblem.problem_version_id)).all())
        rows = [{"version": v.version, "status": v.status, "imported_at": v.created_at,
                 "content": (v.content_sha256 or "")[:12] or None, "files": len(v.files or {}),
                 "contests": pinned.get(v.id, 0), "from_zip": v.package_sha256 is not None}
                for v in db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id)
                                    .order_by(ProblemVersion.version)).scalars()]
    show(ctx, rows, [("version", "V"), ("status", "STATUS"), ("imported_at", "IMPORTED"), ("content", "CONTENT"),
                     ("files", "FILES"), ("contests", "PINNED IN"), ("from_zip", "ZIP")])
    return 0


def problem_show(ctx: Context, args) -> int:
    """<slug> or <slug>@<version> (default: the latest version)."""
    slug, _, number = args.ref.partition("@")
    if number and not number.isdigit():
        raise CliError(f"not a version: {args.ref!r} (write {slug}@3)")
    with ctx.session() as db:
        problem = _problem(db, slug)
        v = _version(db, problem, int(number) if number else None)
        programs = db.execute(select(Program).where(Program.version_id == v.id).order_by(Program.role, Program.name)).scalars()
        data = {
            "slug": problem.slug, "version": v.version, "status": v.status, "type": v.type, "titles": v.titles,
            "archived": problem.archived, "imported_at": v.created_at, "content_sha256": v.content_sha256,
            "manifest_sha256": v.manifest_sha256, "files": len(v.files or {}), "check_report": v.check_report,
            "groups": [{"name": g.name, "points": str(g.points), "scoring": g.scoring, "visibility": g.visibility,
                        "tests": len(g.tests)} for g in v.groups],
            "programs": [{"role": p.role, "name": p.name, "language": p.language_key, "expect": p.expected_verdict}
                         for p in programs],
        }
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
