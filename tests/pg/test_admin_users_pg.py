"""/api/admin users on a real PostgreSQL: lists, students, class import, admins read-only, QR batches, secrets."""
import json
import logging
import unittest

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from sqlalchemy import select

    from app.auth import ratelimit, users
    from app.models import AuditLog, AuthFailureKind, Submission, User
    from app.settings import AuthSettings

CLASS = "фамилия;имя;логин\nИванов;Пётр;\nПетрова;Анна;\nСидоров;Олег;oleg.s\n"


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminUsersTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.boss, password, self.secret = self.make_admin()
        self.b = self.browser()
        r = self.admin_login(self.b, password, self.secret)
        self.h = self.csrf(r)
        self.secrets = [password, self.code(self.secret)]

    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers={**self.h, **kw.pop("headers", {})}, **kw)

    def put(self, path, **kw):
        return self.b.put(f"/api/admin{path}", headers=self.h, **kw)

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def student_id(self, login):
        self.s.expire_all()
        return self.s.execute(select(User.id).where(User.login == login)).scalar_one()

    def upload(self, text, accept=None, dry_run=False, kind="text/csv"):
        headers = {"Content-Type": kind}
        if accept:
            headers["Accept"] = accept
        return self.post("/users/import" + ("?dry_run=true" if dry_run else ""), content=text.encode(), headers=headers)

    # ---------------------------------------------------------------- lists
    def test_list_search_filters_and_pages(self):
        for surname, first, login in (("Иванов", "Пётр", None), ("Петрова", "Анна", None), ("Ли", "Ян", "li_yan"),
                                      ("Ким", "Ира", "kim.ira")):
            self.ok(self.post("/users", json={"surname": surname, "first_name": first, "login": login}), 201)
        self.ok(self.post(f"/users/{self.student_id('kim.ira')}/disable"))
        ratelimit.record_failure(self.s, AuthSettings(login_fail_limit=1), self.now, login="ivanov.p", ip="10.0.0.1",
                                 kind=AuthFailureKind.password)
        self.s.commit()

        def logins(**params):
            return [u["login"] for u in self.ok(self.get("/users", params=params))["items"]]

        self.assertEqual(logins(q="_"), ["li_yan"])  # "_" is a letter here, not a wildcard
        self.assertEqual(logins(q="ПЁТР"), ["ivanov.p"])  # the display name too, any case
        self.assertEqual(logins(role="admin"), ["boss", "admin"])  # and the seed's admin
        self.assertEqual(logins(role="student", active="false"), ["kim.ira"])
        self.assertEqual(logins(locked="true"), ["ivanov.p"])
        self.assertNotIn("ivanov.p", logins(locked="false"))
        everyone, cursor = [], None
        while True:
            page = self.ok(self.get("/users", params={"limit": 2, **({"cursor": cursor} if cursor else {})}))
            everyone += [u["login"] for u in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(everyone), 6)  # 4 students, boss, the seed admin
        self.assertEqual(everyone, logins(limit=500))  # the same order; limit above 200 is cut, not refused
        boss = self.ok(self.get(f"/users/{self.boss.id}"))
        self.assertEqual((boss["two_factor"], boss["last_login_at"][-1]), ("confirmed", "Z"))
        self.assertNotIn("password_hash", boss)
        self.assertEqual(self.get("/users/999999").json()["detail"], "user_not_found")

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json() if r.content else None

    # ---------------------------------------------------------------- create, reset
    def test_passwords_are_shown_once_and_never_cached(self):
        r = self.post("/users", json={"surname": "Иванов", "first_name": "Пётр"})
        body = self.ok(r, 201)
        self.assertEqual((body["user"]["login"], r.headers["cache-control"]), ("ivanov.p", "no-store"))
        self.assertNotIn("password", self.ok(self.get(f"/users/{body['user']['id']}")))
        r = self.post(f"/users/{body['user']['id']}/reset-password")
        new = self.ok(r)["password"]
        self.assertNotEqual(new, body["password"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertEqual(self.post("/users", json={"surname": "Ъ"}).json()["detail"], "login_invalid")
        self.assertEqual(self.post("/users", json={"login": "ivanov.p", "surname": "X", "first_name": "Y"}).status_code, 409)
        self.assertEqual(self.post("/users", json={"surname": "X", "extra": 1}).json()["detail"], "invalid_request")

    # ---------------------------------------------------------------- class import
    def test_class_import_json_csv_dry_run_and_errors(self):
        dry = self.ok(self.upload(CLASS, dry_run=True))
        self.assertEqual([u["login"] for u in dry["users"]], ["ivanov.p", "petrova.a", "oleg.s"])
        self.assertNotIn("password", json.dumps(dry))
        self.assertEqual(self.s.execute(select(User).where(User.login == "oleg.s")).first(), None)  # nothing written

        r = self.upload(CLASS, accept="text/csv")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertEqual(r.headers["cache-control"], "no-store")
        disposition = r.headers["content-disposition"]
        self.assertRegex(disposition, r'^attachment; filename="students-import-\d+\.csv"$')  # no logins in the name
        self.assertTrue(r.content.startswith(b"\xef\xbb\xbf"))
        rows = r.content.decode("utf-8-sig").splitlines()
        self.assertEqual(rows[0], "фамилия;имя;логин;пароль;display_name")
        self.assertEqual(rows[1].split(";")[:3], ["Иванов", "Пётр", "ivanov.p"])

        second = self.ok(self.upload("Кузнецова;Анна\n"), 201)
        self.assertEqual(second["users"][0]["login"], "kuznetsova.a")
        self.assertTrue(second["users"][0]["password"])

        self.assertEqual(self.upload(CLASS, kind="text/plain").status_code, 415)
        self.assertEqual(self.upload("Иванов;Пётр\n" * 120000).status_code, 413)
        bad = self.upload("Иванов\nПетров;Иван;ab\nКим;Ира;oleg.s\n")
        self.assertEqual((bad.status_code, bad.json()["detail"]), (422, "csv_invalid"))
        self.assertEqual([p.split(":")[0] for p in bad.json()["problems"]], ["line 1", "line 2"])
        taken = self.upload("Кимова;Ира;oleg.s\n")  # the file is fine, the login is taken in the database
        self.assertEqual((taken.status_code, taken.json()["problems"][0][:7]), (422, "line 1:"))

        imports = self.ok(self.get("/users/imports"))["items"]
        self.assertEqual([(i["count"], i["rolled_back"]) for i in imports], [(1, False), (3, False)])
        first = imports[1]["id"]
        mismatch = self.post(f"/users/imports/{first}/rollback", json={"confirm_import_id": first + 1})
        self.assertEqual((mismatch.status_code, mismatch.json()["detail"]), (422, "confirmation_mismatch"))
        done = self.ok(self.post(f"/users/imports/{first}/rollback", json={"confirm_import_id": first}))
        self.assertEqual(sorted(done["deleted"]), ["ivanov.p", "oleg.s", "petrova.a"])
        again = self.post(f"/users/imports/{first}/rollback", json={"confirm_import_id": first})
        self.assertEqual((again.status_code, again.json()["detail"]), (409, "already_rolled_back"))

    # ---------------------------------------------------------------- admins: look, do not touch
    def test_any_action_on_an_admin_is_refused(self):
        boss = self.boss.id
        for method, path, body in (("post", f"/users/{boss}/reset-password", None), ("post", f"/users/{boss}/disable", None),
                                   ("post", f"/users/{boss}/enable", None), ("put", f"/users/{boss}/login", {"login": "x123"}),
                                   ("post", f"/users/{boss}/unlock", None),
                                   ("post", f"/users/{boss}/delete", {"confirm_login": "boss"}),
                                   ("post", "/logins/boss/unlock", None), ("post", "/users/qr-tokens", {"user_ids": [boss]})):
            with self.subTest(path=path):
                r = getattr(self, method)(path, **({"json": body} if body is not None else {}))
                self.assertEqual((r.status_code, r.json()["detail"]), (403, "admin_in_console"))
        self.s.expire_all()
        self.assertTrue(self.s.get(User, boss).is_active)
        self.assertEqual(self.get(f"/users/{boss}").status_code, 200)

    # ---------------------------------------------------------------- one student
    def test_student_actions(self):
        sid = self.ok(self.post("/users", json={"surname": "Иванов", "first_name": "Пётр"}), 201)["user"]["id"]
        self.ok(self.post("/users", json={"login": "taken.one", "surname": "X", "first_name": "Y"}), 201)
        self.assertFalse(self.ok(self.post(f"/users/{sid}/disable"))["active"])
        self.assertTrue(self.ok(self.post(f"/users/{sid}/enable"))["active"])
        self.assertEqual(self.put(f"/users/{sid}/login", json={"login": "taken.one"}).status_code, 409)
        self.assertEqual(self.put(f"/users/{sid}/login", json={"login": "a"}).json()["detail"], "login_invalid")
        self.assertEqual(self.ok(self.put(f"/users/{sid}/login", json={"login": "petya"}))["login"], "petya")
        for _ in range(10):
            ratelimit.record_failure(self.s, AuthSettings(), self.now, login="petya", ip="10.0.0.2",
                                     kind=AuthFailureKind.password)
        self.s.commit()
        failures = self.ok(self.get(f"/users/{sid}/failures"))
        # the tenth failure locked the login, and counting starts again after a lockout (docs/AUTH.md 6)
        self.assertEqual((failures["counting"], len(failures["items"]), failures["items"][0]["kind"],
                          failures["locked_until"][-1]), (0, 10, "password", "Z"))
        self.assertTrue(self.ok(self.post(f"/users/{sid}/unlock"))["was_locked"])
        self.assertEqual(self.ok(self.post("/logins/nobody.here/unlock"))["was_locked"], False)  # unknown logins too
        self.assertEqual(self.post("/ips/unlock", json={"ip": "not-an-ip"}).json()["detail"], "invalid_ip")
        self.assertEqual(self.ok(self.post("/ips/unlock", json={"ip": "10.0.0.2"}))["was_locked"], False)

        wrong = self.post(f"/users/{sid}/delete", json={"confirm_login": "someone"})
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (422, "confirmation_mismatch"))
        self.s.add(Submission(user_id=sid, contest_problem_id=self.seed["cp"].id, language_key="python",
                              source_sha256="c" * 64))
        self.s.commit()
        traced = self.post(f"/users/{sid}/delete", json={"confirm_login": "petya"})
        self.assertEqual((traced.status_code, traced.json()["detail"], traced.json()["traces"]),
                         (409, "has_traces", {"submissions": 1}))
        other = self.student_id("taken.one")
        self.assertEqual(self.post(f"/users/{other}/delete", json={"confirm_login": " Taken.One "}).status_code, 204)
        self.assertEqual(self.get(f"/users/{other}").status_code, 404)

    # ---------------------------------------------------------------- QR batches
    def test_qr_tokens_for_a_class_are_one_audit_record(self):
        ids = [self.ok(self.post("/users", json={"surname": s, "first_name": "А"}), 201)["user"]["id"]
               for s in ("Иванов", "Петров", "Сидоров")]
        r = self.post("/users/qr-tokens", json={"user_ids": ids + [ids[0]]})
        tokens = self.ok(r)["tokens"]
        self.assertEqual(([t["user_id"] for t in tokens], r.headers["cache-control"]), (ids, "no-store"))
        self.assertTrue(all(t["url"].startswith(f"{ORIGIN}/login/qr#") and t["expires_at"].endswith("Z") for t in tokens))
        batch = self.s.execute(select(AuditLog).where(AuditLog.action == "user.qr_tokens")).scalars().all()
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].details["user_ids"], ids)
        student = self.browser()
        login = student.post("/api/auth/qr", json={"token": tokens[1]["url"].split("#", 1)[1]})
        self.assertEqual((login.status_code, login.json()["user"]["id"]), (200, ids[1]))
        self.ok(self.post(f"/users/{ids[2]}/disable"))
        refused = self.post("/users/qr-tokens", json={"user_ids": ids})
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "qr_not_allowed"))
        missing = self.post("/users/qr-tokens", json={"user_ids": [ids[0], 999999]})
        self.assertEqual((missing.status_code, missing.json()["problems"]), (404, ["no user 999999"]))
        self.assertEqual(len(self.s.execute(select(AuditLog).where(AuditLog.action == "user.qr_tokens")).all()), 1)

    # ---------------------------------------------------------------- secrets
    def test_no_password_qr_token_or_totp_code_in_the_audit_or_the_logs(self):
        capture = Capture()
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(capture)
        root.setLevel(logging.DEBUG)
        try:
            created = self.ok(self.post("/users", json={"surname": "Иванов", "first_name": "Пётр"}), 201)
            self.secrets.append(created["password"])
            self.secrets.append(self.ok(self.post(f"/users/{created['user']['id']}/reset-password"))["password"])
            for row in self.ok(self.upload(CLASS.replace("Иванов;Пётр;\n", "")), 201)["users"]:
                self.secrets.append(row["password"])
            for t in self.ok(self.post("/users/qr-tokens", json={"user_ids": [created["user"]["id"]]}))["tokens"]:
                self.secrets.append(t["url"].split("#", 1)[1])
            self.tick(seconds=31)
            again = self.browser()  # another admin login: one more TOTP code
            r = self.login(again, "boss", self.secrets[0])
            code = self.code(self.secret)
            self.secrets.append(code)
            self.assertEqual(again.post("/api/auth/totp", json={"code": code}, headers=self.csrf(r)).status_code, 200)
        finally:
            root.removeHandler(capture)
            root.setLevel(old_level)
        self.s.expire_all()
        audit_text = json.dumps([[a.action, a.object_id, a.details] for a in self.s.execute(select(AuditLog)).scalars()],
                                ensure_ascii=False)
        self.assertIn("user.qr_tokens", audit_text)  # the records are there ...
        for secret in self.secrets:
            self.assertNotIn(secret, audit_text)  # ... without any secret
            for line in capture.lines:
                self.assertNotIn(secret, line)
        self.assertEqual(users.find_by_login(self.s, "ivanov.p").role.value, "student")
