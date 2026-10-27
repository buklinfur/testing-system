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


if __name__ == "__main__":
    unittest.main()
