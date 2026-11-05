"""What there is to see about problems: lists, versions, one version (judgectl problem ... and /api/admin/problems).

Read-only; nothing here writes. Version numbers are per problem (`slug@3`); times stay datetimes (callers turn them
into text: judgectl tables, app/jsonable.py for the API).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .. import problem_checks as checks
from ..models import ContestProblem, Problem, ProblemVersion, Program, TestGroup


class CatalogError(LookupError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def problems_query(q: Optional[str] = None, archived: Optional[bool] = False):
    """select(Problem); archived=None: all, False: only active ones (the default), True: only archived ones."""
    query = select(Problem)
    if q:
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.where(Problem.slug.ilike(pattern, escape="\\"))
    if archived is not None:
        query = query.where(Problem.archived.is_(archived))
    return query


def get_problem(db: Session, slug: str) -> Problem:
    problem = db.execute(select(Problem).where(Problem.slug == slug)).scalar_one_or_none()
    if problem is None:
        raise CatalogError(f"no problem {slug!r}", "problem_not_found")
    return problem


def get_version(db: Session, problem: Problem, number: Optional[int] = None) -> ProblemVersion:
    """Version `number` of the problem, or its latest one."""
    query = select(ProblemVersion).where(ProblemVersion.problem_id == problem.id)
    query = query.where(ProblemVersion.version == number) if number is not None \
        else query.order_by(ProblemVersion.version.desc()).limit(1)
    version = db.execute(query.options(selectinload(ProblemVersion.groups).selectinload(TestGroup.tests))
                         ).scalar_one_or_none()
    if version is None:
        raise CatalogError(f"{problem.slug} has no version {number}" if number is not None
                           else f"{problem.slug} has no versions", "version_not_found")
    return version


def _title(titles: dict) -> str:
    return titles.get("ru") or next(iter(titles.values()), "") if titles else ""


def problem_summary(db: Session, problem: Problem) -> dict:
    latest = db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id)
                        .order_by(ProblemVersion.version.desc()).limit(1)).scalar_one_or_none()
    count = db.execute(select(func.count()).select_from(ProblemVersion)
                       .where(ProblemVersion.problem_id == problem.id)).scalar_one()
    return {"id": problem.id, "slug": problem.slug, "title": _title(latest.titles) if latest else "",
            "type": latest.type if latest else None, "archived": problem.archived, "versions": count,
            "latest": {"version": latest.version, "status": latest.status} if latest else None}


def versions(db: Session, problem: Problem) -> list:
    pinned = dict(db.execute(select(ContestProblem.problem_version_id, func.count())
                             .where(ContestProblem.problem_id == problem.id, ContestProblem.hidden.is_(False))
                             .group_by(ContestProblem.problem_version_id)).all())
    return [{"version": v.version, "status": v.status, "check_report": v.check_report, "imported_at": v.created_at,
             "content_sha256": v.content_sha256, "files": len(v.files or {}), "pinned_in": pinned.get(v.id, 0),
             "from_zip": v.package_sha256 is not None}
            for v in db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id)
                                .order_by(ProblemVersion.version)).scalars()]


def problem_detail(db: Session, problem: Problem) -> dict:
    return {**problem_summary(db, problem), "versions": versions(db, problem)}


def version_detail(db: Session, problem: Problem, version: ProblemVersion) -> dict:
    """Everything an admin sees about one version (hidden tests included: names, not contents)."""
    programs = db.execute(select(Program).where(Program.version_id == version.id)
                          .order_by(Program.role, Program.name)).scalars()
    return {
        "slug": problem.slug, "archived": problem.archived, "version": version.version, "status": version.status,
        "type": version.type, "titles": version.titles, "statements": version.statements, "config": version.config,
        "imported_at": version.created_at, "content_sha256": version.content_sha256,
        "manifest_sha256": version.manifest_sha256, "check_report": version.check_report,
        "files": sorted((version.files or {}).keys()),
        "groups": [{"name": g.name, "points": g.points, "scoring": g.scoring, "visibility": g.visibility,
                    "feedback": g.feedback, "depends_on": g.depends_on, "show_in_statement": g.show_in_statement,
                    "tests": [{"name": t.name, "visibility": t.visibility or g.visibility,
                               "has_answer": t.answer_sha256 is not None} for t in g.tests]}
                   for g in version.groups],
        "programs": [{"role": p.role, "name": p.name, "language": p.language_key, "expect": p.expected_verdict}
                     for p in programs],
        # the worker checks of this version, newest first (the report of the last finished one is check_report)
        "checks": [checks.check_row(c) for c in checks.history(session=db, version_id=version.id)],
    }
