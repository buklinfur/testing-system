"""Every number the public app takes is bounded before a handler runs (D-088).

The ids of the schema are int4, scores Numeric(8, 2): an unbounded integer in a path, a query or a body reaches
PostgreSQL and fails there (DataError). The test reads the OpenAPI of the whole public app - every route, so the
student routes of 4b are checked as soon as they exist - and requires a minimum and a maximum on every integer and
number in the parameters and request bodies, the maximum of an integer within int4. `x: Id = Path()` loses the bounds
in fastapi (`app.ids`), and loses them in the OpenAPI too, so the test catches it. The live half -
2**31 sent to every such route - is tests/pg/test_int_bounds_pg.py.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

INT4_MAX = 2 ** 31 - 1
# (path, method, where) that may hold an unbounded number, each with the reason; an entry not needed is an error too
# (the branches of a field's anyOf share its entry)
_POINTS = ("points (feed.POINTS): grading.parse_points reads every number through str() and its 6-digit pattern "
           "before the database, and names the refusal (D-061); a NaN literal must reach it, so no bound of the body")
EXCEPTIONS: dict = {
    ("/api/admin/items", "POST", "body:application/json->CreateBody.max_points"): _POINTS,
    ("/api/admin/items/{item_id}", "PUT", "body:application/json->ItemFields.max_points"): _POINTS,
    ("/api/admin/items/{item_id}/works/{user_id}/grade", "POST", "body:application/json->GradeBody.score"): _POINTS,
}


def public_openapi() -> dict:
    from cryptography.fernet import Fernet

    from app.public_main import create_public_app
    from app.settings import AuthSettings, Settings

    settings = Settings(public_origin="https://judge.example.ru",
                        auth=AuthSettings(secret_key=Fernet.generate_key().decode()))
    return create_public_app(settings, session_factory=lambda: None).openapi()


def numbers(schema, components: dict, where: str, seen=frozenset()):
    """(where, schema) of every integer or number node reachable from this schema."""
    if not isinstance(schema, dict):
        return
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name not in seen:
            yield from numbers(components[name], components, f"{where}->{name}", seen | {name})
        return
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "integer" in kinds or "number" in kinds:
        yield where, schema
    for key in ("anyOf", "oneOf", "allOf"):
        for i, sub in enumerate(schema.get(key, [])):
            yield from numbers(sub, components, f"{where}|{i}", seen)
    for name, sub in schema.get("properties", {}).items():
        yield from numbers(sub, components, f"{where}.{name}", seen)
    if isinstance(schema.get("items"), dict):
        yield from numbers(schema["items"], components, f"{where}[]", seen)
    if isinstance(schema.get("additionalProperties"), dict):
        yield from numbers(schema["additionalProperties"], components, f"{where}{{}}", seen)


def unbounded(doc: dict) -> dict:
    """{(path, method, where): what is wrong} over every operation of the document."""
    components = doc.get("components", {}).get("schemas", {})
    found = {}
    for path, ops in doc["paths"].items():
        for method, op in ops.items():
            inputs = [(f"{p['in']}:{p['name']}", p.get("schema", {})) for p in op.get("parameters", [])]
            for ctype, media in op.get("requestBody", {}).get("content", {}).items():
                inputs.append((f"body:{ctype}", media.get("schema", {})))
            for name, schema in inputs:
                for where, node in numbers(schema, components, name):
                    low = node.get("minimum", node.get("exclusiveMinimum"))
                    high = node.get("maximum", node.get("exclusiveMaximum"))
                    problem = None
                    if low is None or high is None:
                        problem = f"no {'minimum' if low is None else 'maximum'}"
                    elif node.get("type") == "integer" and high > INT4_MAX:
                        problem = f"maximum {high} beyond int4"
                    if problem:
                        found.setdefault((path, method.upper(), where.split("|")[0]), problem)
    return found


class IntBoundsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = public_openapi()

    def test_every_number_of_every_route_is_bounded(self):
        found = unbounded(self.doc)
        self.assertEqual({k: v for k, v in found.items() if k not in EXCEPTIONS}, {})
        self.assertEqual(sorted(set(EXCEPTIONS) - set(found)), [], "an exception that is no longer needed")

    def test_the_check_sees_ids_in_paths_queries_and_bodies(self):
        """It is not vacuous: it finds the bounded ids where they are, and an unbounded one when it is there."""
        components = self.doc["components"]["schemas"]
        seen = set()
        for path, ops in self.doc["paths"].items():
            for op in ops.values():
                for p in op.get("parameters", []):
                    if list(numbers(p.get("schema", {}), components, "")):
                        seen.add(p["in"])
                if any(list(numbers(m.get("schema", {}), components, ""))
                       for m in op.get("requestBody", {}).get("content", {}).values()):
                    seen.add("body")
        self.assertEqual(seen, {"path", "query", "body"})
        bad = {"paths": {"/x/{n}": {"get": {"parameters": [{"in": "path", "name": "n", "schema": {"type": "integer"}}]},
                                    "post": {"requestBody": {"content": {"application/json": {"schema": {
                                        "$ref": "#/components/schemas/B"}}}}}}},
               "components": {"schemas": {"B": {"type": "object", "properties": {
                   "ids": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 2 ** 31}},
                   "n": {"anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]}}}}}}
        body = "body:application/json->B"
        self.assertEqual(unbounded(bad), {("/x/{n}", "GET", "path:n"): "no minimum",
                                          ("/x/{n}", "POST", f"{body}.ids[]"): "maximum 2147483648 beyond int4",
                                          ("/x/{n}", "POST", f"{body}.n"): "no maximum"})


class ConsoleArgumentsTests(unittest.TestCase):
    def test_no_command_takes_an_unbounded_number(self):
        """judgectl and dev_seed: every number argument is judgectl.core.int4 (1..int4), none a bare int or float."""
        import ast

        found = []
        for path in sorted((ROOT / "backend" / "app").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                    for kw in node.keywords:
                        if kw.arg == "type" and isinstance(kw.value, ast.Name) and kw.value.id in ("int", "float"):
                            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(found, [])

    def test_int4(self):
        import argparse

        from app.judgectl.core import int4
        self.assertEqual((int4("1"), int4("2147483647")), (1, 2147483647))
        for text in ("0", "-1", "2147483648", "99999999999999999999", "1.5", "x", ""):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                int4(text)

    def test_parse_id(self):
        from app.ids import parse_id
        self.assertEqual([parse_id(t) for t in ("1", "2147483647", "007")], [1, 2147483647, 7])
        for text in ("0", "2147483648", "99999999999", "١٢", "-1", "1 ", "", None, "+1"):
            with self.subTest(text=text):
                self.assertIsNone(parse_id(text))


if __name__ == "__main__":
    unittest.main()
