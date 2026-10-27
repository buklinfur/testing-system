#!/usr/bin/env python3
"""Write hostile problem archives for a manual check of the importer (docs/PROBLEM_IMPORT.md).

    python3 tools/make-bad-archives.py <new folder>

Each archive is the valid package problems-examples/sum-of-two plus one trap, so a refusal can only come from
the trap. Every one must be refused with the reason printed next to it, and nothing may be written outside
the temporary folder of the import. Standard library only; nothing here is executed by the importer.
"""
from __future__ import annotations

import io
import os
import stat
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "problems-examples" / "sum-of-two"


def base_entries():
    return [(f"sum-of-two/{p.relative_to(BASE).as_posix()}", p.read_bytes())
            for p in sorted(BASE.rglob("*")) if p.is_file()]


def build(extra) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in base_entries() + list(extra):
            z.writestr(name, data)
    return buf.getvalue()


def typed(name: str, kind: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (kind | 0o777) << 16
    return info


def lying_header() -> bytes:
    """The sizes in both headers of the last entry claim 10 bytes; the stored data is longer."""
    data = bytearray(build([("sum-of-two/tests/zz.in", os.urandom(5000))]))
    local = data.rfind(b"PK\x03\x04")
    central = data.rfind(b"PK\x01\x02")
    struct.pack_into("<I", data, local + 22, 10)
    struct.pack_into("<I", data, central + 24, 10)
    return bytes(data)


ARCHIVES = {
    "zip-slip.zip": ("'..' in a name", lambda: build([("sum-of-two/../../evil.txt", b"owned")])),
    "absolute-path.zip": ("an absolute path", lambda: build([("/tmp/evil.txt", b"owned")])),
    "backslash.zip": ("a Windows path with '..'", lambda: build([("..\\..\\evil.txt", b"owned")])),
    "symlink.zip": ("a symbolic link to /etc/passwd",
                    lambda: build([(typed("sum-of-two/tests/zz.in", stat.S_IFLNK), b"/etc/passwd")])),
    "fifo.zip": ("a named pipe", lambda: build([(typed("sum-of-two/tests/pipe.in", stat.S_IFIFO), b"")])),
    "zip-bomb.zip": ("16 MB of zeros (ratio about 1000, the limit is 100)",
                     lambda: build([("sum-of-two/tests/zz.in", b"0" * (16 * 1024 * 1024))])),
    "too-many-files.zip": ("5001 entries (the limit is 5000)",
                           lambda: build([(f"sum-of-two/junk/{i}.txt", b"x") for i in range(5001)])),
    "lying-header.zip": ("sizes in the headers smaller than the data", lying_header),
    "not-a-zip.zip": ("not a zip at all", lambda: b"this is plain text, not an archive\n"),
}


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=False)
    for name, (what, make) in ARCHIVES.items():
        (out / name).write_bytes(make())
        print(f"{out / name}: {what}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
