"""Safe unpacking of problem packages (R27), no database: hostile archives and folders are refused."""
import io
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.problems.unpack import ArchiveError, check_directory, unpack_zip  # noqa: E402
from app.settings import ImportSettings  # noqa: E402
from protocol.problem_package import PackageError  # noqa: E402

EXAMPLE = ROOT / "problems-examples" / "sum-of-two"
LIMITS = ImportSettings()


def make_zip(entries, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """entries: (name, bytes) or (ZipInfo, bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def special(name, kind) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.external_attr = (kind | 0o644) << 16
    return info


def zip_dir(root: Path, prefix: str = "") -> bytes:
    return make_zip([(prefix + p.relative_to(root).as_posix(), p.read_bytes())
                     for p in sorted(root.rglob("*")) if p.is_file()])


class UnpackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.n = 0

    def dest(self) -> Path:
        self.n += 1
        d = self.tmp / f"out{self.n}"
        d.mkdir()
        return d

    def refused(self, data, *fragments, limits=LIMITS):
        dest = self.dest()
        with self.assertRaises(ArchiveError) as ctx:
            unpack_zip(data, dest, limits)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        self.assertFalse((self.tmp / "evil").exists())
        return ctx.exception

    def test_a_package_folder_zipped_with_its_top_folder(self):
        files = unpack_zip(zip_dir(EXAMPLE, "sum-of-two/"), self.dest(), LIMITS)
        self.assertIn("problem.yaml", files)
        self.assertEqual(files, unpack_zip(zip_dir(EXAMPLE), self.dest(), LIMITS))  # without the top folder: same

    def test_names_that_leave_the_package(self):
        for name, why in (("../evil", "'..'"), ("a/../../evil", "'..'"), ("/etc/evil", "absolute"),
                          ("C:/evil", "absolute"), ("c:evil", "absolute"), ("..\\evil", "backslash"),
                          ("a\\..\\evil", "backslash"), ("a//b", "empty"), ("./a", "'.'")):
            with self.subTest(name=name):
                self.refused(make_zip([("problem.yaml", b"x"), (name, b"evil")]), why)

    def test_every_bad_name_is_listed_and_nothing_is_written(self):
        dest = self.dest()
        with self.assertRaises(ArchiveError) as ctx:
            unpack_zip(make_zip([("ok.txt", b"1"), ("../a", b"2"), ("/b", b"3")]), dest, LIMITS)
        self.assertEqual(len(ctx.exception.problems), 2)
        self.assertEqual(list(dest.iterdir()), [])  # names are checked before the first byte is written

    def test_symlinks_and_special_files(self):
        self.refused(make_zip([("problem.yaml", b"x"), (special("tests/link", stat.S_IFLNK), b"/etc/passwd")]),
                     "symbolic links")
        for kind in (stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK):
            with self.subTest(kind=kind):
                self.refused(make_zip([(special("tests/x", kind), b"")]), "only regular files")

    def test_duplicate_names(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z, self.assertWarns(UserWarning):
            z.writestr("a.txt", b"1")
            z.writestr("a.txt", b"2")
        self.refused(buf.getvalue(), "occurs twice")

    def test_too_many_files(self):
        limits = ImportSettings(max_files=10)
        self.refused(make_zip([(f"t/{i}.in", b"1") for i in range(11)]), "11 entries, the limit is 10", limits=limits)
        unpack_zip(make_zip([(f"t/{i}.in", b"1") for i in range(10)]), self.dest(), limits)

    def test_archive_too_big(self):
        self.refused(make_zip([("a", os.urandom(3000))]), "the limit is", limits=ImportSettings(max_archive_bytes=2000))

    def test_zip_bomb_by_ratio(self):
        bomb = make_zip([("problem.yaml", b"x"), ("tests/big.in", b"0" * (2 * 1024 * 1024))])
        self.refused(bomb, "tests/big.in", "zip bomb")
        unpack_zip(bomb, self.dest(), ImportSettings(max_ratio=5000))  # a deliberately higher limit lets it through

    def test_unpacked_total_limit(self):
        data = make_zip([(f"t/{i}.in", os.urandom(40_000)) for i in range(5)])  # random: no compression
        self.refused(data, "unpacks to more than", limits=ImportSettings(max_unpacked_bytes=150_000))

    def test_a_header_that_lies_about_the_size(self):
        """The real content is longer than the size in the headers: zipfile stops at the declared size, the CRC
        does not match, the archive is refused instead of trusting either number."""
        payload = os.urandom(5000)
        data = bytearray(make_zip([("tests/a.in", payload)]))
        local = data.find(b"PK\x03\x04")
        central = data.find(b"PK\x01\x02")
        struct.pack_into("<I", data, local + 22, 10)  # uncompressed size in the local header
        struct.pack_into("<I", data, central + 24, 10)  # and in the central directory
        self.refused(bytes(data), "cannot be unpacked")

    def test_not_a_zip(self):
        self.refused(b"this is not a zip", "not a zip")

    def test_junk_is_skipped(self):
        files = unpack_zip(make_zip([("p/problem.yaml", b"x"), ("p/.DS_Store", b"j"), ("__MACOSX/p/._problem.yaml", b"j"),
                                     ("p/problem.yaml:Zone.Identifier", b"j"), ("p/.git/config", b"j"),
                                     ("p/tests/a.in~", b"j"), ("p/tests/__pycache__/x.pyc", b"j")]),
                           self.dest(), LIMITS)
        self.assertEqual(files, ["problem.yaml"])  # junk does not count as a second top folder either

    def test_folders_are_checked_the_same_way(self):
        root = self.tmp / "pkg"
        shutil.copytree(EXAMPLE, root)
        self.assertIn("problem.yaml", check_directory(root, LIMITS))
        (root / "tests" / "link.in").symlink_to("/etc/passwd")
        os.mkfifo(root / "tests" / "pipe.in")
        (root / "evil-dir").symlink_to("/etc", target_is_directory=True)
        with self.assertRaises(PackageError) as ctx:  # every problem at once
            check_directory(root, LIMITS)
        message = str(ctx.exception)
        for fragment in ("tests/link.in: symbolic links", "tests/pipe.in: only regular files", "evil-dir: symbolic links"):
            self.assertIn(fragment, message)
        for p in ("tests/link.in", "tests/pipe.in", "evil-dir"):
            (root / p).unlink()
        with self.assertRaises(ArchiveError):
            check_directory(root, ImportSettings(max_files=3))
        with self.assertRaises(ArchiveError):
            check_directory(root, ImportSettings(max_unpacked_bytes=10))


    def test_every_archive_of_the_manual_check_is_refused(self):
        """tools/make-bad-archives.py: what the final check of session 4a.2 feeds to judgectl problem import."""
        import subprocess
        out = self.tmp / "bad"
        r = subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "make-bad-archives.py"), str(out)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        archives = sorted(out.iterdir())
        self.assertEqual(len(archives), 9)
        for path in archives:
            with self.subTest(archive=path.name):
                self.refused(path.read_bytes())

    def test_pack_problem_output_unpacks_to_the_package(self):
        import subprocess
        r = subprocess.run(["bash", str(ROOT / "tools" / "pack-problem.sh"), str(EXAMPLE)], capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        dest = self.dest()
        self.assertEqual(unpack_zip(r.stdout, dest, LIMITS), check_directory(EXAMPLE, LIMITS))
        self.assertEqual((dest / "problem.yaml").read_bytes(), (EXAMPLE / "problem.yaml").read_bytes())

if __name__ == "__main__":
    unittest.main()
