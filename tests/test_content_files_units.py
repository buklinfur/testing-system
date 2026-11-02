"""Files people uploaded are never served as pages (MODULE_4A 5.7, D-017, D-026): which ones a browser may show."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.content_files import kind_of  # noqa: E402

OCTET = ("application/octet-stream", "attachment")
TEXT = ("text/plain; charset=utf-8", "attachment")


class KindTests(unittest.TestCase):
    def test_only_png_jpeg_and_webp_are_inline(self):
        cases = {
            "statement/pic.png": ("image/png", "inline"), "a/B.JPG": ("image/jpeg", "inline"),
            "c.jpeg": ("image/jpeg", "inline"), "d.webp": ("image/webp", "inline"),
            "anim.gif": OCTET,  # D-026: one rule for statements and attachments, GIF is an attachment
            "logo.svg": OCTET, "page.html": OCTET, "page.htm": OCTET, "x.xhtml": OCTET, "noext": OCTET,
            "tests/01.in": TEXT, "statement/ru.md": TEXT, "sol.cpp": TEXT,
        }
        for name, want in cases.items():
            with self.subTest(name):
                self.assertEqual(kind_of(name), want)
        self.assertEqual(kind_of("pic.png", text=True), TEXT)  # a submission's source is always text


if __name__ == "__main__":
    unittest.main()
