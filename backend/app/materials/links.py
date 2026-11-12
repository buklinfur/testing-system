"""Links to materials inside Markdown texts (4v.4, docs/CLASSROOM.md §11, D-083): the grammar, pure functions.

A link is `[[памятка:slug]]` or `[[памятка:slug|text]]`:
* the prefix is the word «памятка» in any letter case, followed by a colon; nothing else is a link (no spaces
  inside the brackets around the prefix and the slug);
* the slug is the slug of a material: lower-case Latin letters, digits and hyphens, 2..63 characters, starting with a
  letter or a digit (rules.SLUG_RE, the same as a problem's);
* the text (optional) is what a reader sees instead of the material's title: 1..LABEL_MAX characters after stripping,
  without a line break, `[`, `]`, `|`, control or format characters.

The same grammar everywhere: problem statements, the body of a feed item, the text of a material; the web UI of
module 5 renders with it too. Code spans and code blocks of the Markdown are not exempt: the grammar is one regular
expression, not a Markdown parser, so the server and the UI cannot disagree about where a link is.

Something that starts like a link (`[[памятка:` ... `]]` on one line) but does not match the grammar is not a link;
the texts are stored as they are and a teacher gets a warning (`malformed`). A slug no material has is a warning
too, never a refusal: the link is resolved when it is read, so a material created later makes it work.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

PREFIX = "памятка"
LABEL_MAX = 200
SLUG_PATTERN = r"[a-z0-9][a-z0-9-]{1,62}"  # rules.SLUG_RE; a test keeps them equal
# a link exactly; (?i) only for the prefix, the slug stays lower case
LINK_RE = re.compile(r"\[\[(?i:" + PREFIX + r"):(" + SLUG_PATTERN + r")(?:\|([^\[\]|\n]+))?\]\]")
# anything that looks like a link was meant: the prefix after "[[" (spaces allowed) up to the nearest "]]" on the
# same line; every such candidate that is not exactly a link is reported as malformed
CANDIDATE_RE = re.compile(r"\[\[[ \t]*(?i:" + PREFIX + r")[ \t]*:[^\n]*?\]\]")
BAD_LABEL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclass(frozen=True)
class Ref:
    """One link: the slug, the text a reader sees (None: the material's title), its place in the text."""
    slug: str
    label: Optional[str]
    start: int
    end: int


@dataclass(frozen=True)
class Malformed:
    """Something that looks like a link but is not one: the text as written and its place."""
    text: str
    start: int
    end: int


def _label(raw: Optional[str]) -> tuple[bool, Optional[str]]:
    """(valid, label): the label stripped; None when absent."""
    if raw is None:
        return True, None
    label = raw.strip()
    if not label or len(label) > LABEL_MAX:
        return False, None
    if any(unicodedata.category(ch) in BAD_LABEL_CATEGORIES for ch in label):
        return False, None
    return True, label


def scan(text: str) -> tuple[list[Ref], list[Malformed]]:
    """Every link of a text in order, and every candidate that is not a link."""
    refs, malformed = [], []
    for candidate in CANDIDATE_RE.finditer(text or ""):
        # the candidate ends at the first "]]"; a link is exactly the same characters
        match = LINK_RE.fullmatch(candidate.group(0))
        valid, label = _label(match.group(2)) if match else (False, None)
        if match and valid:
            refs.append(Ref(match.group(1), label, candidate.start(), candidate.end()))
        else:
            malformed.append(Malformed(candidate.group(0), candidate.start(), candidate.end()))
    return refs, malformed


def slugs(texts: Iterable[str]) -> list[str]:
    """The distinct slugs linked from the texts, in the order they first appear."""
    seen: dict[str, None] = {}
    for text in texts:
        for ref in scan(text)[0]:
            seen.setdefault(ref.slug, None)
    return list(seen)


UNKNOWN, ARCHIVED = "unknown", "archived"


def warnings(texts: Mapping[str, str], known: Mapping[str, bool]) -> list[str]:
    """Warnings for a teacher about the links of some texts, never refusals. `texts`: where -> text (where: a file
    of a package, "body", "statement ru" ...); `known`: slug -> archived, for every slug that has a material."""
    out = []
    for where, text in texts.items():
        refs, malformed = scan(text)
        for bad in malformed:
            out.append(f"{where}: {_short(bad.text)} is not a link to a material: write [[{PREFIX}:slug]] or "
                       f"[[{PREFIX}:slug|text]] (slug: a-z, 0-9 and '-')")
        for slug in dict.fromkeys(ref.slug for ref in refs):
            if slug not in known:
                out.append(f"{where}: [[{PREFIX}:{slug}]]: there is no material {slug}; students see the link as "
                           f"unavailable until it is created")
            elif known[slug]:
                out.append(f"{where}: [[{PREFIX}:{slug}]]: the material {slug} is archived; the link still works for "
                           f"students who may open it")
    return out


def _short(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"
