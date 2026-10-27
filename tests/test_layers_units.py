"""Shared definitions stay in line across layers that must not import each other (the backend never imports
the runner)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from protocol.languages import SYSTEM_LANGUAGES  # noqa: E402
from runner.languages import PROFILES  # noqa: E402


class LanguagesTests(unittest.TestCase):
    def test_system_languages_are_the_runner_profiles(self):
        keys = [key for key, _name in SYSTEM_LANGUAGES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sorted(keys), sorted(PROFILES))


if __name__ == "__main__":
    unittest.main()
