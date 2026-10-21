#!/usr/bin/env python3
"""Validate a problem package (format_version 1).

Usage:
    python tools/validate_problem.py <package_dir> [--schema docs/schemas/problem.schema.json]

Checks:
  1. problem.yaml against the JSON Schema (needs `pip install jsonschema`;
     if it is not installed this step is skipped with a warning);
  2. semantic rules the schema cannot express: files exist, test globs match,
     answer files are present where the checker needs them, group dependencies
     are consistent, choice questions are well formed, and so on.

Exit code: 0 if there are no errors (warnings are allowed), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ANSWER_REQUIRED_CHECKERS = {"exact", "tokens", "float"}


def as_list(value):
    return value if isinstance(value, list) else [value]


def validate_schema(data: dict, schema_path: Path, errors: list[str], warnings: list[str]) -> None:
    try:
        import jsonschema
    except ImportError:
        warnings.append("jsonschema is not installed: schema validation skipped (pip install jsonschema)")
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        where = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"schema: {where}: {err.message}")


def find_cycle(deps: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
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


def validate_semantics(root: Path, data: dict, errors: list[str], warnings: list[str]) -> None:
    ptype = data.get("type")

    # Statement files
    statements = data.get("statement") or {lang: f"statement/{lang}.md" for lang in data.get("title", {})}
    for lang, rel in statements.items():
        if not (root / rel).is_file():
            errors.append(f"statement for language '{lang}' not found: {rel}")

    # Referenced programs
    refs = []
    for key in ("interactor", "validator"):
        if key in data:
            refs.append((key, data[key]["path"]))
    checker = data.get("checker", {})
    if checker.get("type") == "custom":
        refs.append(("checker", checker["path"]))
    for sol in data.get("solutions", []):
        refs.append(("solution", sol["path"]))
    for what, rel in refs:
        if not (root / rel).is_file():
            errors.append(f"{what} file not found: {rel}")

    # Test groups
    groups = data.get("test_groups", [])
    names = [g["name"] for g in groups]
    if len(names) != len(set(names)):
        errors.append("duplicate test group names")
    deps = {g["name"]: g.get("depends_on", []) for g in groups}
    for name, ds in deps.items():
        for d in ds:
            if d not in names:
                errors.append(f"group '{name}' depends on unknown group '{d}'")
    cycle = find_cycle({k: [d for d in v if d in names] for k, v in deps.items()})
    if cycle:
        errors.append("dependency cycle between groups: " + " -> ".join(cycle))

    needs_answer = ptype in ("batch", "output_only") and checker.get("type") in ANSWER_REQUIRED_CHECKERS
    seen: dict[Path, str] = {}
    total_points = 0.0
    for g in groups:
        total_points += g.get("points", 0)
        matched: set[Path] = set()
        for pattern in as_list(g["tests"]):
            matched.update(p for p in root.glob(pattern) if p.is_file())
        if not matched:
            errors.append(f"group '{g['name']}': no test files match {g['tests']}")
            continue
        for path in sorted(matched):
            if path.suffix != ".in":
                errors.append(f"group '{g['name']}': test file must end with .in: {path.relative_to(root)}")
            if path in seen:
                errors.append(f"test {path.relative_to(root)} is in groups '{seen[path]}' and '{g['name']}'")
            seen[path] = g["name"]
            answer = path.with_suffix(".out")
            if needs_answer and not answer.is_file():
                errors.append(f"missing answer file {answer.relative_to(root)} (checker '{checker.get('type')}' needs it)")
        if g.get("show_in_statement") and not all(p.with_suffix(".out").is_file() for p in matched):
            warnings.append(f"group '{g['name']}' is shown in the statement but has no .out files: only inputs will be shown")
    if groups and total_points not in (0, 100):
        warnings.append(f"total points of groups is {total_points:g}, usually 100")

    # Choice questions
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

    # Text answers
    if ptype == "text_answer":
        aids = [a["id"] for a in data["answers"]]
        if len(aids) != len(set(aids)):
            errors.append("duplicate answer ids")
        for a in data["answers"]:
            if a.get("match") == "number" and "eps" not in a:
                warnings.append(f"answer '{a['id']}': match=number without eps compares numbers exactly")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package_dir", type=Path)
    default_schema = Path(__file__).resolve().parent.parent / "docs" / "schemas" / "problem.schema.json"
    parser.add_argument("--schema", type=Path, default=default_schema)
    args = parser.parse_args()

    root: Path = args.package_dir
    descriptor = root / "problem.yaml"
    if not descriptor.is_file():
        print(f"error: {descriptor} not found", file=sys.stderr)
        return 1

    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        print(f"error: problem.yaml is not valid YAML: {exc}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("error: problem.yaml must contain a mapping at the top level", file=sys.stderr)
        return 1

    validate_schema(data, args.schema, errors, warnings)
    if not any(e.startswith("schema:") for e in errors):
        validate_semantics(root, data, errors, warnings)

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"{root.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
