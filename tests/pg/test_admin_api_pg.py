"""The admin HTTP API (/api/admin) on a real PostgreSQL: who gets in, CSRF and Origin, the audit actor."""
import unittest

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from fastapi import Depends

    from app.admin_api.deps import admin_actor
    from app.auth import users


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminAccessTests(ApiCase):
    def setUp(self):
        super().setUp()
        seen = self.seen = []

        # an unsafe endpoint only for this test: the admin dependency and CSRF of a POST under /api/admin
        @self.app.post("/api/admin/_probe")
        def probe(actor=Depends(admin_actor)):
            seen.append(actor)
            return {"ok": True}

    def admin_browser(self):
        _user, password, secret = self.make_admin()
        b = self.browser()
        return b, self.admin_login(b, password, secret)

    def test_only_a_fully_logged_in_admin_gets_in(self):
        anonymous = self.browser()
        r = anonymous.get("/api/admin/me")
        self.assertEqual((r.status_code, r.json()), (401, {"detail": "not_authenticated"}))

        _user, password = self.make_student()
        student = self.browser()
        self.login(student, "ivanov.p", password)
        r = student.get("/api/admin/me")
        self.assertEqual((r.status_code, r.json()), (403, {"detail": "admin_required"}))

        _admin, admin_password, secret = self.make_admin()
        pending = self.browser()
        self.assertEqual(self.login(pending, "boss", admin_password).json()["state"], "mfa_pending")
        r = pending.get("/api/admin/me")
        self.assertEqual((r.status_code, r.json()), (401, {"detail": "mfa_required"}))

        full = self.browser()
        self.admin_login(full, admin_password, secret)
        r = full.get("/api/admin/me")
        self.assertEqual((r.status_code, r.json()["user"]["role"]), (200, "admin"))
        self.assertEqual(r.headers["cache-control"], "no-store")

        users.set_active(self.s, self.cli, users.get_by_login(self.s, "boss"), False, self.now)  # disabled
        self.s.commit()
        r = full.get("/api/admin/me")
        self.assertEqual((r.status_code, r.json()), (401, {"detail": "not_authenticated"}))

    def test_unsafe_requests_need_csrf_and_the_origin(self):
        b, r = self.admin_browser()
        csrf = self.csrf(r)
        no_token = b.post("/api/admin/_probe")
        self.assertEqual((no_token.status_code, no_token.json()), (403, {"detail": "csrf_token_required"}))
        foreign = b.post("/api/admin/_probe", headers={**csrf, "Origin": "https://evil.example"})
        self.assertEqual((foreign.status_code, foreign.json()), (403, {"detail": "origin_not_allowed"}))
        ok = b.post("/api/admin/_probe", headers=csrf)
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(len(self.seen), 1)
        actor = self.seen[0]
        self.assertEqual((actor.user_id, actor.cli, actor.ip),
                         (users.get_by_login(self.s, "boss").id, None, "testclient"))  # who lands in audit_log

        _user, password = self.make_student()
        student = self.browser()
        r = self.login(student, "ivanov.p", password)
        denied = student.post("/api/admin/_probe", headers=self.csrf(r))
        self.assertEqual((denied.status_code, denied.json()), (403, {"detail": "admin_required"}))
        self.assertEqual(len(self.seen), 1)

    def test_a_json_body_over_one_megabyte_is_refused(self):
        b, r = self.admin_browser()
        big = b.post("/api/admin/_probe", headers={**self.csrf(r), "Content-Type": "application/json"},
                     content=b"[" + b"0," * (600 * 1024) + b"0]")
        self.assertEqual((big.status_code, big.json()["detail"]), (413, "too_large"))
        self.assertEqual(self.seen, [])
