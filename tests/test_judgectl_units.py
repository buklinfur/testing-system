"""judgectl output without a database: core.terminal_text and the table cells (docs/JUDGECTL.md, «Вывод»).

Text typed by students or teachers (answers, comments, titles, names) must not steer the operator's terminal: ANSI
escape sequences, a carriage return that overwrites the line, other C0/C1 controls and the Unicode controls of text
direction are printed escaped.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

try:
    from app.judgectl.core import cell, terminal_text
    HAVE_APP = True
except ImportError:  # pragma: no cover - the backend's packages are not installed
    HAVE_APP = False

HOSTILE = "ok\x1b[2J\x1b]0;pwned\x07abc\rXYZ‮txt.exe\x00\x7f\x9b31m⁦‏\tend\nnext"


@unittest.skipUnless(HAVE_APP, "needs the backend's packages")
class TerminalTextTests(unittest.TestCase):
    def assert_no_controls(self, text, allowed=""):
        bad = [hex(ord(ch)) for ch in text if ch not in allowed and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                                     or 0x202A <= ord(ch) <= 0x202E
                                                                     or 0x2066 <= ord(ch) <= 0x2069
                                                                     or ch in "‎‏؜  ")]
        self.assertEqual(bad, [])

    def test_escapes_every_control(self):
        out = terminal_text(HOSTILE)
        self.assert_no_controls(out)
        self.assertEqual(out, "ok\\x1b[2J\\x1b]0;pwned\\x07abc\\x0dXYZ\\u202etxt.exe\\x00\\x7f\\x9b31m\\u2066\\u200f"
                              "\\x09end\\x0anext")

    def test_multiline_keeps_newlines_and_tabs_only(self):
        out = terminal_text(HOSTILE, multiline=True)
        self.assert_no_controls(out, allowed="\n\t")
        self.assertIn("\tend\nnext", out)
        self.assertIn("abc\\x0dXYZ", out)

    def test_plain_text_is_unchanged(self):
        for text in ("Блок-схема: «цикл»", "a\\b", "x" * 300, "7Б", "émoji 🙂", "‐dash"):
            self.assertEqual(terminal_text(text), text)

    def test_table_cells_are_escaped(self):
        self.assertEqual(cell("t\x1b[31mred\r"), "t\\x1b[31mred\\x0d")
        self.assertEqual(cell("one\ntwo"), "one\\x0atwo")  # a cell stays on its line
        self.assertEqual(cell(None), "-")
        self.assertEqual(cell(True), "yes")


if __name__ == "__main__":
    unittest.main()
