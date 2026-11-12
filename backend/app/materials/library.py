"""The library («Справочник», 4v.4, docs/CLASSROOM.md §11, D-085): sections two levels deep, materials in them in the
teacher's order, the view of the library and the search by title.

Every change of the sections and of the materials in them takes one advisory lock (locks.LIBRARY) first: positions
at the end, the two levels and an order that must list exactly the current set are decided on what another change
wrote. Changes are rare; one lock for the whole library is enough.

What a student sees in the library is rules.in_library (the library audience, not archived); sections with nothing
for them are left out. An admin sees every section and every material, archived ones marked. The search is ILIKE on
the title with %, _ and \\ taken literally; the databases are C.UTF-8, so Cyrillic letters match in any case.

Functions flush but never commit; a refusal raises FeedError.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .. import locks
from ..auth import audit
from ..classroom import access
from ..classroom import items as feed
from ..classroom.items import FeedError
from ..models import LibrarySection, Material, MaterialGroup, MaterialLibrary, MaterialSection
from . import rules
from .service import refuse

LIKE_ESCAPE = "\\"


def get_section(db: Session, section_id: int) -> LibrarySection:
    section = db.get(LibrarySection, section_id)
    if section is None:
        raise FeedError(f"no section {section_id}", "section_not_found")
    return section


def _lock(db: Session) -> None:
    locks.take(db, locks.LIBRARY)


def _siblings(db: Session, parent_id: Optional[int]) -> list[LibrarySection]:
    cond = LibrarySection.parent_id.is_(None) if parent_id is None else LibrarySection.parent_id == parent_id
    return list(db.execute(select(LibrarySection).where(cond)
                           .order_by(LibrarySection.position, LibrarySection.id)).scalars())


def _has_children(db: Session, section: LibrarySection) -> bool:
    return db.execute(select(exists().where(LibrarySection.parent_id == section.id))).scalar()


def _section(section: LibrarySection) -> dict:
    return {"section": section.id, "title": section.title, "parent_id": section.parent_id}


def create_section(db: Session, actor: audit.Actor, now: datetime, title: str,
                   parent: Optional[LibrarySection] = None) -> LibrarySection:
    """A section at the end of the top level, or a subsection at the end of a top-level section."""
    _lock(db)
    title = rules.clean_title(title)
    refuse(rules.check_section_title(title))
    if parent is not None:
        db.refresh(parent)
    refuse(rules.check_section_place(None, parent, False))
    siblings = _siblings(db, parent.id if parent is not None else None)
    section = LibrarySection(parent_id=parent.id if parent is not None else None, title=title,
                             position=max((s.position for s in siblings), default=-1) + 1)
    db.add(section)
    db.flush()
    audit.record(db, actor, "library.section_create", now, object_type="library_section", object_id=section.id,
                 details=_section(section))
    return section


def rename_section(db: Session, actor: audit.Actor, section: LibrarySection, now: datetime, title: str) -> bool:
    _lock(db)
    title = rules.clean_title(title)
    refuse(rules.check_section_title(title))
    db.refresh(section)
    if title == section.title:
        return False
    before = section.title
    section.title = title
    db.flush()
    audit.record(db, actor, "library.section_rename", now, object_type="library_section", object_id=section.id,
                 details={**_section(section), "before": before})
    return True


def move_section(db: Session, actor: audit.Actor, section: LibrarySection, now: datetime,
                 parent: Optional[LibrarySection]) -> bool:
    """To the end of the top level (parent None) or of a top-level section; never deeper than two levels."""
    _lock(db)
    db.refresh(section)
    if parent is not None:
        db.refresh(parent)
    refuse(rules.check_section_place(section.id, parent, _has_children(db, section)))
    target = parent.id if parent is not None else None
    if section.parent_id == target:
        return False
    before = section.parent_id
    siblings = _siblings(db, target)
    section.parent_id = target
    section.position = max((s.position for s in siblings), default=-1) + 1
    db.flush()
    audit.record(db, actor, "library.section_move", now, object_type="library_section", object_id=section.id,
                 details={**_section(section), "before": before})
    return True


def delete_section(db: Session, actor: audit.Actor, section: LibrarySection, now: datetime) -> dict:
    """Delete a section without subsections (section_not_empty otherwise); its materials only leave it."""
    _lock(db)
    db.refresh(section)
    if _has_children(db, section):
        raise FeedError(f"the section {section.title!r} has subsections: move or delete them first",
                        "section_not_empty")
    slugs = list(db.execute(select(Material.slug).join(MaterialSection, MaterialSection.material_id == Material.id)
                            .where(MaterialSection.section_id == section.id)
                            .order_by(MaterialSection.position)).scalars())
    details = {**_section(section), "materials": slugs}
    db.delete(section)
    db.flush()
    audit.record(db, actor, "library.section_delete", now, object_type="library_section", object_id=details["section"],
                 details=details)
    return details


def _placed(db: Session, section: LibrarySection) -> list[MaterialSection]:
    return list(db.execute(select(MaterialSection).where(MaterialSection.section_id == section.id)
                           .order_by(MaterialSection.position, MaterialSection.material_id)).scalars())


def place(db: Session, actor: audit.Actor, material: Material, section: LibrarySection, now: datetime) -> bool:
    """The material at the end of the section (False: it is there already). An archived one is not placed
    (material_archived)."""
    _lock(db)
    db.refresh(material)
    if material.archived_at is not None:
        raise FeedError(f"the material {material.slug} is archived: unarchive it first", "material_archived")
    rows = _placed(db, section)
    if any(r.material_id == material.id for r in rows):
        return False
    db.add(MaterialSection(section_id=section.id, material_id=material.id,
                           position=max((r.position for r in rows), default=-1) + 1))
    db.flush()
    audit.record(db, actor, "library.place", now, object_type="library_section", object_id=section.id,
                 details={"section": section.id, "material": material.slug})
    return True


def unplace(db: Session, actor: audit.Actor, material: Material, section: LibrarySection, now: datetime) -> bool:
    _lock(db)
    row = db.get(MaterialSection, (section.id, material.id))
    if row is None:
        return False
    db.delete(row)
    db.flush()
    audit.record(db, actor, "library.unplace", now, object_type="library_section", object_id=section.id,
                 details={"section": section.id, "material": material.slug})
    return True


def reorder_sections(db: Session, actor: audit.Actor, now: datetime, ids: Sequence[int],
                     parent: Optional[LibrarySection] = None) -> list[int]:
    """The order of the top level (parent None) or of the subsections of a section: exactly the current ones, each
    once (order_invalid otherwise, what is wrong in `data`)."""
    _lock(db)
    rows = _siblings(db, parent.id if parent is not None else None)
    current, ids = [s.id for s in rows], list(ids)
    if not rules.check_order(current, ids):
        raise FeedError(f"the order must list every section here once: {current}", "order_invalid",
                        **rules.order_problems(current, ids))
    by_id = {s.id: s for s in rows}
    for position, section_id in enumerate(ids):
        by_id[section_id].position = position
    db.flush()
    if ids != current:
        audit.record(db, actor, "library.section_order", now, object_type="library_section",
                     object_id=parent.id if parent is not None else None,
                     details={"parent": parent.id if parent is not None else None, "before": current, "after": ids})
    return ids


def reorder_materials(db: Session, actor: audit.Actor, section: LibrarySection, now: datetime,
                      material_ids: Sequence[int]) -> list[int]:
    """The order of the materials of a section: exactly the current ones, each once (order_invalid)."""
    _lock(db)
    rows = _placed(db, section)
    current, ids = [r.material_id for r in rows], list(material_ids)
    if not rules.check_order(current, ids):
        raise FeedError(f"the order must list every material of the section once: {current}", "order_invalid",
                        **rules.order_problems(current, ids))
    by_id = {r.material_id: r for r in rows}
    for position, material_id in enumerate(ids):
        by_id[material_id].position = position
    db.flush()
    if ids != current:
        audit.record(db, actor, "library.order", now, object_type="library_section", object_id=section.id,
                     details={"section": section.id, "before": current, "after": ids})
    return ids


# ----------------------------------------------------------------------------- reading
def _visible(db: Session, viewer, materials) -> list[Material]:
    """The materials the library shows to the viewer (rules.in_library)."""
    if access.is_admin(viewer):
        return list(materials)
    live = feed.live_group_ids(db, viewer)
    materials = list(materials)
    groups: dict[int, set] = {}
    for material_id, group_id in db.execute(select(MaterialGroup.material_id, MaterialGroup.group_id)
                                            .where(MaterialGroup.material_id.in_([m.id for m in materials] or [-1]))):
        groups.setdefault(material_id, set()).add(group_id)
    return [m for m in materials if rules.in_library(viewer, m, groups.get(m.id, ()), live)]


def _card(m: Material) -> dict:
    return {"id": m.id, "slug": m.slug, "title": m.title, "archived": m.archived_at is not None}


def like_pattern(q: str) -> str:
    """%q% with %, _ and the escape character itself taken literally."""
    escaped = q.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", LIKE_ESCAPE + "%").replace("_", LIKE_ESCAPE + "_")
    return f"%{escaped}%"


def search(db: Session, viewer, q: str) -> list[dict]:
    """Materials of the library whose title contains q, ignoring case (Cyrillic too), by title."""
    q = (q or "").strip()
    if not q:
        return []
    found = db.execute(select(Material).where(Material.title.ilike(like_pattern(q), escape=LIKE_ESCAPE))
                       .order_by(func.lower(Material.title), Material.id)).scalars()
    return [_card(m) for m in _visible(db, viewer, found)]


def library_view(db: Session, viewer, now: datetime, q: Optional[str] = None) -> dict:
    """The library as the viewer sees it: the sections in order, each with its materials in order and its
    subsections, and `unsorted` - materials of the library in no section. With q: {"query", "materials"}, the search
    only. A student does not see sections with nothing for them."""
    if q is not None and q.strip():
        return {"query": q.strip(), "materials": search(db, viewer, q)}
    admin = access.is_admin(viewer)
    candidates = db.execute(select(Material).order_by(Material.id)).scalars()
    if not admin:
        candidates = db.execute(select(Material).where(Material.library != MaterialLibrary.none,
                                                       Material.archived_at.is_(None))
                                .order_by(Material.id)).scalars()
    visible = {m.id: m for m in _visible(db, viewer, candidates)}
    placed: dict[int, list] = {}
    in_any = set()
    for row in db.execute(select(MaterialSection).order_by(MaterialSection.position,
                                                           MaterialSection.material_id)).scalars():
        in_any.add(row.material_id)
        if row.material_id in visible:
            placed.setdefault(row.section_id, []).append(_card(visible[row.material_id]))
    sections = list(db.execute(select(LibrarySection).order_by(LibrarySection.position, LibrarySection.id)).scalars())
    children: dict = {}
    for s in sections:
        children.setdefault(s.parent_id, []).append(s)

    def node(s: LibrarySection) -> Optional[dict]:
        subs = [n for n in (node(c) for c in children.get(s.id, [])) if n is not None]
        materials = placed.get(s.id, [])
        if not admin and not materials and not subs:
            return None
        return {"id": s.id, "title": s.title, "materials": materials, "sections": subs}
    top = [n for n in (node(s) for s in children.get(None, [])) if n is not None]
    unsorted = [_card(m) for mid, m in visible.items() if mid not in in_any]
    return {"sections": top, "unsorted": unsorted}
