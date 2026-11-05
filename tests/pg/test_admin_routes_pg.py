"""Every route under /api/admin, taken from the app itself (a new endpoint is checked without touching this file):
nobody but a fully logged-in admin gets an answer, an unsafe request without CSRF is refused, and there is no
route for what only the console may do (R2)."""
import re
import unittest

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

SAFE = {"get", "head", "options"}
CONSOLE_ONLY = ("/admins", "2fa", "totp", "worker")  # admins, their 2FA, worker tokens: judgectl only


def concrete(path: str) -> str:
    """A real URL for a route template: ids are 1, everything else is x."""
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", lambda m: "1" if m.group(1).endswith("id") or m.group(1) == "number"
                  else "x", path)


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminRoutesTests(ApiCase):
    def routes(self):
        out = [(method, path) for path, ops in self.app.openapi()["paths"].items() if path.startswith("/api/admin/")
               for method in ops]
        self.assertGreater(len(out), 40)  # the sweep really sees the API
        return out

    def test_only_a_fully_logged_in_admin_with_csrf_gets_in(self):
        _student, password = self.make_student()
        student = self.browser()
        sh = self.csrf(self.login(student, "ivanov.p", password))
        _half, half_password, _ = self.make_admin("half")
        half = self.browser()
        hh = self.csrf(self.login(half, "half", half_password))
        _boss, boss_password, secret = self.make_admin()
        admin = self.browser()
        self.admin_login(admin, boss_password, secret)
        anonymous = self.browser()
        for method, path in self.routes():
            url = concrete(path)
            with self.subTest(method=method.upper(), path=path):
                r = anonymous.request(method, url)  # with the right Origin, without a session
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "not_authenticated"))
                r = student.request(method, url, headers=sh)
                self.assertEqual((r.status_code, r.json()["detail"]), (403, "admin_required"))
                r = half.request(method, url, headers=hh)
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "mfa_required"))
                if method not in SAFE:
                    r = admin.request(method, url)  # the admin's own browser, the Origin right, no X-CSRF-Token
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "csrf_token_required"))
                    r = admin.request(method, url, headers={"Origin": "https://evil.example"})
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "origin_not_allowed"))

    def test_there_is_no_route_for_what_only_the_console_does(self):
        self.assertEqual([p for _m, p in self.routes() if any(w in p for w in CONSOLE_ONLY)], [])
