"""Public app checks without PostgreSQL: startup configuration, the Origin check, client IP behind Caddy."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

try:
    from fastapi.testclient import TestClient
    HAVE_API = True
except ImportError:
    HAVE_API = False

if HAVE_API:  # outside the try: a broken app module must fail the tests, not skip them
    from cryptography.fernet import Fernet
    from starlette.requests import Request

    from app.auth_api import client_ip
    from app.public_main import PublicConfigError, canonical_origin, check_config, create_public_app
    from app.settings import AuthSettings, Settings

    KEY = Fernet.generate_key().decode()

    def settings(**kw):
        auth = AuthSettings(secret_key=kw.pop("secret_key", KEY))
        return Settings(auth=auth, **{"public_origin": "https://judge.example.ru", **kw})


class NoDatabase:
    def __call__(self):
        raise AssertionError("the request must be refused before any database access")


@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class ConfigTests(unittest.TestCase):
    def assertRefused(self, s, *fragments):
        with self.assertRaises(PublicConfigError) as ctx:
            create_public_app(s, session_factory=NoDatabase())
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        return str(ctx.exception)

    def test_valid_configuration(self):
        box, origin = check_config(settings())
        self.assertEqual(origin, "https://judge.example.ru")
        self.assertEqual(box.decrypt(box.encrypt("x")), "x")

    def test_secret_key_must_be_a_real_fernet_key(self):
        for bad in (None, "", "CHANGE_ME", " CHANGE_ME ", "not-a-fernet-key"):
            with self.subTest(key=bad):
                message = self.assertRefused(settings(secret_key=bad), "JUDGE_SECRET_KEY")
                if bad:
                    self.assertNotIn("not-a-fernet-key", message)  # the value itself is never printed

    def test_origin(self):
        cases = {
            "https://Judge.Example.ru/": "https://judge.example.ru",
            "https://judge.example.ru:443": "https://judge.example.ru",
            "https://judge.example.ru:8443": "https://judge.example.ru:8443",
            "http://127.0.0.1:8000": "http://127.0.0.1:8000",
            "http://localhost": "http://localhost",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(canonical_origin(raw), want)
        for bad in (None, "", "judge.example.ru", "http://judge.example.ru", "https://judge.example.ru/app",
                    "https://u:p@judge.example.ru", "https://judge.example.ru?x=1", "ftp://judge.example.ru",
                    "https://judge.example.ru:99999"):
            with self.subTest(bad=bad), self.assertRaises(PublicConfigError):
                canonical_origin(bad)

    def test_ports(self):
        self.assertRefused(settings(public_port=8001), "internal worker API port")
        self.assertRefused(settings(public_port=9000, internal_port=9000), "internal worker API port")
        self.assertRefused(settings(public_port=0), "JUDGE_PUBLIC_PORT=0")

    def test_all_problems_are_listed_at_once(self):
        self.assertRefused(settings(secret_key="CHANGE_ME", public_origin=None, public_port=8001),
                           "JUDGE_SECRET_KEY", "JUDGE_PUBLIC_ORIGIN", "internal worker API port")

    def test_from_env(self):
        s = Settings.from_env({"JUDGE_PUBLIC_ORIGIN": "https://x.ru", "JUDGE_PUBLIC_PORT": "8100",
                               "JUDGE_INTERNAL_PORT": "8101"})
        self.assertEqual((s.public_origin, s.public_port, s.internal_port), ("https://x.ru", 8100, 8101))
        d = Settings.from_env({})
        self.assertEqual((d.public_origin, d.public_port, d.internal_port), (None, 8000, 8001))


@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class OriginAndRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_public_app(settings(), session_factory=NoDatabase()),
                                 base_url="https://testserver")

    def test_unsafe_requests_need_our_origin(self):
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": "https://judge.example.ru.evil.example"}, {"Origin": "http://judge.example.ru"}):
            for path in ("/api/auth/login", "/api/auth/logout", "/api/auth/anything"):
                with self.subTest(headers=headers, path=path):
                    r = self.client.post(path, headers=headers, json={"login": "a", "password": "b"})
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "origin_not_allowed"))

    def test_internal_api_is_not_part_of_the_public_app(self):
        origin = {"Origin": "https://judge.example.ru"}
        for method, path in (("GET", "/internal/health"), ("GET", "/internal/metrics"), ("GET", "/internal/status"),
                             ("POST", "/internal/worker/v1/register"), ("POST", "/internal/worker/v1/claim")):
            with self.subTest(path=path):
                self.assertEqual(self.client.request(method, path, headers=origin).status_code, 404)

    def test_no_docs_are_published(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)


@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class ClientIpTests(unittest.TestCase):
    def ip(self, peer, *xff):
        headers = [(b"x-forwarded-for", v.encode()) for v in xff]
        return client_ip(Request({"type": "http", "client": (peer, 1234), "headers": headers}))

    def test_forwarded_for_is_trusted_only_from_localhost(self):
        self.assertEqual(self.ip("203.0.113.9"), "203.0.113.9")
        self.assertEqual(self.ip("203.0.113.9", "198.51.100.1"), "203.0.113.9")
        self.assertEqual(self.ip("127.0.0.1", "198.51.100.1"), "198.51.100.1")
        self.assertEqual(self.ip("::1", "2001:db8::1"), "2001:db8::1")
        # Caddy appends the real client: whatever the client put first is ignored
        self.assertEqual(self.ip("127.0.0.1", "10.0.0.1, 198.51.100.1"), "198.51.100.1")
        self.assertEqual(self.ip("127.0.0.1", "10.0.0.1", "198.51.100.1"), "198.51.100.1")
        self.assertEqual(self.ip("127.0.0.1", "garbage"), "127.0.0.1")
        self.assertEqual(self.ip("127.0.0.1"), "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
