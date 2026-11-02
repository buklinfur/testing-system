"""The time zone of a user (D-024) on a real PostgreSQL: PUT /api/auth/profile for anyone fully logged in, `me`, and
the admin's way to set a student's zone (create, PUT /api/admin/users/{id}/timezone)."""
import json
import unittest

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from sqlalchemy import select

    from app.models import AuditLog, Role, User


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class ProfileTests(ApiCase):
    def setUp(self):
        super().setUp()
        self.student, password = self.make_student()
        self.sb = self.browser()
        self.sh = self.csrf(self.login(self.sb, "ivanov.p", password))
        self.boss, self.boss_password, secret = self.make_admin()
        self.ab = self.browser()
        self.ah = self.csrf(self.admin_login(self.ab, self.boss_password, secret))

    def row(self, user_id):
        self.s.expire_all()
        return self.s.get(User, user_id)

    def audits(self):
        self.s.expire_all()
        return self.s.execute(select(AuditLog).where(AuditLog.action == "user.set_timezone")
                              .order_by(AuditLog.id)).scalars().all()

    def put_profile(self, b, headers, body):
        return b.put("/api/auth/profile", headers=headers, json=body)

    # ---------------------------------------------------------------- PUT /api/auth/profile
    def test_a_student_and_an_admin_set_their_own_zone(self):
        for b, h, user in ((self.sb, self.sh, self.student), (self.ab, self.ah, self.boss)):
            with self.subTest(role=user.role.value):
                r = self.put_profile(b, h, {"timezone": " Asia/Novosibirsk "})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual((r.json()["state"], r.json()["user"]["timezone"]), ("active", "Asia/Novosibirsk"))
                self.assertEqual(b.get("/api/auth/me").json()["user"]["timezone"], "Asia/Novosibirsk")
                self.assertEqual(self.row(user.id).timezone, "Asia/Novosibirsk")
                r = self.put_profile(b, h, {"timezone": None})  # null clears it
                self.assertEqual((r.status_code, r.json()["user"]["timezone"]), (200, None))
                self.assertIsNone(self.row(user.id).timezone)
        entries = self.audits()
        self.assertEqual([(e.actor_user_id, e.object_id, e.details, e.ip) for e in entries], [
            (self.student.id, str(self.student.id), {"old": None, "new": "Asia/Novosibirsk"}, "testclient"),
            (self.student.id, str(self.student.id), {"old": "Asia/Novosibirsk", "new": None}, "testclient"),
            (self.boss.id, str(self.boss.id), {"old": None, "new": "Asia/Novosibirsk"}, "testclient"),
            (self.boss.id, str(self.boss.id), {"old": "Asia/Novosibirsk", "new": None}, "testclient")])
        self.put_profile(self.sb, self.sh, {"timezone": None})  # no change: no audit row
        self.assertEqual(len(self.audits()), 4)

    def test_only_the_time_zone_and_only_a_valid_one(self):
        before = (self.student.login, self.student.display_name, self.student.role)
        for body in ({"timezone": "Europe/Moscow", "role": "admin"}, {"timezone": "Europe/Moscow", "login": "boss2"},
                     {"timezone": "Europe/Moscow", "display_name": "Взломщик"},
                     {"timezone": "Europe/Moscow", "user_id": self.boss.id}, {"role": "admin"}, {},
                     {"timezone": 7}, {"timezone": "x" * 65}):
            with self.subTest(body=body):
                r = self.put_profile(self.sb, self.sh, body)
                self.assertEqual((r.status_code, r.json()["detail"]), (422, "invalid_request"), r.text)
        for zone in ("Mars/Olympus", "../../etc/passwd", "/etc/localtime"):
            with self.subTest(zone=zone):
                r = self.put_profile(self.sb, self.sh, {"timezone": zone})
                self.assertEqual((r.status_code, r.json()["detail"]), (422, "timezone_invalid"), r.text)
        user = self.row(self.student.id)
        self.assertEqual((user.login, user.display_name, user.role, user.timezone), (*before, None))
        self.assertIsNone(self.row(self.boss.id).timezone)  # another user's id in the body reached nobody
        self.assertEqual(self.audits(), [])

    def test_who_may_call_it(self):
        anonymous = self.browser()
        r = anonymous.put("/api/auth/profile", json={"timezone": "UTC"})
        self.assertEqual((r.status_code, r.json()["detail"]), (401, "not_authenticated"))
        _admin, password, _secret = self.make_admin("half")
        half = self.browser()
        hh = self.csrf(self.login(half, "half", password))  # password given, TOTP not yet
        r = self.put_profile(half, hh, {"timezone": "UTC"})
        self.assertEqual((r.status_code, r.json()["detail"]), (401, "mfa_required"))
        r = self.sb.put("/api/auth/profile", json={"timezone": "UTC"})  # without the CSRF token
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "csrf_token_required"))
        r = self.sb.put("/api/auth/profile", json={"timezone": "UTC"}, headers={**self.sh, "Origin": "https://evil.ru"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.audits(), [])

    # ---------------------------------------------------------------- the admin sets a student's zone
    def test_the_admin_sets_a_students_zone_but_never_an_admins(self):
        r = self.ab.post("/api/admin/users", headers=self.ah, json={"surname": "Петрова", "first_name": "Анна",
                                                                   "timezone": "Asia/Novosibirsk"})
        self.assertEqual(r.status_code, 201, r.text)
        created = r.json()["user"]
        self.assertEqual(created["timezone"], "Asia/Novosibirsk")
        r = self.ab.post("/api/admin/users", headers=self.ah, json={"surname": "Сидоров", "first_name": "Олег",
                                                                   "timezone": "Mars/Olympus"})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "timezone_invalid"))
        self.assertIsNone(self.s.execute(select(User).where(User.login == "sidorov.o")).scalar_one_or_none())

        path = f"/api/admin/users/{self.student.id}/timezone"
        r = self.ab.put(path, headers=self.ah, json={"timezone": "Europe/Moscow"})
        self.assertEqual((r.status_code, r.json()["timezone"]), (200, "Europe/Moscow"), r.text)
        self.assertEqual(self.ab.get(f"/api/admin/users/{self.student.id}").json()["timezone"], "Europe/Moscow")
        r = self.ab.put(path, headers=self.ah, json={"timezone": None})
        self.assertEqual((r.status_code, r.json()["timezone"]), (200, None))
        for body, code in (({"timezone": "Nowhere/City"}, "timezone_invalid"), ({}, "invalid_request"),
                           ({"timezone": "UTC", "role": "admin"}, "invalid_request")):
            with self.subTest(body=body):
                r = self.ab.put(path, headers=self.ah, json=body)
                self.assertEqual((r.status_code, r.json()["detail"]), (422, code))
        r = self.ab.put(f"/api/admin/users/{self.boss.id}/timezone", headers=self.ah, json={"timezone": "UTC"})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "admin_in_console"))
        self.assertIsNone(self.row(self.boss.id).timezone)
        r = self.ab.put("/api/admin/users/9999/timezone", headers=self.ah, json={"timezone": "UTC"})
        self.assertEqual((r.status_code, r.json()["detail"]), (404, "user_not_found"))
        r = self.sb.put(path, headers=self.sh, json={"timezone": "UTC"})  # a student cannot use the admin route
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "admin_required"))

        entries = self.audits()
        self.assertEqual([(e.actor_user_id, e.details) for e in entries],
                         [(self.boss.id, {"old": None, "new": "Europe/Moscow"}),
                          (self.boss.id, {"old": "Europe/Moscow", "new": None})])
        create = self.s.execute(select(AuditLog).where(AuditLog.action == "user.create",
                                                       AuditLog.object_id == str(created["id"]))).scalar_one()
        self.assertEqual(create.details, {"login": "petrova.a", "role": "student", "timezone": "Asia/Novosibirsk"})
        self.assertNotIn(r"password", json.dumps([e.details for e in entries]))
        self.assertEqual(self.row(self.student.id).role, Role.student)
