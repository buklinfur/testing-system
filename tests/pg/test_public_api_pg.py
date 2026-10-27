"""The public auth API (/api/auth) end to end through TestClient on a real PostgreSQL."""
import unittest
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

try:
    import httpx  # noqa: F401
    import fastapi  # noqa: F401
    HAVE_API = True
except ImportError:
    HAVE_API = False

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.auth import audit, sessions, totp, users
    from app.auth_api import COOKIE
    from app.models import AuthFailure, LoginToken
    from app.public_main import create_public_app
    from app.settings import AuthSettings, Settings

ORIGIN = "https://judge.example.ru"
T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class PublicAuthApiTests(PgCase):
    def setUp(self):
        super().setUp()
        self.now = T0
        settings = Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()))
        self.app = create_public_app(settings, session_factory=self.Session, clock=lambda: self.now)
        self.box = self.app.state.box
        self.cli = audit.Actor(cli="cli:tester")

    # ---------------------------------------------------------------- helpers
    def tick(self, **kw):
        self.now += timedelta(**kw)

    def browser(self, peer="testclient"):
        return TestClient(self.app, base_url="https://testserver", headers={"Origin": ORIGIN}, client=(peer, 50000))

    def make_student(self, surname="Иванов", first="Пётр"):
        user, password = users.create_student(self.s, self.cli, self.now, surname=surname, first_name=first)
        self.s.commit()
        return user, password

    def make_admin(self, login="boss", password="correct-horse-battery"):
        user = users.create_admin(self.s, self.cli, self.now, login=login, display_name="Boss", password=password)
        secret = users.enroll_totp(self.s, self.cli, user, self.box, self.now)
        self.assertTrue(users.confirm_totp(self.s, self.cli, user, self.box, self.code(secret), self.now))
        self.s.commit()
        self.tick(seconds=31)
        return user, password, secret

    def code(self, secret, delta=0):
        return totp.code_at_step(secret, totp.step_at(self.now) + delta)

    def login(self, b, login, password):
        return b.post("/api/auth/login", json={"login": login, "password": password})

    def csrf(self, r):
        return {"X-CSRF-Token": r.json()["csrf_token"]}

    def admin_login(self, b, password, secret):
        r = self.login(b, "boss", password)
        self.assertEqual(r.json()["state"], "mfa_pending")
        r = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual((r.status_code, r.json()["state"]), (200, "active"), r.text)
        return r

    # ---------------------------------------------------------------- cookie and session basics
    def test_student_login_sets_a_host_only_secure_cookie(self):
        user, password = self.make_student()
        b = self.browser()
        r = self.login(b, " Ivanov.P ", password)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["state"], body["user"]["login"], body["user"]["role"]), ("active", "ivanov.p", "student"))
        self.assertEqual(body["user"]["display_name"], "Пётр И.")
        raw = r.headers.get_list("set-cookie")
        self.assertEqual(len(raw), 1)
        morsel = SimpleCookie(raw[0])[COOKIE]
        self.assertTrue(morsel["httponly"] and morsel["secure"])
        self.assertEqual((morsel["samesite"].lower(), morsel["path"], morsel["domain"]), ("lax", "/", ""))
        self.assertEqual(int(morsel["max-age"]), 30 * 86400)
        token = morsel.value
        self.assertEqual(self.s.execute(select(sessions.UserSession.token_hash)).scalar_one(), sessions.hash_token(token))
        me = b.get("/api/auth/me")
        self.assertEqual((me.status_code, me.json()["user"]["id"]), (200, user.id))
        self.assertEqual(me.headers["cache-control"], "no-store")
        self.assertEqual(self.browser().get("/api/auth/me").status_code, 401)  # another browser, no cookie

    def test_activity_slides_the_session_and_the_cookie(self):
        _user, password = self.make_student()
        b = self.browser()
        self.login(b, "ivanov.p", password)
        self.tick(days=20)
        r = b.get("/api/auth/me")
        self.assertEqual(int(SimpleCookie(r.headers["set-cookie"])[COOKIE]["max-age"]), 30 * 86400)
        self.tick(days=20)  # 40 days after login, 20 after the last activity
        self.assertEqual(b.get("/api/auth/me").status_code, 200)
        self.tick(days=31)
        self.assertEqual(b.get("/api/auth/me").json()["detail"], "not_authenticated")

    def test_logout(self):
        _user, password = self.make_student()
        b = self.browser()
        r = self.login(b, "ivanov.p", password)
        self.assertEqual(b.post("/api/auth/logout").status_code, 403)  # no CSRF token
        self.assertEqual(b.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code, 403)
        self.assertEqual(b.get("/api/auth/me").status_code, 200)
        out = b.post("/api/auth/logout", headers=self.csrf(r))
        self.assertEqual(out.status_code, 204)
        self.assertIn('Max-Age=0', out.headers["set-cookie"])
        self.assertEqual(b.get("/api/auth/me").status_code, 401)
        self.assertEqual(b.post("/api/auth/logout").status_code, 204)  # nothing to revoke: just clears

    def test_unsafe_requests_need_origin_and_csrf(self):
        _user, password = self.make_student()
        b = self.browser()
        r = self.login(b, "ivanov.p", password)
        body = {"current_password": password, "new_password": "brand-new-secret"}
        for headers in ({"Origin": ""}, {"Origin": "https://evil.example"}):
            with self.subTest(headers=headers):
                bad = b.post("/api/auth/password", json=body, headers={**headers, **self.csrf(r)})
                self.assertEqual((bad.status_code, bad.json()["detail"]), (403, "origin_not_allowed"))
        no_csrf = b.post("/api/auth/password", json=body)
        self.assertEqual((no_csrf.status_code, no_csrf.json()["detail"]), (403, "csrf_token_required"))
        other = self.login(self.browser(), "ivanov.p", password)  # a CSRF token of another session
        self.assertEqual(b.post("/api/auth/password", json=body, headers=self.csrf(other)).status_code, 403)
        self.assertEqual(self.login(self.browser(), "ivanov.p", password).status_code, 200)  # password unchanged
        # a login from another site is refused before the password is even checked
        evil = self.browser().post("/api/auth/login", json={"login": "ivanov.p", "password": password},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403)
        self.assertNotIn("set-cookie", evil.headers)

    # ---------------------------------------------------------------- brute force
    def test_unknown_login_and_wrong_password_look_the_same(self):
        _user, password = self.make_student()
        a = self.login(self.browser(), "ivanov.p", password + "x")
        b = self.login(self.browser(), "nobody.here", password)
        self.assertEqual((a.status_code, a.json()), (401, {"detail": "invalid_credentials"}))
        self.assertEqual((b.status_code, b.json()), (a.status_code, a.json()))
        self.assertNotIn("set-cookie", a.headers)

    def test_lockout_is_the_same_for_unknown_logins_and_can_be_lifted(self):
        _user, password = self.make_student()
        b = self.browser()
        for _ in range(10):
            self.login(b, "ivanov.p", "wrong")
            self.login(b, "nobody.here", "wrong")
        self.tick(seconds=60)
        known, unknown = self.login(b, "ivanov.p", password), self.login(b, "nobody.here", password)
        self.assertEqual((known.status_code, known.json()), (429, {"detail": "locked"}))
        self.assertEqual((unknown.status_code, unknown.json()), (429, {"detail": "locked"}))
        self.assertEqual(known.headers["retry-after"], unknown.headers["retry-after"])
        self.assertEqual(known.headers["retry-after"], str(900 - 60))
        users.unlock(self.s, self.cli, "ivanov.p", self.now)
        self.s.commit()
        self.assertEqual(self.login(b, "ivanov.p", password).status_code, 200)
        self.assertEqual(self.login(b, "nobody.here", password).status_code, 429)

    def test_client_ip_comes_from_forwarded_for_only_behind_a_local_proxy(self):
        self.make_student()
        self.login(self.browser("203.0.113.9"), "ivanov.p", "wrong")
        self.browser("203.0.113.9").post("/api/auth/login", json={"login": "ivanov.p", "password": "wrong"},
                                         headers={"X-Forwarded-For": "198.51.100.1"})
        self.browser("127.0.0.1").post("/api/auth/login", json={"login": "ivanov.p", "password": "wrong"},
                                       headers={"X-Forwarded-For": "10.9.9.9, 198.51.100.2"})
        ips = self.s.execute(select(AuthFailure.ip).order_by(AuthFailure.id)).scalars().all()
        self.assertEqual(ips, ["203.0.113.9", "203.0.113.9", "198.51.100.2"])

    # ---------------------------------------------------------------- admin and TOTP
    def test_admin_needs_the_totp_step_and_mfa_pending_allows_nothing(self):
        admin, password, secret = self.make_admin()
        b = self.browser()
        r = self.login(b, "boss", password)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["state"], r.json()["user"]), ("mfa_pending", None))
        pending = SimpleCookie(r.headers["set-cookie"])[COOKIE]
        self.assertEqual(int(pending["max-age"]), 300)
        me = b.get("/api/auth/me")
        self.assertEqual((me.status_code, me.json()["detail"]), (401, "mfa_required"))
        pw = b.post("/api/auth/password", headers=self.csrf(r),
                    json={"current_password": password, "new_password": "another-long-pass", "totp_code": self.code(secret)})
        self.assertEqual(pw.status_code, 401)
        self.assertEqual(b.post("/api/auth/totp", json={"code": self.code(secret)}).status_code, 403)  # no CSRF
        wrong = b.post("/api/auth/totp", json={"code": "000000"}, headers=self.csrf(r))
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (401, "invalid_credentials"))
        ok = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual((ok.status_code, ok.json()["state"], ok.json()["user"]["role"]), (200, "active", "admin"))
        self.assertNotEqual(SimpleCookie(ok.headers["set-cookie"])[COOKIE].value, pending.value)
        self.assertNotEqual(ok.json()["csrf_token"], r.json()["csrf_token"])
        self.assertEqual(int(SimpleCookie(ok.headers["set-cookie"])[COOKIE]["max-age"]), 12 * 3600)
        self.assertEqual(b.get("/api/auth/me").json()["user"]["id"], admin.id)
        stale = TestClient(self.app, base_url="https://testserver", headers={"Origin": ORIGIN},
                           cookies={COOKIE: pending.value})
        self.assertEqual(stale.get("/api/auth/me").status_code, 401)  # the pre-2FA token is dead
        again = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(ok))
        self.assertEqual(again.status_code, 409)

    def test_a_totp_code_works_once(self):
        _admin, password, secret = self.make_admin()
        self.admin_login(self.browser(), password, secret)
        b = self.browser()
        r = self.login(b, "boss", password)
        replay = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual(replay.status_code, 401)
        self.tick(seconds=30)
        self.assertEqual(b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r)).status_code, 200)

    def test_mfa_pending_expires(self):
        _admin, password, secret = self.make_admin()
        b = self.browser()
        r = self.login(b, "boss", password)
        self.tick(seconds=301)
        late = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual((late.status_code, late.json()["detail"]), (401, "not_authenticated"))

    def test_admin_totp_failures_lock_and_unlock_works_for_admins(self):
        _admin, password, secret = self.make_admin()
        b = self.browser()
        r = self.login(b, "boss", password)
        for _ in range(5):
            b.post("/api/auth/totp", json={"code": "000000"}, headers=self.csrf(r))
        locked = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(self.login(self.browser(), "boss", password).status_code, 429)
        users.unlock(self.s, self.cli, "boss", self.now)
        self.s.commit()
        self.admin_login(self.browser(), password, secret)

    # ---------------------------------------------------------------- QR
    def test_qr_login_is_one_time(self):
        user, _ = self.make_student()
        token, _ = users.issue_login_token(self.s, self.cli, user, self.app.state.settings.auth, self.now)
        self.s.commit()
        b = self.browser()
        r = b.post("/api/auth/qr", json={"token": token})
        self.assertEqual((r.status_code, r.json()["user"]["id"]), (200, user.id))
        self.assertEqual(b.get("/api/auth/me").status_code, 200)
        again = self.browser().post("/api/auth/qr", json={"token": token})
        self.assertEqual((again.status_code, again.json()["detail"]), (401, "invalid_credentials"))

    def test_qr_does_not_log_an_admin_in(self):
        admin, _password, _secret = self.make_admin()
        token = sessions.new_token()  # issue_login_token refuses admins, so the row is put in by hand
        self.s.add(LoginToken(token_hash=sessions.hash_token(token), user_id=admin.id, created_at=self.now,
                              expires_at=self.now + timedelta(days=1)))
        self.s.commit()
        r = self.browser().post("/api/auth/qr", json={"token": token})
        self.assertEqual((r.status_code, r.json()["detail"]), (401, "invalid_credentials"))
        self.assertNotIn("set-cookie", r.headers)

    # ---------------------------------------------------------------- password change and reset
    def test_student_changes_password(self):
        _user, password = self.make_student()
        b, other = self.browser(), self.browser()
        r = self.login(b, "ivanov.p", password)
        self.login(other, "ivanov.p", password)
        weak = b.post("/api/auth/password", headers=self.csrf(r),
                      json={"current_password": password, "new_password": "qwerty123"})
        self.assertEqual(weak.status_code, 422)
        self.assertIn("common", weak.json()["detail"])
        wrong = b.post("/api/auth/password", headers=self.csrf(r),
                       json={"current_password": "nope-nope", "new_password": "brand-new-secret"})
        self.assertEqual((wrong.status_code, wrong.json()["detail"]), (403, "invalid_credentials"))
        ok = b.post("/api/auth/password", headers=self.csrf(r),
                    json={"current_password": password, "new_password": "brand-new-secret"})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertNotEqual(ok.json()["csrf_token"], r.json()["csrf_token"])
        self.assertEqual(b.get("/api/auth/me").status_code, 200)  # this browser stays, with the new cookie
        self.assertEqual(other.get("/api/auth/me").status_code, 401)  # every other session is gone
        self.assertEqual(self.login(self.browser(), "ivanov.p", "brand-new-secret").status_code, 200)

    def test_admin_password_change_needs_a_totp_code(self):
        _admin, password, secret = self.make_admin()
        b = self.browser()
        r = self.admin_login(b, password, secret)
        self.tick(seconds=30)
        body = {"current_password": password, "new_password": "another-long-passphrase"}
        self.assertEqual(b.post("/api/auth/password", headers=self.csrf(r), json=body).status_code, 403)
        ok = b.post("/api/auth/password", headers=self.csrf(r), json={**body, "totp_code": self.code(secret)})
        self.assertEqual(ok.status_code, 200, ok.text)

    def test_password_reset_by_admin_revokes_sessions(self):
        user, password = self.make_student()
        b = self.browser()
        self.login(b, "ivanov.p", password)
        new = users.reset_password(self.s, self.cli, user, self.now)
        self.s.commit()
        self.assertEqual(b.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login(b, "ivanov.p", password).status_code, 401)
        self.assertEqual(self.login(b, "ivanov.p", new).status_code, 200)

    def test_disabled_user_is_logged_out_everywhere(self):
        user, password = self.make_student()
        b = self.browser()
        self.login(b, "ivanov.p", password)
        users.set_active(self.s, self.cli, user, False, self.now)
        self.s.commit()
        self.assertEqual(b.get("/api/auth/me").status_code, 401)
        self.assertEqual(self.login(b, "ivanov.p", password).status_code, 401)


if __name__ == "__main__":
    unittest.main()
