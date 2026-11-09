"""Files people uploaded are never served as pages (docs/FILES.md, D-070; D-017, D-026): the type by the content and
the name together, text only when it is UTF-8, the headers of every answer, If-None-Match, the saved name, and that
the file is streamed rather than read whole."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.content_files import (HEAD_BYTES, NAME_MAX, OCTET, TEXT, ZIP, ascii_name, clean_name,  # noqa: E402
                               decide, disposition, etag_matches, file_response, kind_of, looks_like_text, sniff)
from app.api_errors import ApiError  # noqa: E402
from app.blobstore import BlobStore  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 40
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
ZIPPED = b"PK\x03\x04" + b"\x00" * 40
HTML = b"<html><script>alert(1)</script></html>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'


class SniffTests(unittest.TestCase):
    def test_signatures(self):
        for data, kind in ((PNG, "png"), (JPEG, "jpeg"), (WEBP, "webp"), (ZIPPED, "zip"), (GIF, None), (HTML, None),
                           (SVG, None), (b"RIFF\x00\x00\x00\x00WAVE", None), (b"", None)):
            with self.subTest(kind=kind, head=data[:8]):
                self.assertEqual(sniff(data), kind)

    def test_text_is_utf8_without_nul(self):
        self.assertTrue(looks_like_text("тест 1 2\n".encode()))
        self.assertFalse(looks_like_text(b"1 2\x00 3"))  # a NUL: binary
        self.assertFalse(looks_like_text("тест".encode("cp1251"), complete=True))  # cp1251 is not UTF-8
        cut = ("a" + "я" * 4097).encode()[:HEAD_BYTES]  # an 8 KB head that ends in the middle of a letter
        self.assertEqual(cut[-1], "я".encode()[0], "the head really ends inside a letter")
        self.assertTrue(looks_like_text(cut), "a sequence cut by the end of the head is fine")
        self.assertFalse(looks_like_text(cut, complete=True), "but not at the real end of the file")
        self.assertFalse(looks_like_text(b"\xff\xfe broken"))


class DecideTests(unittest.TestCase):
    def test_pictures_only_when_bytes_and_name_agree(self):
        cases = {
            ("pic.png", PNG): ("image/png", "inline"), ("photo.JPG", JPEG): ("image/jpeg", "inline"),
            ("a.jpeg", JPEG): ("image/jpeg", "inline"), ("b.webp", WEBP): ("image/webp", "inline"),
            ("photo.jpg", PNG): ("image/png", "inline"),  # the type of the bytes, not of the name
            ("statement/pic.png", HTML): OCTET, ("pic.jpg", SVG): OCTET, ("pic.webp", HTML): OCTET,  # lies
            ("logo.svg", SVG): OCTET, ("page.html", HTML): OCTET, ("anim.gif", GIF): OCTET,  # D-026
            ("pic.txt", PNG): OCTET, ("tests/01.in", PNG): OCTET,  # picture bytes under a text name
            ("noext", PNG): OCTET, ("noext", b"hello"): OCTET,
        }
        for (name, head), want in cases.items():
            with self.subTest(name=name, head=head[:6]):
                self.assertEqual(decide(name, head, complete=True), want)

    def test_text(self):
        self.assertEqual(decide("tests/01.in", b"1 2\n", complete=True), TEXT)
        self.assertEqual(decide("statement/ru.md", "# Задача".encode(), complete=True), TEXT)
        self.assertEqual(decide("tests/01.in", b"1\x002", complete=True), OCTET)  # NUL
        self.assertEqual(decide("statement/ru.md", "Задача".encode("cp1251"), complete=True), OCTET)
        self.assertEqual(decide("data.bin", b"plain words", complete=True), OCTET)  # no text name: octet
        # a submission's source: text whatever its name, but only if it is UTF-8
        self.assertEqual(decide("submission-1.py", b"print(1)\n", text=True, complete=True), TEXT)
        self.assertEqual(decide("submission-1.py", "привет".encode("cp1251"), text=True, complete=True), OCTET)
        self.assertEqual(decide("submission-1.py", PNG, text=True, complete=True), OCTET)

    def test_archives(self):
        self.assertEqual(decide("submission-1.zip", ZIPPED, archive=True), ZIP)
        self.assertEqual(decide("submission-1.zip", HTML, archive=True), OCTET)  # not a zip under that name
        self.assertEqual(decide("submission-1.zip", PNG, archive=True), OCTET)

    def test_the_name_only_rule_is_never_wider(self):
        self.assertEqual(kind_of("a.gif"), OCTET)
        self.assertEqual(kind_of("a.svg"), OCTET)
        self.assertEqual(kind_of("tests/01.in"), TEXT)


class NameTests(unittest.TestCase):
    def test_clean_name(self):
        cases = {
            "задача.png": "задача.png", "../../etc/passwd": "passwd", "a\\b\\c.txt": "c.txt",
            'he said "hi".txt': "he said hi.txt", "a;b.txt": "ab.txt", "line\r\nbreak.txt": "linebreak.txt",
            "evil‮gnp.exe": "evilgnp.exe",  # direction marks are format characters: gone
            "100%.txt": "100%.txt", "smile 🙂.png": "smile 🙂.png", "": "file", "..": "file", "/": "file",
            "\x00\x07": "file",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_name(raw), want)
        long = clean_name("я" * 300 + ".png")
        self.assertEqual((len(long), long[-4:]), (NAME_MAX, ".png"))
        self.assertEqual(len(clean_name("x" * 300)), NAME_MAX)

    def test_ascii_fallback_and_header(self):
        self.assertEqual(ascii_name("Задача 1.png"), "Zadacha_1.png")
        self.assertEqual(ascii_name("щука.txt"), "shchuka.txt")
        self.assertEqual(ascii_name("🙂🙂.png"), "file.png")
        self.assertEqual(ascii_name("🙂"), "file")
        header = disposition("attachment", "../Задача \"1\";\r\n.png")
        self.assertEqual(header, "attachment; filename=\"Zadacha_1.png\"; "
                                 "filename*=UTF-8''%D0%97%D0%B0%D0%B4%D0%B0%D1%87%D0%B0%201.png")
        for bad in ("\r", "\n", "\\", ";", "/.."):
            self.assertNotIn(bad, header.split("filename*=")[0].split("; ", 1)[1].replace("; ", ""))
        self.assertEqual(disposition("inline", "100%.png").split("filename*=")[1], "UTF-8''100%25.png")


class EtagTests(unittest.TestCase):
    def test_if_none_match(self):
        tag = "a" * 64
        for header, want in ((f'"{tag}"', True), (f'W/"{tag}"', True), (f'"x", "{tag}"', True), ("*", True),
                             (f'"x", W/"y"', False), (tag, False), ("", False), (None, False), ('"aaa', False)):
            with self.subTest(header=header):
                self.assertEqual(etag_matches(header, tag), want)


class FakeRequest:
    def __init__(self, headers=None):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


class CountingStore(BlobStore):
    """Records how the file is read: file_response may read the head, then stream - never the whole file."""

    def __init__(self, root):
        super().__init__(root)
        self.reads, self.whole = [], 0

    def get(self, sha256):
        self.whole += 1
        return super().get(sha256)

    def open(self, sha256):
        fh = super().open(sha256)
        reads = self.reads

        class Spy:
            def read(self, n=-1):
                reads.append(n)
                return fh.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                fh.close()
        return None if fh is None else Spy()


def body_of(response) -> bytes:
    async def collect():
        return b"".join([chunk async for chunk in response.body_iterator])
    return asyncio.run(collect())


class ResponseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blobs = CountingStore(tmp.name)

    def put(self, data: bytes) -> str:
        return self.blobs.put(data)

    def assert_safety(self, r, sha):
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        self.assertEqual(r.headers["content-security-policy"], "sandbox; default-src 'none'")
        self.assertEqual(r.headers["cache-control"], "private, no-cache")
        self.assertEqual(r.headers["etag"], f'"{sha}"')
        self.assertIn("filename*=UTF-8''", r.headers["content-disposition"])

    def test_headers_on_200_and_304(self):
        sha = self.put(PNG)
        r = file_response(self.blobs, sha, "statement/pic.png", request=FakeRequest())
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "image/png"))
        self.assertTrue(r.headers["content-disposition"].startswith('inline; filename="pic.png"'))
        self.assert_safety(r, sha)
        self.assertEqual(body_of(r), PNG)
        for header in (f'"{sha}"', f'W/"{sha}"', f'"other", "{sha}"', "*"):
            with self.subTest(header=header):
                r = file_response(self.blobs, sha, "statement/pic.png", request=FakeRequest({"If-None-Match": header}))
                self.assertEqual((r.status_code, r.body), (304, b""))
                self.assert_safety(r, sha)
                self.assertEqual(r.headers["content-type"], "image/png")
        r = file_response(self.blobs, sha, "pic.png", request=FakeRequest({"If-None-Match": '"stale"'}))
        self.assertEqual(r.status_code, 200)

    def test_streamed_never_read_whole(self):
        data = b"1 2\n" * (3 * 1024 * 1024 // 4)  # 3 MB of text
        sha = self.put(data)
        r = file_response(self.blobs, sha, "tests/big.in")
        self.assertEqual(r.headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(self.blobs.whole, 0)
        self.assertEqual(self.blobs.reads, [HEAD_BYTES, 1], "only the head (and one byte to know there is more)")
        self.assertEqual(body_of(r), data)
        self.assertEqual(self.blobs.whole, 0)

    def test_compatible_calls_and_a_missing_blob(self):
        src = self.put(b"print(1)\n")
        r = file_response(self.blobs, src, "submission-1.py", text=True)  # the call of admin_api/submissions
        self.assertEqual((r.headers["content-type"], r.headers["content-disposition"][:11]),
                         ("text/plain; charset=utf-8", "attachment;"))
        archive = self.put(ZIPPED)
        r = file_response(self.blobs, archive, "submission-2.zip", media_type="application/zip", kind="attachment")
        self.assertEqual(r.headers["content-type"], "application/zip")
        fake = self.put(HTML)
        r = file_response(self.blobs, fake, "submission-3.zip", media_type="application/zip", kind="attachment")
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        with self.assertRaises(ApiError) as caught:
            file_response(self.blobs, "0" * 64, "x.txt")
        self.assertEqual(caught.exception.code, "file_missing")


if __name__ == "__main__":
    unittest.main()
