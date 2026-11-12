"""Services of the library of materials (4v.4, docs/CLASSROOM.md §11, D-085): materials, their immutable versions and
the attachments of a version, the groups of the library, who opens a material, links for rendering, where a material
is used. The sections of the library are materials/library.py; the pure rules are materials/rules.py.

Versions. Every change of the content makes a new version: a new text (new_version), a file or a link added
(add_file, add_link), one removed (remove), a new order (reorder). The new version takes the text and the
attachments of the latest one (rows copied, blobs shared) with the change applied. All of it runs under the row of
the material locked FOR UPDATE, so two changes at once give two versions one after another, numbered without gaps or
repeats, each on top of the other. The same text again, or the same order, makes no version. The title and the
library audience are changed in place (update_material); the slug never (slug_immutable).

Attachments of a version are the attachments of 4v.3 with a second owner: the same rules (attachment_rules, the
teacher's limits), the same upload (blobstore.Incoming, adopted last, the caller closes it), the same precheck before
the body. Their ids are those of the latest version: a change copies them into new rows with new ids.

Visibility. An admin opens every material and every version. A student opens the latest version when
rules.open_basis finds a basis; otherwise the material is "not found" (material_not_found), whatever the reason, as
attachments are (D-080). Links of a text are resolved by rules.resolve: a student cannot tell a missing material from
a closed one.

An archived material is read only (material_archived) until unarchived; it leaves the library but still opens by the
feed and by links. Functions flush but never commit; a refusal raises FeedError and the caller rolls back. Every
change is audited without the content: lengths, names, sha and size, urls and titles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import audit
from ..blobstore import Incoming
from ..classroom import access
from ..classroom import attachment_rules as arules
from ..classroom import attachments as att
from ..classroom import items as feed
from ..classroom.items import FeedError
from ..contests import service as contests
from ..content_files import clean_name
from ..models import (Attachment, AttachmentKind, Contest, ContestProblem, FeedItem, FeedItemKind, FeedItemState,
                      Group, LibrarySection, Material, MaterialGroup, MaterialLibrary, MaterialRef, MaterialSection,
                      MaterialVersion, Problem, ProblemVersion, User)
from . import links, refs, rules

EDITABLE = ("title", "library")


def refuse(decision) -> None:
    if not decision:
        raise FeedError(decision.reason, decision.reason)


@dataclass
class VersionResult:
    """What a change of the content gave: the latest version, whether it is new, the warnings about its links."""

    version: MaterialVersion
    created: bool
    warnings: list = field(default_factory=list)


# ----------------------------------------------------------------------------- finding
def get_material(db: Session, material_id: int) -> Material:
    material = db.get(Material, material_id)
    if material is None:
        raise FeedError(f"no material {material_id}", "material_not_found")
    return material


def by_slug(db: Session, slug: str) -> Material:
    material = db.execute(select(Material).where(Material.slug == slug)).scalar_one_or_none()
    if material is None:
        raise FeedError(f"no material {slug}", "material_not_found")
    return material


def lock_material(db: Session, material: Material) -> Material:
    """The row of the material, locked (SELECT ... FOR UPDATE) and read again: its changes go one after another."""
    return db.execute(select(Material).where(Material.id == material.id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one()


def latest_version(db: Session, material: Material) -> MaterialVersion:
    return db.execute(select(MaterialVersion).where(MaterialVersion.material_id == material.id)
                      .order_by(MaterialVersion.version.desc()).limit(1)).scalar_one()


def get_version(db: Session, material: Material, number: int) -> MaterialVersion:
    version = db.execute(select(MaterialVersion).where(MaterialVersion.material_id == material.id,
                                                       MaterialVersion.version == number)).scalar_one_or_none()
    if version is None:
        raise FeedError(f"no version {number} of material {material.slug}", "version_not_found")
    return version


def version_attachments(db: Session, version: MaterialVersion) -> list[Attachment]:
    return list(db.execute(select(Attachment).where(Attachment.material_version_id == version.id)
                           .order_by(Attachment.position, Attachment.id)).scalars())


def _changeable(material: Material) -> None:
    if material.archived_at is not None:
        raise FeedError(f"the material {material.slug} is archived: unarchive it first", "material_archived")


def library_group_ids(db: Session, material: Material) -> set[int]:
    return set(db.execute(select(MaterialGroup.group_id).where(MaterialGroup.material_id == material.id)).scalars())


# ----------------------------------------------------------------------------- creating and editing
def create_material(db: Session, actor: audit.Actor, now: datetime, *, slug: str, title: str, body_md: str = "",
                    library: str = "none") -> VersionResult:
    """A new material and its version 1. The slug is checked as given (no trimming, no lower-casing) and is fixed
    from now on. slug_taken: another material, archived or not, has it."""
    refuse(rules.check_slug(slug))
    title = rules.clean_title(title)
    refuse(rules.check_title(title))
    body_md = "" if body_md is None else body_md
    refuse(rules.check_body(body_md))
    refuse(rules.check_library(library))
    if db.execute(select(Material.id).where(Material.slug == slug)).first() is not None:
        raise FeedError(f"there is already a material {slug}", "slug_taken")
    material = Material(slug=slug, title=title, library=MaterialLibrary(library), created_by=actor.user_id,
                        created_at=now, updated_at=now)
    try:
        with db.begin_nested():  # two creations of one slug at once: the second meets the unique index
            db.add(material)
            db.flush()
    except IntegrityError as exc:
        raise FeedError(f"there is already a material {slug}", "slug_taken") from exc
    version = MaterialVersion(material_id=material.id, version=1, body_md=body_md, created_by=actor.user_id,
                              created_at=now)
    db.add(version)
    db.flush()
    refs.replace(db, [body_md], material_version_id=version.id)
    audit.record(db, actor, "material.create", now, object_type="material", object_id=material.id,
                 details={"slug": slug, "title": title, "library": library, "version": 1,
                          "body_chars": len(body_md)})
    return VersionResult(version, True, refs.warnings(db, {"body": body_md}))


def update_material(db: Session, actor: audit.Actor, material: Material, now: datetime, **values) -> list:
    """Change the title and the library audience in place (EDITABLE). The slug never changes: a slug other than the
    current one is slug_immutable (create a new material and archive this one). Returns the changed names."""
    if "slug" in values:
        if values.pop("slug") != material.slug:
            raise FeedError("the slug of a material never changes (links in problem versions cannot be corrected): "
                            "create a new material and archive this one", "slug_immutable")
    unknown = sorted(set(values) - set(EDITABLE))
    if unknown:
        raise FeedError(f"these fields cannot be changed: {', '.join(unknown)}", "material_invalid")
    material = lock_material(db, material)
    _changeable(material)
    changes = {}
    if "title" in values:
        title = rules.clean_title(values["title"])
        refuse(rules.check_title(title))
        if title != material.title:
            changes["title"] = [material.title, title]
            material.title = title
    if "library" in values:
        library = getattr(values["library"], "value", values["library"])
        refuse(rules.check_library(library))
        if library != material.library.value:
            changes["library"] = [material.library.value, library]
            material.library = MaterialLibrary(library)
    if not changes:
        return []
    material.updated_at = now
    db.flush()
    audit.record(db, actor, "material.update", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "changes": changes})
    return list(changes)


def _copy(rows: Sequence[Attachment], version: MaterialVersion) -> list[Attachment]:
    """The attachments of a version as rows of the next one: same names, blobs, links, authors and moments, positions
    0.. in the given order."""
    return [Attachment(material_version_id=version.id, kind=r.kind, name=r.name, blob_sha256=r.blob_sha256,
                       size=r.size, url=r.url, title=r.title, position=position, created_by=r.created_by,
                       created_at=r.created_at) for position, r in enumerate(rows)]


def _next_version(db: Session, actor: audit.Actor, material: Material, latest: MaterialVersion, now: datetime,
                  body_md: str, rows: Sequence[Attachment]) -> MaterialVersion:
    """The version after `latest` (the material locked by the caller): the text and these attachments, links
    written again."""
    version = MaterialVersion(material_id=material.id, version=latest.version + 1, body_md=body_md,
                              created_by=actor.user_id, created_at=now)
    db.add(version)
    db.flush()
    db.add_all(_copy(rows, version))
    refs.replace(db, [body_md], material_version_id=version.id)
    material.updated_at = now
    db.flush()
    return version


def new_version(db: Session, actor: audit.Actor, material: Material, now: datetime, body_md: str) -> VersionResult:
    """A new text: version + 1 with the attachments of the latest one. The same text as the latest: no version
    (created False)."""
    body_md = "" if body_md is None else body_md
    refuse(rules.check_body(body_md))
    material = lock_material(db, material)
    _changeable(material)
    latest = latest_version(db, material)
    if latest.body_md == body_md:
        return VersionResult(latest, False, refs.warnings(db, {"body": body_md}))
    version = _next_version(db, actor, material, latest, now, body_md, version_attachments(db, latest))
    audit.record(db, actor, "material.version", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "version": version.version, "from": latest.version,
                          "body_chars": len(body_md)})
    return VersionResult(version, True, refs.warnings(db, {"body": body_md}))


# ----------------------------------------------------------------------------- attachments of a version
def _locked(db: Session, material: Material) -> tuple[Material, MaterialVersion, list[Attachment]]:
    material = lock_material(db, material)
    _changeable(material)
    latest = latest_version(db, material)
    return material, latest, version_attachments(db, latest)


def _file_names(rows: Sequence[Attachment]) -> list[str]:
    return [r.name for r in rows if r.kind == AttachmentKind.file]


def precheck_file(db: Session, material: Material, name: str,
                  limits: arules.Limits = arules.TEACHER_LIMITS) -> str:
    """The cheap refusals of a file before its body (the admin API calls it before save_body): the material is
    archived, the latest version has its limit of attachments, the cleaned name is not allowed or repeats one. No
    lock, nothing written: add_file decides again under the lock of the material. Returns the cleaned name."""
    _changeable(material)
    rows = version_attachments(db, latest_version(db, material))
    if len(rows) + 1 > limits.files_max:
        att.refuse(arules.Refusal("too_many_attachments"))
    clean = clean_name(name)
    att.refuse(arules.check_name(clean))
    if any(arules.same_name(clean, n) for n in _file_names(rows)):
        att.refuse(arules.Refusal("duplicate_name", 0))
    return clean


def add_file(db: Session, actor: audit.Actor, material: Material, upload: Incoming, now: datetime, *,
             limits: arules.Limits = arules.TEACHER_LIMITS) -> MaterialVersion:
    """A new version with this file appended. The upload must be finished; it is adopted only when accepted (the
    caller closes it either way)."""
    upload.finish()
    material, latest, rows = _locked(db, material)
    view = att.file_view(upload)
    att.refuse(arules.check_new_attachment(_file_names(rows), len(rows), file=view, limits=limits))
    sha = att.adopt(db, upload)
    new = Attachment(kind=AttachmentKind.file, name=view.name, blob_sha256=sha, size=view.size,
                     created_by=actor.user_id, created_at=now)
    version = _next_version(db, actor, material, latest, now, latest.body_md, [*rows, new])
    added = version_attachments(db, version)[-1]
    audit.record(db, actor, "material.attachment_add", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "version": version.version, "attachment": added.id, "kind": "file",
                          "name": view.name, "sha256": sha, "size": view.size})
    return version


def add_link(db: Session, actor: audit.Actor, material: Material, url: str, now: datetime, *,
             title: Optional[str] = None, limits: arules.Limits = arules.TEACHER_LIMITS) -> MaterialVersion:
    """A new version with this link appended (stored after strip; the server never opens it)."""
    material, latest, rows = _locked(db, material)
    link = att.rules_link(url, title)
    att.refuse(arules.check_new_attachment(_file_names(rows), len(rows), link=link, limits=limits))
    new = Attachment(kind=AttachmentKind.link, url=link.url, title=link.title, created_by=actor.user_id,
                     created_at=now)
    version = _next_version(db, actor, material, latest, now, latest.body_md, [*rows, new])
    added = version_attachments(db, version)[-1]
    audit.record(db, actor, "material.attachment_add", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "version": version.version, "attachment": added.id, "kind": "link",
                          "url": link.url, "title": link.title})
    return version


def remove(db: Session, actor: audit.Actor, material: Material, attachment_id: int, now: datetime) -> MaterialVersion:
    """A new version without this attachment of the latest one (attachment_not_found: not one of them). The blob
    stays, and so does the old version with it."""
    material, latest, rows = _locked(db, material)
    row = next((r for r in rows if r.id == attachment_id), None)
    if row is None:
        raise FeedError(f"no attachment {attachment_id} in the latest version of {material.slug}",
                        "attachment_not_found")
    details = att.describe(row)
    details.pop("created_at")
    details["attachment"] = details.pop("id")
    version = _next_version(db, actor, material, latest, now, latest.body_md, [r for r in rows if r is not row])
    audit.record(db, actor, "material.attachment_remove", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "version": version.version, **details})
    return version


def reorder(db: Session, actor: audit.Actor, material: Material, ids: Sequence[int],
            now: datetime) -> VersionResult:
    """A new version with the attachments in this order: `ids` must be exactly those of the latest version, each
    once (attachment_order_invalid otherwise, what is wrong in `data`). The same order: no version."""
    material, latest, rows = _locked(db, material)
    current = [r.id for r in rows]
    ids = list(ids)
    if not rules.check_order(current, ids):
        raise FeedError(f"the order must list every attachment of the latest version once: {current}",
                        "attachment_order_invalid", **rules.order_problems(current, ids))
    if ids == current:
        return VersionResult(latest, False)
    by_id = {r.id: r for r in rows}
    version = _next_version(db, actor, material, latest, now, latest.body_md, [by_id[i] for i in ids])
    audit.record(db, actor, "material.attachment_order", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug, "version": version.version, "before": current, "after": ids})
    return VersionResult(version, True)


# ----------------------------------------------------------------------------- archive and the library audience
def archive(db: Session, actor: audit.Actor, material: Material, now: datetime) -> bool:
    material = lock_material(db, material)
    if material.archived_at is not None:
        return False
    material.archived_at = now
    material.updated_at = now
    db.flush()
    audit.record(db, actor, "material.archive", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug})
    return True


def unarchive(db: Session, actor: audit.Actor, material: Material, now: datetime) -> bool:
    material = lock_material(db, material)
    if material.archived_at is None:
        return False
    material.archived_at = None
    material.updated_at = now
    db.flush()
    audit.record(db, actor, "material.unarchive", now, object_type="material", object_id=material.id,
                 details={"slug": material.slug})
    return True


def set_library_groups(db: Session, actor: audit.Actor, material: Material, now: datetime,
                       groups: Iterable[Group]) -> list[int]:
    """The groups the library shows the material to (with library = groups): exactly these. A group added must not
    be archived (group_archived); one already there stays even if archived since. Returns the group ids."""
    material = lock_material(db, material)
    _changeable(material)
    groups = list({g.id: g for g in groups}.values())
    before = library_group_ids(db, material)
    wanted = {g.id for g in groups}
    for g in groups:
        if g.id not in before and g.archived_at is not None:
            raise FeedError(f"the group {g.name} is archived", "group_archived")
    for gid in before - wanted:
        db.delete(db.get(MaterialGroup, (material.id, gid)))
    for gid in sorted(wanted - before):
        db.add(MaterialGroup(material_id=material.id, group_id=gid, added_by=actor.user_id, added_at=now))
    db.flush()
    if before != wanted:
        audit.record(db, actor, "material.library_groups", now, object_type="material", object_id=material.id,
                     details={"slug": material.slug, "before": sorted(before), "after": sorted(wanted)})
    return sorted(wanted)


def add_library_groups(db: Session, actor: audit.Actor, material: Material, now: datetime,
                       groups: Iterable[Group]) -> list[int]:
    current = {g.id: g for g in db.execute(select(Group).join(MaterialGroup, MaterialGroup.group_id == Group.id)
                                           .where(MaterialGroup.material_id == material.id)).scalars()}
    return set_library_groups(db, actor, material, now, [*current.values(), *groups])


def remove_library_groups(db: Session, actor: audit.Actor, material: Material, now: datetime,
                          groups: Iterable[Group]) -> list[int]:
    drop = {g.id for g in groups}
    current = [g for g in db.execute(select(Group).join(MaterialGroup, MaterialGroup.group_id == Group.id)
                                     .where(MaterialGroup.material_id == material.id)).scalars() if g.id not in drop]
    return set_library_groups(db, actor, material, now, current)


# ----------------------------------------------------------------------------- who opens it
def open_inputs(db: Session, material: Material, user: User) -> dict:
    """What rules.open_basis needs about this student and this material, from the database: the library groups and
    the student's current groups; the material items of it and the feed items linking it (published, with what
    item_listed needs); the contest problems whose version links it (with what problem_visible needs)."""
    material_items = db.execute(select(FeedItem).where(FeedItem.kind == FeedItemKind.material,
                                                       FeedItem.material_id == material.id,
                                                       FeedItem.state == FeedItemState.published)).scalars()
    linking = db.execute(select(FeedItem).join(MaterialRef, MaterialRef.feed_item_id == FeedItem.id)
                         .where(MaterialRef.slug == material.slug,
                                FeedItem.state == FeedItemState.published)).scalars()
    items = []
    for links_it, rows in ((False, list(material_items)), (True, list(linking))):
        for item in rows:
            inputs = feed.view_inputs(db, item, user)
            items.append(rules.ItemBasis(item, inputs["in_audience"], links=links_it,
                                         contest=inputs.get("contest"), participation=inputs.get("participation"),
                                         in_contest_group=inputs.get("in_contest_group", False)))
    problems = []
    rows = db.execute(select(ContestProblem, Contest).join(Contest, Contest.id == ContestProblem.contest_id)
                      .join(MaterialRef, MaterialRef.problem_version_id == ContestProblem.problem_version_id)
                      .where(MaterialRef.slug == material.slug).order_by(ContestProblem.id)).all()
    for cp, contest in rows:
        participation, in_group = contests.access_inputs(db, contest, user)
        problems.append(rules.ProblemBasis(contest, cp, participation, in_group))
    return {"library_group_ids": library_group_ids(db, material), "live_group_ids": feed.live_group_ids(db, user),
            "items": items, "problems": problems}


def open_basis(db: Session, viewer, material: Material, now: datetime) -> Optional[str]:
    if access.is_admin(viewer):
        return "admin"
    return rules.open_basis(viewer, material, now, **open_inputs(db, material, viewer))


def can_open(db: Session, viewer, material: Material, now: datetime) -> bool:
    return open_basis(db, viewer, material, now) is not None


def _openable(db: Session, viewer, material: Material, now: datetime) -> None:
    if not can_open(db, viewer, material, now):
        raise FeedError(f"no material {material.id}", "material_not_found")


def resolve(db: Session, viewer, texts: Iterable[str], now: datetime) -> list[dict]:
    """The links of these texts, one entry per distinct slug in order, as rules.resolve gives them to this viewer."""
    slugs = links.slugs(texts)
    found = {}
    if slugs:
        for material in db.execute(select(Material).where(Material.slug.in_(slugs))).scalars():
            found[material.slug] = (material, can_open(db, viewer, material, now))
    return rules.resolve(viewer, slugs, found)


def material_view(db: Session, viewer, material: Material, now: datetime) -> dict:
    """The latest version as the viewer sees it: the text as stored, the attachments (never their content) and the
    links of the text resolved for this viewer. material_not_found: a student without a basis. An admin also gets
    the slug's state, the library audience and the number of versions."""
    _openable(db, viewer, material, now)
    version = latest_version(db, material)
    out = {"id": material.id, "slug": material.slug, "title": material.title, "version": version.version,
           "updated_at": material.updated_at, "body_md": version.body_md,
           "attachments": [att.describe(r) for r in version_attachments(db, version)],
           "links": resolve(db, viewer, [version.body_md], now)}
    if access.is_admin(viewer):
        out.update(material_summary(db, material))
    return out


def material_file(db: Session, viewer, material: Material, attachment_id: int, now: datetime, *,
                  version: Optional[int] = None) -> Attachment:
    """One file of a version for the viewer, to be served by content_files.file_response after this check. A student:
    the latest version only, and only with a basis (material_not_found otherwise); an admin: any version.
    attachment_not_found: not a file of that version (another version's, a link, no such id)."""
    _openable(db, viewer, material, now)
    if version is not None and not access.is_admin(viewer):
        version = None
    v = get_version(db, material, version) if version is not None else latest_version(db, material)
    row = db.execute(select(Attachment).where(Attachment.id == attachment_id,
                                              Attachment.material_version_id == v.id)).scalar_one_or_none()
    if row is None or row.kind != AttachmentKind.file:
        raise FeedError(f"no file attachment {attachment_id} of {material.slug} v{v.version}", "attachment_not_found")
    return row


# ----------------------------------------------------------------------------- what the teacher sees
def material_summary(db: Session, material: Material) -> dict:
    latest = db.execute(select(func.max(MaterialVersion.version))
                        .where(MaterialVersion.material_id == material.id)).scalar_one()
    groups = list(db.execute(select(Group).join(MaterialGroup, MaterialGroup.group_id == Group.id)
                             .where(MaterialGroup.material_id == material.id).order_by(Group.id)).scalars())
    sections = list(db.execute(select(LibrarySection).join(MaterialSection,
                                                           MaterialSection.section_id == LibrarySection.id)
                               .where(MaterialSection.material_id == material.id)
                               .order_by(LibrarySection.id)).scalars())
    return {"id": material.id, "slug": material.slug, "title": material.title, "library": material.library,
            "archived_at": material.archived_at, "created_at": material.created_at,
            "updated_at": material.updated_at, "created_by": material.created_by, "versions": latest,
            "groups": [{"id": g.id, "name": g.name, "archived": g.archived_at is not None} for g in groups],
            "sections": [{"id": s.id, "title": s.title, "parent_id": s.parent_id} for s in sections]}


def versions(db: Session, material: Material) -> list[dict]:
    """The history: every version, newest first, without the text (its length) and with the number of
    attachments."""
    counts = dict(db.execute(select(Attachment.material_version_id, func.count())
                             .join(MaterialVersion, MaterialVersion.id == Attachment.material_version_id)
                             .where(MaterialVersion.material_id == material.id)
                             .group_by(Attachment.material_version_id)).all())
    rows = db.execute(select(MaterialVersion).where(MaterialVersion.material_id == material.id)
                      .order_by(MaterialVersion.version.desc())).scalars()
    return [{"version": v.version, "created_at": v.created_at, "created_by": v.created_by,
             "body_chars": len(v.body_md), "attachments": counts.get(v.id, 0)} for v in rows]


def version_detail(db: Session, material: Material, number: int) -> dict:
    """One version for an admin: its text and attachments."""
    v = get_version(db, material, number)
    return {"slug": material.slug, "version": v.version, "created_at": v.created_at, "created_by": v.created_by,
            "body_md": v.body_md, "attachments": [att.describe(r) for r in version_attachments(db, v)]}


def where_used(db: Session, material: Material) -> dict:
    """Where the material is given and linked: material items of it; feed items linking it; problem versions whose
    statements link it (slug@version); materials whose latest version links it."""
    items = db.execute(select(FeedItem).where(FeedItem.material_id == material.id).order_by(FeedItem.id)).scalars()
    linking = db.execute(select(FeedItem).join(MaterialRef, MaterialRef.feed_item_id == FeedItem.id)
                         .where(MaterialRef.slug == material.slug).order_by(FeedItem.id)).scalars()
    problems = db.execute(select(Problem.slug, ProblemVersion.version)
                          .join(ProblemVersion, ProblemVersion.problem_id == Problem.id)
                          .join(MaterialRef, MaterialRef.problem_version_id == ProblemVersion.id)
                          .where(MaterialRef.slug == material.slug)
                          .order_by(Problem.slug, ProblemVersion.version)).all()
    latest = (select(MaterialVersion.material_id, func.max(MaterialVersion.version).label("v"))
              .group_by(MaterialVersion.material_id).subquery())
    others = db.execute(select(Material.slug, MaterialVersion.version)
                        .join(MaterialVersion, MaterialVersion.material_id == Material.id)
                        .join(latest, (latest.c.material_id == MaterialVersion.material_id)
                              & (latest.c.v == MaterialVersion.version))
                        .join(MaterialRef, MaterialRef.material_version_id == MaterialVersion.id)
                        .where(MaterialRef.slug == material.slug, Material.id != material.id)
                        .order_by(Material.slug)).all()

    def item(i):
        return {"id": i.id, "kind": i.kind.value, "state": i.state.value, "title": i.title}
    return {"material_items": [item(i) for i in items], "linking_items": [item(i) for i in linking],
            "problems": [f"{slug}@{v}" for slug, v in problems], "materials": [slug for slug, _v in others]}
