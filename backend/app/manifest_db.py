"""Build the worker manifest of a problem version from the database."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from protocol.manifest import assemble_manifest, manifest_digest

from .models import ProblemVersion, TestGroup, VersionStatus


def build_manifest(session: Session, version_id: int) -> Optional[tuple]:
    """Returns (manifest, digest) or None if the version does not exist.

    The digest of a published version is stored in problem_versions.manifest_sha256 (it never changes
    afterwards), so claims do not rebuild the manifest. Drafts are rebuilt each time.
    """
    version = session.execute(
        select(ProblemVersion).where(ProblemVersion.id == version_id)
        .options(selectinload(ProblemVersion.groups).selectinload(TestGroup.tests),
                 selectinload(ProblemVersion.programs), selectinload(ProblemVersion.problem))
    ).scalar_one_or_none()
    if version is None:
        return None
    groups = [{
        "id": g.id, "position": g.position, "name": g.name, "points": g.points, "scoring": g.scoring.value,
        "visibility": g.visibility.value, "feedback": g.feedback.value if g.feedback else None,
        "show_in_statement": g.show_in_statement, "depends_on": g.depends_on,
        "tests": [{"id": t.id, "position": t.position, "name": t.name, "input_sha256": t.input_sha256,
                   "answer_sha256": t.answer_sha256} for t in g.tests],
    } for g in version.groups]
    programs = [{"role": p.role.value, "name": p.name, "language": p.language_key, "source_sha256": p.source_sha256}
                for p in version.programs]
    manifest = assemble_manifest(
        {"id": version.id, "slug": version.problem.slug, "type": version.type.value,
         "titles": version.titles, "config": version.config}, groups, programs)
    return manifest, manifest_digest(manifest)


def manifest_digest_for_claim(session: Session, version_id: int) -> str:
    version = session.get(ProblemVersion, version_id)
    if version.manifest_sha256:
        return version.manifest_sha256
    _manifest, digest = build_manifest(session, version_id)
    if version.status == VersionStatus.published:
        version.manifest_sha256 = digest
    return digest
