"""Rules of the library of materials (4v.4, docs/CLASSROOM.md §11, D-083): pure functions.

No database and no clock of their own: the service passes `now` and what it read (the student's current groups, the
groups of the library, the feed items and contest problems that give a basis). Dates are compared only in
app/deadline_rules.py, through classroom/access.py and contests/access.py.

A material has a slug (fixed once created: links in immutable problem versions cannot be corrected, so changing it
would break them silently; instead a new material and the old one archived), a title (changed in place) and
immutable versions; everyone sees the latest one.

A student opens a material when at least one basis holds (`open_basis` tells which; admins open everything):
1. library - the material is not archived and its library audience is all_students, or a group (not archived) the
   student is in now;
2. feed - a feed item of kind material for it is in the student's feed (access.item_listed: published, addressed,
   from available_from on, not archived); an archived material stays open this way;
3. item_link - a feed item whose text links the material is in the student's feed (item_listed); the deadline and a
   statement hidden after it do not take it away;
4. problem_link - a contest problem whose version's statement links the material is visible to the student
   (contests.access.problem_visible: no participation needed, the window, the deadline and a hidden statement do not
   matter).
A link from another material gives nothing: there is no chain of bases.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping, Optional, Sequence

from ..classroom import access
from ..contests import access as contest_access
from ..deadline_rules import Decision

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")  # == the slug of a problem (problem.schema.json)
TITLE_MAX = 200
SECTION_TITLE_MAX = 200
BODY_MAX_CHARS = 100_000  # the Markdown of one version, code points (char_length)
LIBRARY = ("none", "all_students", "groups")
SECTION_DEPTH = 2  # a section or a subsection
# what may not be in a title: control, format (direction marks, zero width ...), surrogates, private use, unassigned,
# line and paragraph separators
BAD_TITLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})

OK = Decision(True)
LIBRARY_BASIS, FEED_BASIS, ITEM_LINK_BASIS, PROBLEM_LINK_BASIS = "library", "feed", "item_link", "problem_link"


def _v(value) -> str:
    return getattr(value, "value", value)


def _no(code: str) -> Decision:
    return Decision(False, code)


# --------------------------------------------------------------------------- what a teacher types
def check_slug(slug) -> Decision:
    """slug_invalid unless it is a slug exactly (no trimming, no lower-casing here: the service does neither)."""
    return OK if isinstance(slug, str) and SLUG_RE.fullmatch(slug) else _no("slug_invalid")


def clean_title(title) -> str:
    """Runs of whitespace become one space, the ends are trimmed (as the titles of feed items)."""
    return " ".join(title.split()) if isinstance(title, str) else ""


def check_title(title, max_chars: int = TITLE_MAX, code: str = "title_invalid") -> Decision:
    """A cleaned title: 1..max_chars characters, nothing invisible or controlling the terminal."""
    if not isinstance(title, str) or not title or len(title) > max_chars:
        return _no(code)
    if any(unicodedata.category(ch) in BAD_TITLE_CATEGORIES for ch in title):
        return _no(code)
    return OK


def check_body(body) -> Decision:
    """The Markdown of a version, stored as sent (untrusted: rendered only through a sanitizer). May be empty (a
    material of attachments only). body_invalid - NUL or a lone surrogate (PostgreSQL text cannot hold them);
    body_too_long - more than BODY_MAX_CHARS code points."""
    if not isinstance(body, str) or "\x00" in body or any("\ud800" <= ch <= "\udfff" for ch in body):
        return _no("body_invalid")
    return _no("body_too_long") if len(body) > BODY_MAX_CHARS else OK


def check_library(library) -> Decision:
    return OK if _v(library) in LIBRARY else _no("library_invalid")


def check_section_title(title) -> Decision:
    return check_title(title, SECTION_TITLE_MAX, "section_title_invalid")


def check_section_place(section_id: Optional[int], parent, has_children: bool) -> Decision:
    """Where a section goes (parent None: the top level); at most SECTION_DEPTH levels. section_invalid - under
    itself; section_too_deep - under a subsection, or a section with subsections put under another one."""
    if parent is None:
        return OK
    if section_id is not None and parent.id == section_id:
        return _no("section_invalid")
    if parent.parent_id is not None or has_children:
        return _no("section_too_deep")
    return OK


def check_order(current: Sequence[int], wanted: Sequence[int]) -> Decision:
    """A new order must be exactly the current set: no missing, extra or repeated ids (order_invalid)."""
    if len(wanted) != len(set(wanted)) or set(wanted) != set(current):
        return _no("order_invalid")
    return OK


def order_problems(current: Sequence[int], wanted: Sequence[int]) -> dict:
    """What is wrong with an order, for the refusal's data."""
    seen, repeated = set(), []
    for x in wanted:
        if x in seen and x not in repeated:
            repeated.append(x)
        seen.add(x)
    return {"missing": [x for x in current if x not in seen], "extra": [x for x in dict.fromkeys(wanted)
                                                                       if x not in set(current)],
            "repeated": repeated}


# --------------------------------------------------------------------------- who opens a material
@dataclass(frozen=True)
class ItemBasis:
    """A feed item that may give a basis: a material item of this material (feed) or an item linking it (item_link),
    with what access.item_listed needs."""
    item: object
    in_audience: bool
    links: bool = False  # True: the item links the material in its text; False: a material item of it
    contest: object = None  # a contest item: its contest, the participation, in a group of the contest
    participation: object = None
    in_contest_group: bool = False


@dataclass(frozen=True)
class ProblemBasis:
    """A contest problem whose version's statement links the material, with what problem_visible needs."""
    contest: object
    contest_problem: object
    participation: object = None
    in_group: bool = False


def in_library(user, material, library_group_ids: Iterable[int], live_group_ids: Iterable[int]) -> bool:
    """The library («Справочник») shows the material to the student: not archived, and all_students or a group of
    the library they are in now (live_group_ids: their current groups that are not archived). Admins see every
    material there, archived ones marked."""
    if access.is_admin(user):
        return True
    if material.archived_at is not None:
        return False
    library = _v(material.library)
    if library == "all_students":
        return True
    return library == "groups" and bool(set(library_group_ids) & set(live_group_ids))


def open_basis(user, material, now: datetime, *, library_group_ids: Iterable[int] = (),
               live_group_ids: Iterable[int] = (), items: Iterable[ItemBasis] = (),
               problems: Iterable[ProblemBasis] = ()) -> Optional[str]:
    """The first basis on which the user opens the latest version of the material (the order of the module
    docstring), None: not at all. Admins: "admin"."""
    if access.is_admin(user):
        return "admin"
    if in_library(user, material, library_group_ids, live_group_ids):
        return LIBRARY_BASIS
    items = list(items)
    for links in (False, True):
        for b in items:
            if b.links == links and access.item_listed(user, b.item, now, b.in_audience, contest=b.contest,
                                                       participation=b.participation,
                                                       in_contest_group=b.in_contest_group):
                if links or _v(b.item.kind) == "material":
                    return ITEM_LINK_BASIS if links else FEED_BASIS
    for p in problems:
        if contest_access.problem_visible(user, p.contest, p.contest_problem, p.participation, now, p.in_group):
            return PROBLEM_LINK_BASIS
    return None


def can_open(user, material, now: datetime, **bases) -> bool:
    return open_basis(user, material, now, **bases) is not None


# --------------------------------------------------------------------------- links for rendering
def resolve(user, slugs: Sequence[str], found: Mapping[str, tuple]) -> list[dict]:
    """slug -> material for the links of a text, one entry per distinct slug in order. `found`: slug -> (material,
    opens) for the slugs that have a material; `opens`: can_open for this reader. A student gets {slug, available}
    plus id and title only when available: no material, an archived one without a basis and one without access look
    the same (neither existence nor title leaks). An admin gets the status: ok, archived or unknown."""
    out = []
    for slug in dict.fromkeys(slugs):
        material, opens = found.get(slug, (None, False))
        if access.is_admin(user):
            if material is None:
                out.append({"slug": slug, "status": "unknown"})
            else:
                out.append({"slug": slug, "status": "archived" if material.archived_at is not None else "ok",
                            "id": material.id, "title": material.title})
        elif material is not None and opens:
            out.append({"slug": slug, "available": True, "id": material.id, "title": material.title})
        else:
            out.append({"slug": slug, "available": False})
    return out
