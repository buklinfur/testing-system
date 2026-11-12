"""Links to materials in saved texts (4v.4, docs/CLASSROOM.md §11, D-085): material_refs written when a text is saved
and the warnings a teacher gets about it.

material_refs is an index of the texts, never their truth: every save of a source writes its rows again (delete +
insert), so the rows always say what the text says now. A source is a problem version (all its statements; written
once, at import, since a version never changes), a feed item (its body_md) or a material version (its body_md). The
slug is stored, not the material, so a link to a slug nobody has yet works once the material is created.

Functions flush but never commit.
"""
from __future__ import annotations

from typing import Mapping, Optional

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Material, MaterialRef
from . import links


def replace(db: Session, texts, *, problem_version_id: Optional[int] = None, feed_item_id: Optional[int] = None,
            material_version_id: Optional[int] = None) -> list[str]:
    """The rows of exactly one source, written again from its texts; the slugs written, in order."""
    source = {"problem_version_id": problem_version_id, "feed_item_id": feed_item_id,
              "material_version_id": material_version_id}
    given = {k: v for k, v in source.items() if v is not None}
    if len(given) != 1:
        raise ValueError("exactly one source")
    (column, value), = given.items()
    db.execute(delete(MaterialRef).where(getattr(MaterialRef, column) == value))
    slugs = links.slugs(texts)
    for slug in slugs:
        db.add(MaterialRef(slug=slug, **given))
    db.flush()
    return slugs


def known(db: Session, slugs) -> dict:
    """slug -> archived, for the slugs that have a material."""
    slugs = list(slugs)
    if not slugs:
        return {}
    rows = db.execute(select(Material.slug, Material.archived_at).where(Material.slug.in_(slugs))).all()
    return {slug: archived_at is not None for slug, archived_at in rows}


def warnings(db: Session, texts: Mapping[str, str]) -> list[str]:
    """links.warnings for these texts (where -> text) against the materials there are now: malformed links, slugs
    without a material, archived materials. Never a refusal."""
    return links.warnings(texts, known(db, links.slugs(texts.values())))
