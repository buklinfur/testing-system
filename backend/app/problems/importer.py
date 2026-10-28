"""The one way a problem version is created (R26): package -> checks -> canonical files -> blobs + ProblemVersion.

    import_package(db, blobs, limits, actor, now, archive=<path of a zip>)   # or directory=<path>

* The package is unpacked safely (unpack.py) or, for a folder, checked the same way, then validated
  (protocol/problem_package.py: schema, semantics, safe test masks).
* The problem is found by `slug`. The same content (content_sha256 over every file, junk excluded) as an
  existing version -> nothing is written, the result says "exists as vN" (R29). Other content -> version N+1.
* Nothing reaches the blob store before the package has passed every check and is known to be new.
* An archived problem refuses imports (judgectl problem unarchive).
* Until the worker check of session 4a.4 exists, a new version is `ready` at once (R30).
Callers own the transaction (judgectl: one per command; dev_seed: its session).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from protocol import problem_package
from protocol.problem_package import PackageError, content_digest, sha256_file

from ..auth import audit
from ..blobstore import BlobStore
from ..manifest_db import build_manifest
from ..models import (Blob, Feedback, Problem, ProblemType, ProblemVersion, Program, ProgramRole, Scoring, TestCase,
                      TestGroup, Verdict, VersionStatus, Visibility)
from ..settings import ImportSettings
from .unpack import check_directory, unpack_zip


class ImportFailed(PackageError):
    """The package was refused; `problems` lists every reason (nothing was written)."""


@dataclass
class ImportResult:
    kind: str  # "created" | "exists" | "new" (dry run: would be created; nothing written)
    slug: str
    number: int  # the version number (existing, created or to be created)
    content_sha256: str
    problem: Optional[Problem] = None
    version: Optional[ProblemVersion] = None  # None in a dry run of new content
    warnings: list = field(default_factory=list)

    def describe(self) -> str:
        what = {"created": "imported as", "exists": "already exists as", "new": "would be imported as"}[self.kind]
        return f"{self.slug}: {what} v{self.number}"


def store_blob(db: Session, blobs: BlobStore, data: bytes) -> str:
    sha = blobs.put(data)
    if db.get(Blob, sha) is None:
        db.add(Blob(sha256=sha, size_bytes=len(data)))
    return sha


def store_file(db: Session, blobs: BlobStore, path: Path) -> str:
    """store_blob for a file on disk, copied in chunks."""
    sha, size = blobs.put_file(path)
    if db.get(Blob, sha) is None:
        db.add(Blob(sha256=sha, size_bytes=size))
    return sha


def _statements(pkg: problem_package.Package) -> dict:
    out, problems = {}, []
    for lang, rel in pkg.statements.items():
        try:
            out[lang] = (pkg.root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"statement {rel} is not UTF-8 text")
    if problems:
        raise ImportFailed(problems)
    return out


def _program_name(ref: problem_package.ProgramRef) -> str:
    # checker and interactor keep their file name (the worker writes checker/<name>); solutions keep the path
    return ref.path if ref.role == "solution" else PurePosixPath(ref.path).name


def import_package(db: Session, blobs: BlobStore, limits: ImportSettings, actor: audit.Actor, now: datetime, *,
                   archive: Optional[Path] = None, directory: Optional[Path] = None, source_name: str = "",
                   owner_id: Optional[int] = None, dry_run: bool = False) -> ImportResult:
    """Import one package given as zip bytes or as a folder. ImportFailed (a PackageError) if it is refused.
    dry_run: every check, but nothing is written (no blob, no row); kind "new" or "exists"."""
    if (archive is None) == (directory is None):
        raise ValueError("give either archive or directory")
    with tempfile.TemporaryDirectory(prefix="judge-import-") as tmp:
        if archive is not None:
            root = Path(tmp) / "package"
            root.mkdir()
            files = unpack_zip(archive, root, limits)
        else:
            root = Path(directory)
            files = check_directory(root, limits)
        pkg = problem_package.load(root, files)
        hashes = {rel: sha256_file(root / rel) for rel in files}
        digest = content_digest(hashes)
        statements = _statements(pkg)

        # one import of a slug at a time: version numbers and "exists" stay right under concurrency
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"problem-import:{pkg.slug}"})
        problem = db.execute(select(Problem).where(Problem.slug == pkg.slug)).scalar_one_or_none()
        if problem is not None and problem.archived:
            raise ImportFailed([f"problem {pkg.slug} is archived: unarchive it first (judgectl problem unarchive "
                                f"{pkg.slug})"])
        if problem is not None:
            same = db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id,
                                                           ProblemVersion.content_sha256 == digest)).scalar_one_or_none()
            if same is not None:
                return ImportResult("exists", pkg.slug, same.version, digest, problem, same, pkg.warnings)
        if dry_run:
            last = db.execute(select(func.max(ProblemVersion.version))
                              .where(ProblemVersion.problem_id == problem.id)).scalar() if problem is not None else None
            return ImportResult("new", pkg.slug, (last or 0) + 1, digest, problem, None, pkg.warnings)
        if problem is None:
            problem = Problem(slug=pkg.slug, owner_id=owner_id if owner_id is not None else actor.user_id)
            db.add(problem)
            db.flush()

        sha_of = {rel: store_file(db, blobs, root / rel) for rel in files}
        package_sha = store_file(db, blobs, archive) if archive is not None else None
        number = (db.execute(select(func.max(ProblemVersion.version))
                             .where(ProblemVersion.problem_id == problem.id)).scalar() or 0) + 1
        version = ProblemVersion(problem_id=problem.id, version=number, status=VersionStatus.ready,
                                 type=ProblemType(pkg.data["type"]), titles=dict(pkg.data["title"]),
                                 statements=statements, config=pkg.config, files=hashes, content_sha256=digest,
                                 package_sha256=package_sha, imported_by=actor.user_id)
        db.add(version)
        db.flush()
        for position, g in enumerate(pkg.groups):
            group = TestGroup(version_id=version.id, position=position, name=g.name, points=Decimal(g.points),
                              scoring=Scoring(g.scoring), visibility=Visibility(g.visibility),
                              feedback=Feedback(g.feedback) if g.feedback else None,
                              show_in_statement=g.show_in_statement, depends_on=g.depends_on)
            db.add(group)
            db.flush()
            for tpos, t in enumerate(g.tests):
                db.add(TestCase(group_id=group.id, position=tpos, name=t.name, input_sha256=sha_of[t.input],
                                answer_sha256=sha_of[t.answer] if t.answer else None))
        for ref in pkg.programs:
            db.add(Program(version_id=version.id, role=ProgramRole(ref.role), name=_program_name(ref),
                           language_key=ref.language, source_sha256=sha_of[ref.path],
                           expected_verdict=Verdict(ref.expect) if ref.expect else None))
        db.flush()
        db.expire(version, ["groups", "programs"])  # rows were added by id: let the manifest query load them
        _manifest, version.manifest_sha256 = build_manifest(db, version.id)
        audit.record(db, actor, "problem.import", now, object_type="problem_version", object_id=version.id,
                     details={"slug": problem.slug, "version": number, "content_sha256": digest, "files": len(files),
                              "source": source_name or ("zip" if archive is not None else "directory"),
                              "package_sha256": package_sha})
        db.flush()
        return ImportResult("created", pkg.slug, number, digest, problem, version, pkg.warnings)
