"""Internal API checks that need FastAPI but no PostgreSQL: requests that came through a proxy are refused."""
import sys
import tempfile
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
    from app.internal_main import PROXY_HEADERS, create_internal_app
    from app.settings import Settings


class FakeSession:
    """Enough of a Session for /internal/health; records that the request reached the handler."""

    def __init__(self, calls):
        self.calls = calls

    def execute(self, *args, **kwargs):
        self.calls.append("execute")

    def rollback(self):
        pass

    def close(self):
        pass


@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class ProxyHeaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls = []
        app = create_internal_app(Settings(blob_dir=tmp.name), session_factory=lambda: FakeSession(self.calls))
        self.client = TestClient(app)

    def test_direct_request_passes(self):
        r = self.client.get("/internal/health")
        self.assertEqual((r.status_code, r.json()), (200, {"ok": True}))
        self.assertEqual(self.calls, ["execute"])

    def test_caddy_default_headers_are_refused(self):
        # what Caddy's reverse_proxy adds to every request by default
        for name, value in (("X-Forwarded-For", "203.0.113.7"), ("X-Forwarded-Proto", "https"),
                            ("X-Forwarded-Host", "judge.example.ru")):
            with self.subTest(header=name):
                r = self.client.get("/internal/health", headers={name: value})
                self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [], "refused before any handler or database access")

    def test_every_listed_header_is_refused_in_any_case(self):
        for name in PROXY_HEADERS:
            for spelled in (name, name.upper(), name.title()):
                with self.subTest(header=spelled):
                    r = self.client.get("/internal/health", headers={spelled: "x"})
                    self.assertEqual(r.status_code, 403)
                    self.assertIn("proxy", r.json()["detail"])

    def test_required_headers_are_listed(self):
        for name in ("x-forwarded-for", "x-forwarded-host", "forwarded", "cf-connecting-ip", "cf-ray"):
            self.assertIn(name, PROXY_HEADERS)

    def test_worker_endpoints_are_refused_before_authentication(self):
        r = self.client.post("/internal/worker/v1/register", headers={"Forwarded": "for=203.0.113.7",
                                                                       "Authorization": "Bearer anything"},
                             json={"name": "w0"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.calls, [])

    def test_empty_header_value_still_counts(self):
        r = self.client.get("/internal/health", headers={"X-Forwarded-For": ""})
        self.assertEqual(r.status_code, 403)



class LocalhostOnlyTests(unittest.TestCase):
    """The internal API listens on localhost only (the first line of defence; the proxy headers are the second)."""

    def test_the_systemd_unit_binds_localhost(self):
        unit = (ROOT / "deploy/systemd/judge-api-internal.service").read_text()
        exec_start = next(line for line in unit.splitlines() if line.startswith("ExecStart="))
        self.assertIn("app.internal_main:app", exec_start)
        self.assertIn("--host 127.0.0.1", exec_start)
        self.assertNotIn("0.0.0.0", unit)
        self.assertNotIn("--proxy-headers", exec_start)

    def test_the_worker_default_url_is_local(self):
        env = (ROOT / "deploy/env/worker.env.example").read_text()
        self.assertIn("JUDGE_API_URL=http://127.0.0.1:8001", env)


if __name__ == "__main__":
    unittest.main()
