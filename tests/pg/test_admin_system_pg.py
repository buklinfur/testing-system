"""/api/admin system, audit and languages on a real PostgreSQL; and that audit_log, read through /audit, holds no
password, QR token or TOTP code made during the test."""
import json
import unittest

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from app import groups as g
    from app.system import migration_head

CLASS = "фамилия;имя;логин\nСидоров;Олег;oleg.s\nКузнецова;Анна;\n"


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminSystemTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.boss, self.password, self.secret = self.make_admin()
        self.b = self.browser()
        self.login_code = self.code(self.secret)
        self.h = self.csrf(self.admin_login(self.b, self.password, self.secret))

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers={**self.h, **kw.pop("headers", {})}, **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def audit(self, **params):
        """Every row /audit gives for these filters, following the cursor."""
        items, cursor = [], None
        while True:
            page = self.ok(self.get("/audit", params={**params, "limit": 3, **({"cursor": cursor} if cursor else {})}))
            items += page["items"]
            cursor = page["next_cursor"]
            if cursor is None:
                return items

    # ---------------------------------------------------------------- system, languages
    def test_system(self):
        self.token_and_worker("w1")
        self.submission(self.make_student()[0])
        self.s.commit()
        data = self.ok(self.get("/system"))
        self.assertEqual(set(data), {"migration", "load", "metrics", "workers", "workers_known", "config_problems",
                                     "timezone"})
        self.assertNotIn("blobs", json.dumps(data))  # the blob store is the console's
        self.assertEqual(data["migration"], {"current": None, "head": migration_head(), "ok": False})  # create_all
        self.assertEqual((data["load"]["queue_length"], data["load"]["workers_online"]), (1, 1))
        self.assertEqual([(w["name"], w["state"]) for w in data["workers"]], [("w1", "idle")])
        self.assertEqual((data["workers_known"], data["metrics"]["queue_length"], data["timezone"]), (1, 1, "UTC"))
        self.assertEqual(data["config_problems"], [])

    def test_languages(self):
        items = self.ok(self.get("/languages"))["items"]
        self.assertEqual([(i["key"], i["enabled"]) for i in items], [("c", True), ("cpp", True), ("python", True)])

    # ---------------------------------------------------------------- audit
    def test_audit_filters_and_pages(self):
        self.ok(self.post("/groups", json={"name": "7Б"}), 201)
        made = self.ok(self.post("/groups", json={"name": "8А"}), 201)
        self.tick(hours=1)
        later = self.now
        g.create(self.s, self.cli, "9В", self.now)  # from the console
        self.s.commit()
        self.ok(self.b.put(f"/api/admin/groups/{made['id']}", headers=self.h, json={"name": "8 А"}))

        groups = self.audit(action="group.")
        self.assertEqual([i["action"] for i in groups], ["group.rename", "group.create", "group.create", "group.create"])
        self.assertEqual([i["action"] for i in self.audit(action="group.create")], ["group.create"] * 3)
        self.assertEqual(self.audit(action="group"), [])  # without the dot it is the exact action
        self.assertEqual(self.audit(action="%."), [])  # a prefix is literal
        self.assertEqual([i["object_id"] for i in self.audit(object_type="group", object_id=str(made["id"]))],
                         [str(made["id"])] * 2)
        console = self.audit(action="group.", console="true")
        self.assertEqual([(i["actor_cli"], i["details"]["name"]) for i in console], [("cli:tester", "9В")])
        web = self.audit(action="group.", console="false", actor_user_id=self.boss.id)
        self.assertEqual(len(web), 3)
        self.assertEqual({i["ip"] for i in web}, {"testclient"})
        self.assertEqual([i["details"].get("name") for i in self.audit(action="group.", since=later.isoformat())],
                         [None, "9В"])  # the rename has {from, to}
        self.assertEqual(len(self.audit(action="group.", until=later.isoformat())), 2)
        self.assertTrue(all(i["at"].endswith("Z") for i in groups))
        r = self.get("/audit", params={"since": "вчера"})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "time_invalid"))

    def test_no_password_qr_token_or_totp_code_in_the_audit(self):
        secrets = [self.password, self.login_code]
        created = self.ok(self.post("/users", json={"surname": "Иванов", "first_name": "Пётр"}), 201)
        secrets.append(created["password"])
        student_id = created["user"]["id"]
        secrets.append(self.ok(self.post(f"/users/{student_id}/reset-password"))["password"])
        tokens = self.ok(self.post("/users/qr-tokens", json={"user_ids": [student_id]}))["tokens"]
        secrets.append(tokens[0]["url"].split("#", 1)[1])
        imported = self.ok(self.post("/users/import", content=CLASS.encode(), headers={"Content-Type": "text/csv"}), 201)
        secrets += [u["password"] for u in imported["users"]]
        student = self.browser()  # the student logs in with the password and fails once with a wrong one
        student.post("/api/auth/login", json={"login": "ivanov.p", "password": secrets[3]})
        student.post("/api/auth/login", json={"login": "ivanov.p", "password": "wrong-" + secrets[3]})
        self.tick(seconds=31)
        again = self.browser()  # one more admin login: its TOTP code too
        r = again.post("/api/auth/login", json={"login": "boss", "password": self.password})
        code = self.code(self.secret)
        secrets.append(code)
        again.post("/api/auth/totp", json={"code": code}, headers=self.csrf(r))

        rows = self.audit()
        actions = {i["action"] for i in rows}
        self.assertTrue({"user.create", "user.reset_password", "user.import", "auth.login"} <= actions, actions)
        self.assertTrue(any(a.startswith("user.qr") or a.startswith("qr.") for a in actions), actions)
        dump = json.dumps(rows, ensure_ascii=False)
        for secret in secrets:
            with self.subTest(secret=secret[:3] + "…"):
                self.assertGreaterEqual(len(secret), 6)
                self.assertNotIn(secret, dump)
