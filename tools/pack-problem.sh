#!/usr/bin/env bash
# Zip a problem package folder to stdout, for `judgectl problem import -` run as another user:
#   tools/pack-problem.sh problems-examples/sum-of-two | j .venv/bin/python -m app.judgectl problem import -
#   tools/pack-problem.sh <folder> > <folder>.zip
# The files are those the importer takes (protocol/problem_package.py: list_files): junk such as .DS_Store,
# Thumbs.db, *:Zone.Identifier, .git/ or __pycache__/ is left out, symbolic links and special files are an
# error. Entries sit in one top folder named like the package folder (the importer removes it); dates are
# fixed, so the same files give the same zip. `python3 -m zipfile -c - <folder>` cannot do this: it writes a
# file named "-".
set -euo pipefail
[ $# = 1 ] && [ -d "$1" ] || { echo "usage: $0 <package folder> > package.zip" >&2; exit 2; }
[ -t 1 ] && { echo "$0: stdout is a terminal: redirect it to a file or a pipe" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")/.." && pwd)
exec python3 -I - "$HERE" "$1" <<'PY'
import sys, zipfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from protocol.problem_package import PackageError, list_files
root = Path(sys.argv[2])
try:
    files = list_files(root)
except PackageError as exc:
    sys.exit(f"pack-problem: {exc}")
if "problem.yaml" not in files:
    sys.exit(f"pack-problem: {root}/problem.yaml not found")
top = root.resolve().name
with zipfile.ZipFile(sys.stdout.buffer, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in files:
        info = zipfile.ZipInfo(f"{top}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, (root / rel).read_bytes())
print(f"pack-problem: {len(files)} files of {top}", file=sys.stderr)
PY
