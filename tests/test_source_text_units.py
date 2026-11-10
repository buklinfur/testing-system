"""No literal text-direction controls anywhere in the repository (Trojan Source, CVE-2021-42574).

A direction control (U+202A..U+202E embeddings and overrides, U+2066..U+2069 isolates, U+200E/U+200F marks, U+061C
the Arabic letter mark) written literally makes an editor show the text in another order than the interpreter or
the reader of a diff sees it. Tests that need such a character write it as an escape (\\u202e). The check covers
every file git knows (tracked, and new files not ignored) with the suffixes below.

EXCEPTIONS lists the files that must hold such a character literally, with the characters and the reason; an entry
that is no longer needed fails too, so the list stays short.
"""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".sh", ".md", ".yaml", ".yml", ".json", ".sql"}
DIRECTION = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)] + ["\u200e", "\u200f", "\u061c"])

# path relative to ROOT -> (the characters allowed literally in it, why)
EXCEPTIONS: dict[str, tuple[frozenset, str]] = {}


def repository_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    names = sorted({n for n in out.decode("utf-8").split("\0") if n})
    return [ROOT / n for n in names if Path(n).suffix.lower() in SUFFIXES and (ROOT / n).is_file()]


def direction_controls(path: Path) -> list[tuple[int, str]]:
    """(line number, U+XXXX) of every direction control in the file."""
    text = path.read_bytes().decode("utf-8", errors="replace")
    return [(no, f"U+{ord(ch):04X}") for no, line in enumerate(text.splitlines(), 1) for ch in line
            if ch in DIRECTION]


class DirectionControlTests(unittest.TestCase):
    def test_the_set_is_the_agreed_one(self):
        self.assertEqual(len(DIRECTION), 5 + 4 + 3)
        self.assertEqual({f"{ord(c):04X}" for c in DIRECTION},
                         {"202A", "202B", "202C", "202D", "202E", "2066", "2067", "2068", "2069", "200E", "200F",
                          "061C"})

    def test_the_scan_sees_the_files(self):
        files = {p.relative_to(ROOT).as_posix() for p in repository_files()}
        for expected in ("tests/test_source_text_units.py", "backend/app/models.py", "docs/CLASSROOM.md",
                         "deploy/setup-postgres.sh"):
            self.assertIn(expected, files)

    def test_the_scan_finds_a_literal_one(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.py"
            p.write_text('s = "abc' + "\u202e" + 'def"\nt = 1\n', encoding="utf-8")
            self.assertEqual(direction_controls(p), [(1, "U+202E")])
            p.write_text('s = "abc\\u202edef"\n', encoding="utf-8")
            self.assertEqual(direction_controls(p), [])

    def test_no_literal_direction_controls(self):
        found = {}
        for path in repository_files():
            rel = path.relative_to(ROOT).as_posix()
            allowed = EXCEPTIONS.get(rel, (frozenset(), ""))[0]
            hits = [(no, code) for no, code in direction_controls(path) if chr(int(code[2:], 16)) not in allowed]
            if hits:
                found[rel] = hits
        self.assertEqual(found, {}, "write these characters as escapes (\\u202e) or list the file in EXCEPTIONS")

    def test_every_exception_is_still_needed(self):
        for rel, (chars, why) in EXCEPTIONS.items():
            with self.subTest(rel):
                self.assertTrue(why)
                present = {chr(int(code[2:], 16)) for _, code in direction_controls(ROOT / rel)}
                self.assertTrue(chars <= present, f"{rel}: the exception is no longer needed")


if __name__ == "__main__":
    unittest.main()
