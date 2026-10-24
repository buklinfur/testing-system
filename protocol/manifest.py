"""Problem manifest: everything a worker needs to judge a problem version, referencing files by sha256.

The backend builds it from the database, the worker turns it into a directory the runner can load.
Both sides use the same canonicalisation, so `manifest_digest` is the cache key on the worker and
changes whenever any test, limit or jury program changes.
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from typing import Optional

MANIFEST_FORMAT = 1
_HEX64 = re.compile(r"[0-9a-f]{64}")
PROGRAM_ROLES_USED_BY_WORKER = ("checker", "interactor")


class ManifestError(Exception):
    pass


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def manifest_digest(manifest: dict) -> str:
    return hashlib.sha256(canonical_json(manifest)).hexdigest()


def _points(value) -> str:
    return format(Decimal(str(value)).quantize(Decimal("0.01")), "f")


def assemble_manifest(version: dict, groups: list, programs: list) -> dict:
    """version: {id, slug, type, titles, config}; groups: dicts with a `tests` list; programs: dicts.

    `config` is the normalized problem.yaml *without* test_groups (tests live in `groups`).
    """
    out_groups = []
    for g in sorted(groups, key=lambda g: g["position"]):
        tests = []
        for t in sorted(g.get("tests", []), key=lambda t: t["position"]):
            tests.append({
                "id": t["id"], "position": t["position"], "name": t["name"],
                "input_sha256": t["input_sha256"], "answer_sha256": t.get("answer_sha256"),
            })
        out_groups.append({
            "id": g["id"], "position": g["position"], "name": g["name"],
            "points": _points(g.get("points", 0)), "scoring": g.get("scoring", "sum"),
            "visibility": g.get("visibility", "hidden"), "feedback": g.get("feedback"),
            "show_in_statement": bool(g.get("show_in_statement", False)),
            "depends_on": list(g.get("depends_on") or []), "tests": tests,
        })
    out_programs = [
        {"role": p["role"], "name": p["name"], "language": p["language"], "source_sha256": p["source_sha256"]}
        for p in sorted(programs, key=lambda p: (p["role"], p["name"]))
        if p["role"] in PROGRAM_ROLES_USED_BY_WORKER
    ]
    return {
        "format": MANIFEST_FORMAT, "version_id": version["id"], "slug": version["slug"], "type": version["type"],
        "titles": dict(version.get("titles") or {}), "config": version.get("config") or {},
        "groups": out_groups, "programs": out_programs,
    }


def validate_manifest(manifest: dict) -> None:
    """Defensive checks on the worker side: the manifest comes over the network."""
    if manifest.get("format") != MANIFEST_FORMAT:
        raise ManifestError(f"unsupported manifest format: {manifest.get('format')!r}")
    for key in ("version_id", "slug", "type", "titles", "config", "groups", "programs"):
        if key not in manifest:
            raise ManifestError(f"manifest has no {key!r}")

    def sha(value: Optional[str], what: str, allow_none: bool = False) -> None:
        if value is None and allow_none:
            return
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            raise ManifestError(f"bad sha256 for {what}: {value!r}")

    names = set()
    for g in manifest["groups"]:
        if g["name"] in names:
            raise ManifestError(f"duplicate group name {g['name']!r}")
        names.add(g["name"])
        seen = set()
        for t in g["tests"]:
            if t["name"] in seen:
                raise ManifestError(f"duplicate test name {t['name']!r} in group {g['name']!r}")
            seen.add(t["name"])
            sha(t["input_sha256"], f"input of {t['name']}")
            sha(t["answer_sha256"], f"answer of {t['name']}", allow_none=True)
    for p in manifest["programs"]:
        sha(p["source_sha256"], f"program {p['name']}")
