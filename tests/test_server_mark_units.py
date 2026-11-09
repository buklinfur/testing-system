"""The mark «the server ended this judgement» (D-069) lives in judgements.error behind one constant and one check:
queue.SERVER_GAVE_UP is the only place the text is written down, and only queue._carries_server_mark (through
queue.server_gave_up) compares anything with it. Everything else - finish, views, tests - calls the function, so the
temporary text mark can be replaced by a column in one place (ROADMAP)."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("backend", "worker", "runner", "protocol", "tools", "tests")
TEXT = "gave up after"


def python_files():
    for folder in FOLDERS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if ".venv" not in path.parts and "__pycache__" not in path.parts:
                yield path


def mentions_mark(node) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "SERVER_GAVE_UP":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "SERVER_GAVE_UP":
            return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and TEXT in sub.value:
            return True
    return False


class ServerMarkTests(unittest.TestCase):
    def test_only_one_function_compares_the_mark(self):
        found = []
        for path in python_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            owner = {}
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for sub in ast.walk(fn):
                        owner.setdefault(id(sub), fn.name)
            for node in ast.walk(tree):
                compare = isinstance(node, ast.Compare) and mentions_mark(node)
                method = (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                          and node.func.attr in ("startswith", "endswith", "find", "index", "count", "__contains__")
                          and mentions_mark(node))
                if compare or method:
                    found.append((str(path.relative_to(ROOT)), owner.get(id(node), "<module>")))
        self.assertEqual(found, [("backend/app/queue.py", "_carries_server_mark")])

    def test_the_text_is_written_down_once(self):
        places = []
        for path in python_files():
            if path.name == Path(__file__).name:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(
                        "the server " + TEXT):
                    places.append(str(path.relative_to(ROOT)))
        self.assertEqual(places, ["backend/app/queue.py"])


if __name__ == "__main__":
    unittest.main()
