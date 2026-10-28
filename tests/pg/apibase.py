"""Shared setup of the public-app tests (/api/auth, /api/admin): an app on a throwaway schema, browsers,
students, admins with a confirmed TOTP, logins."""
import unittest
from datetime import datetime, timedelta, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase

try:
    import httpx  # noqa: F401
    import fastapi  # noqa: F401
    HAVE_API = True
except ImportError:
    HAVE_API = False

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from app.auth import audit, totp, users
    from app.public_main import create_public_app
    from app.settings import AuthSettings, Settings

ORIGIN = "https://judge.example.ru"
T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)


class ApiCase(PgCase):
    """Not a test case itself: subclasses add @requires_pg and the skip on HAVE_API."""

    def settings(self):
        return Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()))

    def setUp(self):
        super().setUp()
        self.now = T0
        self.app = create_public_app(self.settings(), session_factory=self.Session, clock=lambda: self.now)
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
