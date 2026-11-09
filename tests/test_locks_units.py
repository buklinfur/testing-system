"""The advisory-lock keys of the backend (app/locks.py, D-075): different numbers, int4-sized, and no module taking a
lock with a key of its own."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app import locks  # noqa: E402

BACKEND = ROOT / "backend" / "app"


class LockKeyTests(unittest.TestCase):
    def test_every_key_is_a_different_number(self):
        values = list(locks.ALL.values())
        self.assertEqual(len(values), len(set(values)), locks.ALL)

    def test_all_lists_every_constant_of_the_module(self):
        constants = {name: value for name, value in vars(locks).items() if name.isupper() and isinstance(value, int)}
        self.assertEqual(constants, locks.ALL)

    def test_every_key_fits_an_int4(self):
        # the pairs (key, sub) take int4 keys; the bigint one is kept in the same range so that one list holds all
        for name, value in locks.ALL.items():
            self.assertTrue(0 < value < 2 ** 31, name)

    def test_no_other_module_takes_an_advisory_lock(self):
        found = []
        for path in sorted(BACKEND.rglob("*.py")):
            if path.name == "locks.py" and path.parent == BACKEND:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"pg_(try_)?advisory_\w*lock", code):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(found, [], "take advisory locks through app/locks.py")


if __name__ == "__main__":
    unittest.main()
