"""protocol/problem_package.py: validation, safe test masks, junk, the content digest (no database)."""
import builtins
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from protocol.problem_package import (PackageError, content_digest, is_junk, list_files, load, mask_problem,  # noqa: E402
                                      match_mask, sha256_file)

EXAMPLES = ROOT / "problems-examples"


class PackageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def copy(self, name="sum-of-two") -> Path:
        root = self.tmp / name
        shutil.copytree(EXAMPLES / name, root)
        return root

    def edit(self, root: Path, change):
        data = yaml.safe_load((root / "problem.yaml").read_text(encoding="utf-8"))
        change(data)
        (root / "problem.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def refused(self, root, *fragments):
        with self.assertRaises(PackageError) as ctx:
            load(root)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        return ctx.exception

    def test_all_examples_load(self):
        for root in sorted(EXAMPLES.iterdir()):
            with self.subTest(example=root.name):
                pkg = load(root)
                self.assertEqual(pkg.slug, root.name)
                self.assertNotIn("test_groups", pkg.config)
                self.assertNotIn("solutions", pkg.config)

    def test_programs_and_tests_of_an_example(self):
        pkg = load(EXAMPLES / "pair-with-sum")
        roles = {(p.role, p.language, p.expect) for p in pkg.programs}
        self.assertIn(("checker", "cpp", None), roles)
        self.assertIn(("solution", "python", "OK"), roles)
        tests = [t for g in pkg.groups for t in g.tests]
        self.assertTrue(tests and all(t.input.endswith(".in") for t in tests))

    def test_masks_cannot_leave_the_package(self):
        for mask, why in (("../secret/*.in", "'..'"), ("tests/../../x.in", "'..'"), ("/etc/*.in", "absolute"),
                          ("C:/x/*.in", "absolute"), ("tests\\*.in", "backslash")):
            with self.subTest(mask=mask):
                self.assertIn(why, mask_problem(mask))
                root = self.copy()
                self.edit(root, lambda d: d["test_groups"][0].update(tests=mask))
                self.refused(root, why)
                shutil.rmtree(root)

    def test_masks_match_like_glob_inside_the_file_list(self):
        files = ["tests/sample01.in", "tests/small01.in", "tests/a/x1.in", "tests/a/b/x2.in", "other/s1.in"]
        self.assertEqual(match_mask("tests/s*.in", files), ["tests/sample01.in", "tests/small01.in"])
        self.assertEqual(match_mask("tests/**/x*.in", files), ["tests/a/b/x2.in", "tests/a/x1.in"])
        self.assertEqual(match_mask("tests/sma??01.in", files), ["tests/small01.in"])
        self.assertEqual(match_mask("*/s1.in", files), ["other/s1.in"])

    def test_a_symlink_to_a_secret_is_not_a_test(self):
        root = self.copy()
        (root / "tests" / "zz.in").symlink_to("/etc/passwd")
        self.refused(root, "tests/zz.in: symbolic links")

    def test_jsonschema_is_required(self):
        real_import = builtins.__import__

        def no_jsonschema(name, *args, **kwargs):
            if name == "jsonschema":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", no_jsonschema):
            self.refused(EXAMPLES / "sum-of-two", "jsonschema is not installed")

    def test_errors_are_listed_together(self):
        root = self.copy()
        (root / "tests" / "small01.out").unlink()
        self.edit(root, lambda d: (d.update(limits={"time_ms": 1000, "wall_ms": 10}),
                                   d["test_groups"][0].update(depends_on=["nope"])))
        exc = self.refused(root, "wall_ms", "missing answer file tests/small01.out", "unknown group 'nope'")
        self.assertGreaterEqual(len(exc.problems), 3)

    def test_points_fit_the_score_column(self):
        """test_groups.points and judgements.score are Numeric(8, 2): a group or a total beyond 999999.99 would fail in
        the database at import or at the result, so the package is refused (D-088)."""
        root = self.copy()
        self.edit(root, lambda d: d["test_groups"][0].update(points=1_000_000))
        self.refused(root, "is greater than the maximum of 999999.99")
        # three groups of 600000: each within the column, the total not
        self.edit(root, lambda d: [g.update(points=600_000) for g in d["test_groups"]])
        self.refused(root, "total points of groups", "at most 999999.99")
        for name, key in (("quiz-python-basics", "questions"), ("what-is-the-output", "answers")):
            with self.subTest(example=name):
                root = self.copy(name)
                self.edit(root, lambda d: [q.update(points=999_999) for q in d[key]])
                self.refused(root, "total points is", "at most 999999.99")
                self.edit(root, lambda d: [q.update(points=1) for q in d[key]])
                load(root)  # the same package within the limit loads

    def test_unknown_jury_language_and_solution_extension(self):
        root = self.copy()
        (root / "solutions" / "ok.rb").write_text("puts 1\n")
        self.edit(root, lambda d: d["solutions"].append({"path": "solutions/ok.rb", "expect": "OK"}))
        self.refused(root, "solutions/ok.rb: unknown language")

    def test_junk_and_the_content_digest(self):
        for rel in (".DS_Store", "a/Thumbs.db", "desktop.ini", "problem.yaml:Zone.Identifier", "__MACOSX/x",
                    ".git/HEAD", "a/__pycache__/m.cpython-312.pyc", "m.pyc", "notes.md~"):
            self.assertTrue(is_junk(rel), rel)
        for rel in ("problem.yaml", "tests/a.in", "git/x", "statement/images/pic.png"):
            self.assertFalse(is_junk(rel), rel)
        root = self.copy()
        clean = {rel: sha256_file(root / rel) for rel in list_files(root)}
        (root / ".DS_Store").write_bytes(b"junk")
        (root / "statement" / "ru.md:Zone.Identifier").write_text("[ZoneTransfer]")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref")
        self.assertEqual(list_files(root), sorted(clean))  # junk is not part of the package
        self.assertEqual(content_digest(dict(reversed(list(clean.items())))), content_digest(clean))  # order-free
        changed = dict(clean, **{"statement/ru.md": "0" * 64})
        self.assertNotEqual(content_digest(changed), content_digest(clean))
        renamed = {("x/" + k if k == "statement/ru.md" else k): v for k, v in clean.items()}
        self.assertNotEqual(content_digest(renamed), content_digest(clean))  # the path is part of the identity


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 40
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 40


class PictureWarningTests(unittest.TestCase):
    """statement/images/* that are not PNG/JPEG/WebP by content: a warning with advice, never an error (В3, D-071)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "sum-of-two"
        shutil.copytree(EXAMPLES / "sum-of-two", self.root)
        images = self.root / "statement" / "images"
        images.mkdir(parents=True, exist_ok=True)
        for name, data in {"ok.png": PNG, "photo.jpg": JPEG, "pic.webp": WEBP, "схема.png": PNG,
                           "fake.png": b"<html><script>alert(1)</script></html>",
                           "logo.svg": b'<svg xmlns="http://www.w3.org/2000/svg"/>', "anim.gif": b"GIF89a....",
                           "notes.txt": b"just text", "empty.png": b""}.items():
            (images / name).write_bytes(data)

    def test_only_non_pictures_warn_and_the_package_imports(self):
        pkg = load(self.root)  # no PackageError: warnings only
        warned = sorted(w.split(":")[0] for w in pkg.warnings if w.startswith("statement/images/"))
        self.assertEqual(warned, sorted(f"statement/images/{n}" for n in
                                        ("fake.png", "logo.svg", "anim.gif", "notes.txt", "empty.png")))
        self.assertIn("statement/images/схема.png", pkg.files, "a Cyrillic name is a valid package file")
        self.assertTrue(all("save it as PNG" in w for w in pkg.warnings if w.startswith("statement/images/")))

    def test_the_warning_does_not_change_the_content_hash(self):
        """The same package with the warning and with the check switched off: one content_sha256 (the warnings
        never reach the files that are hashed)."""
        def digest(pkg):
            return content_digest({rel: sha256_file(self.root / rel) for rel in pkg.files})

        with_warning = load(self.root)
        with mock.patch("protocol.problem_package.picture_warnings", return_value=[]):
            without = load(self.root)
        self.assertTrue(any(w.startswith("statement/images/") for w in with_warning.warnings))
        self.assertFalse(any(w.startswith("statement/images/") for w in without.warnings))
        self.assertEqual(with_warning.files, without.files)
        self.assertEqual(digest(with_warning), digest(without))

    def test_the_package_rule_is_the_servers_rule(self):
        """protocol cannot import the backend: picture_kind repeats content_files.sniff for pictures."""
        sys.path.insert(0, str(ROOT / "backend"))
        try:
            from app.content_files import sniff
        except ImportError:  # pragma: no cover - the backend's packages are not installed
            self.skipTest("needs the backend")
        from protocol.problem_package import picture_kind
        for head in (PNG, JPEG, WEBP, b"GIF89a", b"PK\x03\x04", b"<html>", b"", b"RIFF\x00\x00\x00\x00WAVE"):
            with self.subTest(head=head[:6]):
                server = sniff(head)
                self.assertEqual(picture_kind(head), server if server in ("png", "jpeg", "webp") else None)


if __name__ == "__main__":
    unittest.main()
