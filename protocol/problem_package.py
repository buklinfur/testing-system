"""Problem package (docs/PROBLEM_FORMAT.md): validation (R28) and the canonical view of its files.

Shared by the backend importer, tools/validate_problem.py and tools/pack-problem.sh; it imports neither the
backend nor the runner nor the worker (tests/test_layers_units.py).

* `list_files` walks a package directory: junk is skipped, symlinks and special files are errors.
* Test masks are matched against that file list, never against the file system, so a mask cannot reach
  outside the package; `..`, absolute paths and backslashes in masks are errors.
* `content_digest` is the identity of a version (R29): sha256 over the sorted "path, sha256" of every file.
* jsonschema is required: without it validation is an error, not a skipped step.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from .languages import SYSTEM_LANGUAGES, language_of

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "problem.schema.json"
DESCRIPTOR = "problem.yaml"
JUNK_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
JUNK_DIRS = frozenset({"__MACOSX", ".git", "__pycache__"})
ANSWER_REQUIRED_CHECKERS = frozenset({"exact", "tokens", "float"})
JURY_LANGUAGE_DEFAULT = "cpp"  # as in runner/package.py
PROGRAM_PATH_MAX = 128  # programs.name


class PackageError(Exception):
    """The package is not valid; `problems` lists every reason, `code` names the kind for an API."""

    def __init__(self, problems: list[str], warnings: Optional[list[str]] = None, code: str = "package_invalid"):
        self.problems = list(problems)
        self.warnings = list(warnings or [])
        self.code = code
        super().__init__("the problem package is not valid:\n  - " + "\n  - ".join(self.problems))


# ----------------------------------------------------------------------------- files
def is_junk(rel: str) -> bool:
    """Files that never belong to a package (operating system and editor leftovers): not hashed, not stored."""
    parts = rel.split("/")
    if any(part in JUNK_DIRS for part in parts):
        return True
    name = parts[-1]
    return (name in JUNK_NAMES or name.endswith(":Zone.Identifier") or name.endswith(".pyc")
            or name.endswith("~"))


def list_files(root: Path) -> list[str]:
    """Sorted relative POSIX paths of the package files without junk. PackageError for symlinks and special files."""
    root = Path(root)
    found, problems = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            rel = (here / name).relative_to(root).as_posix()
            if (here / name).is_symlink():
                problems.append(f"{rel}: symbolic links are not allowed")
                dirnames.remove(name)
            elif is_junk(rel):
                dirnames.remove(name)
        for name in filenames:
            rel = (here / name).relative_to(root).as_posix()
            if is_junk(rel):
                continue
            mode = os.lstat(here / name).st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{rel}: symbolic links are not allowed")
            elif not stat.S_ISREG(mode):
                problems.append(f"{rel}: only regular files are allowed")
            else:
                found.append(rel)
    if problems:
        raise PackageError(sorted(problems))
    return sorted(found)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_digest(files: Mapping[str, str]) -> str:
    """{path: sha256} -> the content identity of a version (independent of order and of junk)."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{files[path]}\n".encode("utf-8"))
    return h.hexdigest()


# ----------------------------------------------------------------------------- statement pictures
PICTURE_DIR = "statement/images/"


def picture_kind(head: bytes) -> Optional[str]:
    """"png" / "jpeg" / "webp" by the signature, or None - the same test as the server's when it serves the file
    (backend app/content_files.sniff, docs/FILES.md; a unit test keeps the two in step)."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def picture_warnings(root: Path, files: list) -> list:
    """statement/images/* that are not PNG, JPEG or WebP by their content (В3, D-071): a warning, never an error -
    the package imports, but students would get such a file as a download instead of seeing it in the statement."""
    out = []
    for rel in files:
        if rel.startswith(PICTURE_DIR):
            with open(Path(root) / rel, "rb") as f:
                head = f.read(16)
            if picture_kind(head) is None:
                out.append(f"{rel}: not a PNG, JPEG or WebP picture by its content: students will get it as a "
                           "download, not see it in the statement; save it as PNG")
    return out


# ----------------------------------------------------------------------------- masks
def _mask_regex(pattern: str) -> re.Pattern:
    """A glob mask as Path.glob reads it: `*`, `?`, `[...]` stay inside one path part, `**/` spans parts."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 1:]:
            end = pattern.index("]", i + 1)
            body = pattern[i + 1:end]
            body = body.replace("\\", "\\\\")  # ranges like 0-9 stay ranges; only backslashes are escaped
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
            i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def mask_problem(pattern: str) -> Optional[str]:
    """Why a test mask is refused, or None."""
    if not isinstance(pattern, str) or not pattern.strip():
        return "an empty mask"
    if "\\" in pattern:
        return f"backslashes are not allowed in masks: {pattern!r}"
    if pattern.startswith("/") or re.match(r"^[A-Za-z]:", pattern):
        return f"absolute masks are not allowed: {pattern!r}"
    if ".." in PurePosixPath(pattern).parts:
        return f"'..' is not allowed in masks: {pattern!r}"
    return None


def match_mask(pattern: str, files) -> list[str]:
    regex = _mask_regex(pattern)
    return sorted(f for f in files if regex.match(f))


# ----------------------------------------------------------------------------- the package model
@dataclass
class TestRef:
    __test__ = False
    name: str  # file stem, e.g. "small03"
    input: str  # relative path of the .in file
    answer: Optional[str]  # relative path of the .out file, if present


@dataclass
class GroupSpec:
    name: str
    points: str
    scoring: str
    visibility: str
    feedback: Optional[str]
    show_in_statement: bool
    depends_on: list
    tests: list = field(default_factory=list)


@dataclass
class ProgramRef:
    role: str  # checker | interactor | validator | solution
    path: str
    language: str
    expect: Optional[str] = None  # solutions: the expected verdict


@dataclass
class Package:
    root: Path
    data: dict
    files: list
    groups: list
    programs: list
    statements: dict  # language -> relative path of the Markdown statement
    warnings: list

    @property
    def slug(self) -> str:
        return self.data["slug"]

    @property
    def config(self) -> dict:
        """problem.yaml without test_groups and solutions: what the worker gets (docs/WORKER_PROTOCOL.md)."""
        return {k: v for k, v in self.data.items() if k not in ("test_groups", "solutions")}


def as_list(value):
    return value if isinstance(value, list) else [value]


def _load_descriptor(root: Path) -> dict:
    import yaml  # here: tools/pack-problem.sh needs only list_files and may run without PyYAML

    path = root / DESCRIPTOR
    if not path.is_file():
        raise PackageError([f"{DESCRIPTOR} not found"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise PackageError([f"{DESCRIPTOR} is not valid YAML: {exc}"]) from exc
    if not isinstance(data, dict):
        raise PackageError([f"{DESCRIPTOR} must contain a mapping at the top level"])
    return data


def validate_schema(data: dict, schema_path: Path = SCHEMA_PATH) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed: the package cannot be checked against the schema "
                "(pip install -r requirements.txt)"]
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    return [f"schema: {'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path))]


def find_cycle(deps: dict) -> Optional[list]:
    state: dict = {}
    stack: list = []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for nxt in deps.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state.get(nxt) is None:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for name in deps:
        if state.get(name) is None:
            found = visit(name)
            if found:
                return found
    return None


def _semantics(data: dict, files: list, errors: list, warnings: list):
    """Rules the schema cannot express. Returns (groups, programs, statements)."""
    present = set(files)
    ptype = data.get("type")

    limits = data.get("limits") or {}
    if limits.get("wall_ms") and limits.get("time_ms") and limits["wall_ms"] < limits["time_ms"]:
        errors.append(f"limits.wall_ms ({limits['wall_ms']}) must not be less than limits.time_ms ({limits['time_ms']})")

    statements = data.get("statement") or {lang: f"statement/{lang}.md" for lang in data.get("title", {})}
    for lang, rel in statements.items():
        if rel not in present:
            errors.append(f"statement for language '{lang}' not found: {rel}")

    programs = []
    for role in ("interactor", "validator"):
        if role in data:
            programs.append(ProgramRef(role, data[role]["path"], data[role].get("language", JURY_LANGUAGE_DEFAULT)))
    checker = data.get("checker", {})
    if checker.get("type") == "custom":
        programs.append(ProgramRef("checker", checker["path"], checker.get("language", JURY_LANGUAGE_DEFAULT)))
    for sol in data.get("solutions", []):
        language = language_of(sol["path"])
        if language is None:
            errors.append(f"solution {sol['path']}: unknown language (by extension)")
        programs.append(ProgramRef("solution", sol["path"], language or "", sol.get("expect")))
    known = {key for key, _name in SYSTEM_LANGUAGES}
    for prog in programs:
        if prog.path not in present:
            errors.append(f"{prog.role} file not found: {prog.path}")
        if prog.language and prog.language not in known:
            errors.append(f"{prog.role} {prog.path}: unknown language {prog.language!r} (known: {', '.join(sorted(known))})")
        if len(prog.path) > PROGRAM_PATH_MAX:
            errors.append(f"{prog.role} {prog.path}: the path is longer than {PROGRAM_PATH_MAX} characters")

    groups_raw = data.get("test_groups", [])
    names = [g["name"] for g in groups_raw]
    if len(names) != len(set(names)):
        errors.append("duplicate test group names")
    deps = {g["name"]: g.get("depends_on", []) for g in groups_raw}
    for name, ds in deps.items():
        for d in ds:
            if d not in names:
                errors.append(f"group '{name}' depends on unknown group '{d}'")
    cycle = find_cycle({k: [d for d in v if d in names] for k, v in deps.items()})
    if cycle:
        errors.append("dependency cycle between groups: " + " -> ".join(cycle))

    needs_answer = ptype in ("batch", "output_only") and checker.get("type") in ANSWER_REQUIRED_CHECKERS
    seen: dict = {}
    total_points = 0.0
    groups = []
    for g in groups_raw:
        total_points += g.get("points", 0)
        matched: set = set()
        for pattern in as_list(g["tests"]):
            refused = mask_problem(pattern)
            if refused:
                errors.append(f"group '{g['name']}': {refused}")
                continue
            matched.update(match_mask(pattern, files))
        group = GroupSpec(name=g["name"], points=str(g.get("points", 0)), scoring=g.get("scoring", "sum"),
                          visibility=g.get("visibility", "hidden"), feedback=g.get("feedback"),
                          show_in_statement=bool(g.get("show_in_statement", False)),
                          depends_on=list(g.get("depends_on", [])))
        groups.append(group)
        if not matched:
            errors.append(f"group '{g['name']}': no test files match {g['tests']}")
            continue
        stems = set()
        for rel in sorted(matched):
            path = PurePosixPath(rel)
            if path.suffix != ".in":
                errors.append(f"group '{g['name']}': test file must end with .in: {rel}")
                continue
            if rel in seen:
                errors.append(f"test {rel} is in groups '{seen[rel]}' and '{g['name']}'")
            seen[rel] = g["name"]
            if path.stem in stems:
                errors.append(f"group '{g['name']}': two tests are named '{path.stem}' (in different folders)")
            stems.add(path.stem)
            answer = str(path.with_suffix(".out"))
            if needs_answer and answer not in present:
                errors.append(f"missing answer file {answer} (checker '{checker.get('type')}' needs it)")
            group.tests.append(TestRef(path.stem, rel, answer if answer in present else None))
        if g.get("show_in_statement") and not all(t.answer for t in group.tests):
            warnings.append(f"group '{g['name']}' is shown in the statement but has no .out files: "
                            "only inputs will be shown")
    if groups_raw and total_points not in (0, 100):
        warnings.append(f"total points of groups is {total_points:g}, usually 100")

    if ptype == "choice":
        qids = [q["id"] for q in data["questions"]]
        if len(qids) != len(set(qids)):
            errors.append("duplicate question ids")
        for q in data["questions"]:
            oids = [o["id"] for o in q["options"]]
            if len(oids) != len(set(oids)):
                errors.append(f"question '{q['id']}': duplicate option ids")
            for c in q["correct"]:
                if c not in oids:
                    errors.append(f"question '{q['id']}': correct option '{c}' does not exist")
    if ptype == "text_answer":
        aids = [a["id"] for a in data["answers"]]
        if len(aids) != len(set(aids)):
            errors.append("duplicate answer ids")
        for a in data["answers"]:
            if a.get("match") == "number" and "eps" not in a:
                warnings.append(f"answer '{a['id']}': match=number without eps compares numbers exactly")
    return groups, programs, statements


def load(root, files: Optional[list] = None, schema_path: Path = SCHEMA_PATH) -> Package:
    """Validate a package directory and describe it. PackageError lists every problem at once."""
    root = Path(root)
    files = list_files(root) if files is None else sorted(files)
    data = _load_descriptor(root)
    errors = validate_schema(data, schema_path)
    if errors:
        raise PackageError(errors)
    warnings: list = []
    groups, programs, statements = _semantics(data, files, errors, warnings)
    warnings += picture_warnings(root, files)  # warnings only: they never change the files or content_sha256
    if errors:
        raise PackageError(errors, warnings)
    return Package(root=root, data=data, files=files, groups=groups, programs=programs, statements=statements,
                   warnings=warnings)
