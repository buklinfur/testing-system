#!/usr/bin/env python3
"""Validate a problem package (format_version 1).

Usage:
    python tools/validate_problem.py <package_dir> [--schema protocol/schemas/problem.schema.json]

The checks are those of the importer (protocol/problem_package.py):
  1. problem.yaml against the JSON Schema (jsonschema is required: without it this is an error);
  2. semantic rules the schema cannot express: files exist, test masks match (no '..' or absolute masks),
     answer files are present where the checker needs them, group dependencies are consistent, choice
     questions are well formed, no symbolic links or special files, and so on.

Exit code: 0 if there are no errors (warnings are allowed), 1 otherwise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from protocol.problem_package import SCHEMA_PATH, PackageError, load  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("package_dir", type=Path)
    parser.add_argument("--schema", type=Path, default=SCHEMA_PATH)
    args = parser.parse_args()

    root: Path = args.package_dir
    if not (root / "problem.yaml").is_file():
        print(f"error: {root / 'problem.yaml'} not found", file=sys.stderr)
        return 1
    try:
        package = load(root, schema_path=args.schema)
        errors, warnings = [], package.warnings
    except PackageError as exc:
        errors, warnings = exc.problems, exc.warnings

    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    print(f"{root.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
