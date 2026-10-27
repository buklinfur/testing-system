"""Layers (CLAUDE.md, R28): the backend and protocol/ never import the runner or the worker, and the definitions
they share with the runner stay in line."""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from protocol.languages import EXTENSIONS, SYSTEM_LANGUAGES  # noqa: E402
from runner.languages import PROFILES  # noqa: E402

FORBIDDEN = ("runner", "worker")
CHECKED = ("backend/app", "backend/migrations", "protocol")


def imported_modules(path: Path):
    """(line, module) of every import statement of a file, relative imports resolved to nothing."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


class LayerTests(unittest.TestCase):
    def test_no_backend_or_protocol_file_imports_the_runner_or_the_worker(self):
        found = []
        for top in CHECKED:
            for path in sorted((ROOT / top).rglob("*.py")):
                for line, module in imported_modules(path):
                    if module.split(".")[0] in FORBIDDEN:
                        found.append(f"{path.relative_to(ROOT)}:{line} imports {module}")
        self.assertEqual(found, [])

    def test_importing_every_backend_module_loads_neither(self):
        """Also catches indirect imports (through a third package) that the source scan cannot see."""
        script = """
import importlib, pkgutil, sys
import app, protocol
for pkg in (app, protocol):
    for info in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
        if not info.name.endswith(".__main__"):  # those run a command line when imported
            importlib.import_module(info.name)
bad = sorted(m for m in sys.modules if m.split(".")[0] in ("runner", "worker"))
print("loaded:", len([m for m in sys.modules if m.startswith(("app.", "protocol."))]), "forbidden:", bad)
sys.exit(1 if bad else 0)
"""
        r = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True,
                           env={"PYTHONPATH": f"{ROOT}:{ROOT / 'backend'}", "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("forbidden: []", r.stdout)


class LanguagesTests(unittest.TestCase):
    def test_system_languages_are_the_runner_profiles(self):
        keys = [key for key, _name in SYSTEM_LANGUAGES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sorted(keys), sorted(PROFILES))

    def test_extensions_are_the_runner_extensions(self):
        runner = {ext: key for key, profile in PROFILES.items() for ext in profile.extensions}
        self.assertEqual(EXTENSIONS, runner)


if __name__ == "__main__":
    unittest.main()
