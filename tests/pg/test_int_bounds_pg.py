"""Numbers out of range never become a 500 (D-088): the live half of tests/test_int_bounds_units.py.

Every route of the public app is taken from the app itself, so the student routes of 4b are swept as soon as they
exist. Each integer of a path or a query gets 2**31 (one past int4), 2**63 and -1, each integer field of a JSON body
gets 2**31: the answer is 422 invalid_request naming that input, never a database error. The safety net - a
DataError the bounds did not stop - is 422 as well and is logged as an error of our code.
"""
import contextlib
import io
import logging
import re
import unittest
from unittest import mock

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import text

    from app.auth import audit
    from app.judgectl import ops
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.settings import Settings

if HAVE_API:
    from fastapi import Depends

    from app.auth_api import get_db

BAD = (2 ** 31, 2 ** 63, -1)


def concrete(path: str, name: str = "", value=None) -> str:
    """A real URL for a route template: `name` gets `value`, other integers 1, everything else x."""
    def one(m):
        if m.group(1) == name:
            return str(value)
        return "1" if m.group(1).endswith("id") or m.group(1) == "number" else "x"
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", one, path)


def is_int(schema: dict) -> bool:
    if schema.get("type") == "integer":
        return True
    return any(s.get("type") == "integer" for s in schema.get("anyOf", []))


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class IntBoundsTests(ApiCase):
    def setUp(self):
        super().setUp()
        _boss, password, secret = self.make_admin()
        self.admin = self.browser()
        self.h = self.csrf(self.admin_login(self.admin, password, secret))

    def client_for(self, path):
        if path.startswith("/api/admin/"):
            return self.admin, self.h
        self.fail(f"no client for {path}: a new prefix of the public app needs its role here")

    def operations(self):
        doc = self.app.openapi()
        return doc, [(path, method, op) for path, ops in doc["paths"].items() for method, op in ops.items()]

    def assert_refused(self, r, name, what):
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "invalid_request"), what)
        self.assertTrue(any(p.startswith(f"{name}:") or f".{name}:" in p or f"{name}." in p
                            for p in r.json().get("problems", [])), (what, r.json()))

    def test_every_integer_in_a_path_or_query_is_bounded(self):
        _doc, ops = self.operations()
        checked = set()
        for path, method, op in ops:
            if not path.startswith("/api/admin/"):
                continue
            client, headers = self.client_for(path)
            for p in op.get("parameters", []):
                if not is_int(p.get("schema", {})):
                    continue
                for value in BAD:
                    if p["in"] == "path":
                        url, params = concrete(path, p["name"], value), {}
                    else:
                        url, params = concrete(path), {p["name"]: value}
                    what = f"{method.upper()} {path} {p['in']}:{p['name']}={value}"
                    with self.subTest(what):
                        r = client.request(method, url, params=params, headers=headers)
                        self.assert_refused(r, p["name"], what)
                checked.add((p["in"], p["name"]))
        names = {name for _where, name in checked}
        for name in ("item_id", "user_id", "group_id", "submission_id", "comment_id", "number", "cursor", "after",
                     "limit", "extension_id", "import_id"):  # the old routes of ROADMAP "До 4б.1" are in the sweep
            self.assertIn(name, names)
        self.assertEqual({where for where, _name in checked}, {"path", "query"})

    def test_every_integer_field_of_a_json_body_is_bounded(self):
        doc, ops = self.operations()
        components = doc["components"]["schemas"]
        fields = 0
        for path, method, op in ops:
            if not path.startswith("/api/admin/"):
                continue
            media = op.get("requestBody", {}).get("content", {}).get("application/json")
            ref = (media or {}).get("schema", {}).get("$ref")
            if not ref:
                continue
            client, headers = self.client_for(path)
            for name, schema in components[ref.rsplit("/", 1)[-1]].get("properties", {}).items():
                item = schema.get("items", {}) if schema.get("type") == "array" else None
                if item is None and "anyOf" in schema:
                    arrays = [s for s in schema["anyOf"] if s.get("type") == "array"]
                    item = arrays[0].get("items", {}) if arrays else None
                if any(s.get("type") == "number" for s in schema.get("anyOf", [])):
                    continue  # points: the rule decides (test_times_durations_and_points_...)
                if item is not None and is_int(item):
                    body = {name: [2 ** 31]}
                elif is_int(schema):
                    body = {name: 2 ** 31}
                else:
                    continue
                what = f"{method.upper()} {path} body.{name}"
                with self.subTest(what):
                    r = client.request(method, concrete(path), json=body, headers=headers)
                    self.assert_refused(r, name, what)
                fields += 1
        self.assertGreater(fields, 20)  # user_ids, group_ids, up_to, parent_id, material_id, expect, version, ...

    def test_times_durations_and_points_beyond_the_columns_are_refused_by_their_rules(self):
        """Inputs that are not integers of a route but would still overflow: a date the calendar does not have, a
        year whose UTC form overflows, a duration beyond timedelta, points beyond Numeric(8, 2). Each gets the code
        of its own rule, nothing reaches the database."""
        api = "/api/admin"

        def contest(**kw):
            body = {"format_version": 1, "slug": "bounds", "title": "B", "starts_at": "2026-11-10 09:00",
                    "problems": [{"label": "A", "problem": "sum"}], **kw}
            return self.admin.put(f"{api}/contests/bounds", json=body, headers=self.h)

        for kw, fragment in (({"starts_at": "2026-02-30 10:00"}, "starts_at: day is out of range"),
                             ({"ends_at": "9999-12-31 23:59-05:00"}, "ends_at: time out of range"),
                             ({"timing_mode": "personal", "personal_duration": "99999999999h"}, "personal_duration"),
                             ({"problems": [{"label": "A", "problem": "sum", "due_at": "0001-01-01 00:00+05:00"}]},
                              "A: due_at: time out of range"),
                             ({"problems": [{"label": "A", "problem": "sum", "version": 2 ** 31}]}, "maximum"),
                             ({"max_submissions_per_problem": 2 ** 31}, "maximum")):
            with self.subTest(kw):
                r = contest(**kw)
                self.assertEqual((r.status_code, r.json()["detail"]), (422, "contest_invalid"), r.text)
                self.assertTrue(any(fragment in p for p in r.json()["problems"]), r.json())
        student, _pw = self.make_student()
        r = self.admin.post(f"{api}/contests/c/problems/A/extensions", headers=self.h,
                            json={"user_id": student.id, "due_at": "0001-01-01 00:00+05:00"})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "due_at_invalid"))
        r = self.admin.get(f"{api}/audit", params={"since": "9999-12-31T23:59:00-05:00"})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "time_invalid"))
        for points in ("1000000", 1000000, 1234567.5, 999_999_999, 2 ** 31, 10 ** 30, "9" * 40):
            with self.subTest(max_points=points):
                r = self.admin.post(f"{api}/items", headers=self.h,
                                    json={"kind": "assignment", "title": "З", "grading": "points", "max_points": points})
                self.assertEqual((r.status_code, r.json()["detail"]), (422, "max_points_invalid"), r.text)
        r = self.admin.post(f"{api}/items", headers=self.h,
                            json={"kind": "assignment", "title": "З", "grading": "points", "max_points": "9" * 41})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "invalid_request"))  # the string's own bound
        r = self.admin.post(f"{api}/items", headers=self.h, json={"kind": "assignment", "title": "З",
                                                                   "grading": "points", "max_points": "999999.99"})
        self.assertEqual(r.status_code, 201, r.text)

    def test_a_value_the_database_refuses_is_422_and_an_error_in_the_log(self):
        """The safety net: a DataError that no bound stopped (an int4 overflow, a Numeric(8, 2) overflow, a too long
        varchar) is 422 invalid_request, the transaction rolled back, and an ERROR in the log - without the value."""
        def overflow(*statements):
            def endpoint(db=Depends(get_db)):
                for sql in statements:
                    db.execute(text(sql))
                return {"ok": True}
            return endpoint

        cases = {"/test/int4": ("SELECT 2147483648::int4",), "/test/numeric": ("SELECT 1000000::numeric(8, 2)",),
                 "/test/varchar": ("CREATE TEMP TABLE too_long (v varchar(1))", "INSERT INTO too_long VALUES ('xy')")}
        for url, statements in cases.items():
            self.app.add_api_route(url, overflow(*statements), methods=["GET"])
        for url in cases:
            with self.subTest(url), self.assertLogs("judge.api", logging.ERROR) as logs:
                r = self.admin.get(url)
                self.assertEqual((r.status_code, r.json()),
                                 (422, {"detail": "invalid_request", "problems": ["value out of range or too long"]}))
                self.assertIn(f"GET {url}", logs.output[0])
                self.assertNotIn("2147483648", logs.output[0])
        self.assertEqual(self.admin.get("/api/admin/me").status_code, 200)  # the session's connection still works

        r = self.admin.post("/api/admin/submissions/rejudge", headers=self.h,
                            json={"problem_version": "sum@99999999999", "dry_run": True})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "version_not_found"))  # a number inside a text


@requires_pg
class JudgectlBoundsTests(PgCase):
    """judgectl takes ids as arguments and inside texts (#12, sum@3): beyond int4 is a usage error or "not found",
    never a traceback of the database; and the same safety net as the API stands behind it."""

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(session_factory=self.Session, settings=Settings(), actor=audit.Actor(cli="cli:judge"), out=out,
                      err=err, interactive=lambda: False)
        with contextlib.redirect_stderr(err):
            try:
                code = main(list(argv), ctx)
            except SystemExit as exc:  # argparse
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_ids_beyond_int4(self):
        for argv in (("item", "show", "2147483648"), ("submission", "show", "99999999999999999999"),
                     ("comment", "show", "0"), ("queue", "judgements", "--last", "2147483648")):
            with self.subTest(argv):
                code, _out, err = self.run_cli(*argv)
                self.assertEqual(code, 2, err)
                self.assertIn("must be from 1 to 2147483647", err)
        for argv, fragment in ((("group", "show", "#99999999999"), "no group #99999999999"),
                               (("material", "show", "#99999999999"), "no material #99999999999"),
                               (("problem", "show", "sum@99999999999"), "not a version")):
            with self.subTest(argv):
                code, _out, err = self.run_cli(*argv)
                self.assertEqual(code, 1, err)
                self.assertIn(fragment, err)
        self.assertEqual(self.run_cli("problem", "show", "sum@1")[0], 0)  # the same form within int4

    def test_a_database_refusal_is_one_line_not_a_traceback(self):
        def overflow(_ctx, _args):
            with self.Session() as db:
                db.execute(text("SELECT 2147483648::int4"))
            return 0

        with mock.patch.object(ops, "status", overflow):
            code, _out, err = self.run_cli("status")
        self.assertEqual((code, err.strip()), (1, "judgectl: the database refused a value (out of range or too long)"))

if __name__ == "__main__":
    unittest.main()
