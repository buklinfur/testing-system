"""The mark «the server ended this judgement» (D-069) is the column judgements.server_gave_up_at (D-090).

Its only writer is queue._fail_attempt (the server giving up after max_attempts): nothing a worker sends can set it,
so a late result of the last holder is refused by queue.finish and an error text of a worker is never read as the
server's mark. Everything else reads it through queue.server_gave_up. The old text mark in judgements.error lives on
only in the migration that moved it into the column.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("backend", "worker", "runner", "protocol", "tools")
COLUMN = "server_gave_up_at"


def python_files():
    for folder in FOLDERS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if ".venv" not in path.parts and "__pycache__" not in path.parts and "migrations" not in path.parts:
                yield path


def owners(tree) -> dict:
    owner = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(fn):
                owner.setdefault(id(sub), fn.name)
    return owner


def writers(tree) -> list:
    """Names of the functions that assign the column (`x.server_gave_up_at = ...`, also in a tuple) or pass it as a
    keyword (`values(server_gave_up_at=...)`, `Judgement(server_gave_up_at=...)`)."""
    owner, found = owners(tree), []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Attribute) and t.attr == COLUMN for top in targets for t in ast.walk(top)):
                found.append(owner.get(id(node), "<module>"))
        if isinstance(node, ast.Call) and any(kw.arg == COLUMN for kw in node.keywords):
            found.append(owner.get(id(node), "<module>"))
    return found


class ServerMarkTests(unittest.TestCase):
    def test_only_the_server_giving_up_writes_the_column(self):
        writes = [(str(path.relative_to(ROOT)), name) for path in python_files()
                  for name in writers(ast.parse(path.read_text(encoding="utf-8")))]
        self.assertEqual(writes, [("backend/app/queue.py", "_fail_attempt")])

    def test_the_old_text_mark_is_gone_from_the_code(self):
        places = []
        for path in python_files():
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "the server gave up after " in node.value:
                    places.append(str(path.relative_to(ROOT)))
                if isinstance(node, (ast.Name, ast.Attribute)) and "SERVER_GAVE_UP" in (
                        getattr(node, "id", None) or getattr(node, "attr", "")):
                    places.append(str(path.relative_to(ROOT)))
        self.assertEqual(places, [])

    def test_the_check_sees_a_write(self):
        tree = ast.parse("def f(j):\n    j.server_gave_up_at = 1\n\ndef g(s):\n    s.values(server_gave_up_at=None)\n"
                         "\ndef h(j):\n    j.a, j.server_gave_up_at = 1, 2\n\ndef k(j):\n    return j.server_gave_up_at\n")
        self.assertEqual(sorted(writers(tree)), ["f", "g", "h"])


if __name__ == "__main__":
    unittest.main()
