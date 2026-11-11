"""Request body limits and the error format of the public app, at the ASGI level (no database): a refused body is
never read beyond the limit."""
import asyncio
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

try:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    HAVE_API = True
except ImportError:
    HAVE_API = False

if HAVE_API:
    from cryptography.fernet import Fernet

    from app import api_errors
    from app.http_body import JSON_LIMIT, BodyLimitMiddleware, require_type, save_body
    from app.public_main import create_public_app
    from app.settings import AuthSettings, Settings

CHUNK = 64 * 1024
ORIGIN = "https://judge.example.ru"


class Body:
    """An ASGI receive() that serves `chunks` pieces of CHUNK bytes and counts how many were taken."""

    def __init__(self, chunks):
        self.chunks, self.taken = chunks, 0

    async def __call__(self):
        if self.taken >= self.chunks:
            return {"type": "http.disconnect"}
        self.taken += 1
        return {"type": "http.request", "body": b"x" * CHUNK, "more_body": self.taken < self.chunks}


def call(app, method, path, receive, headers=()):
    sent = []

    async def send(message):
        sent.append(message)

    given = {name for name, _v in headers}
    default = [] if b"content-type" in given else [(b"content-type", b"application/json")]
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": [(b"origin", ORIGIN.encode()), *default, *headers],
             "client": ("127.0.0.1", 5000), "server": ("testserver", 443), "scheme": "https", "root_path": "",
             "http_version": "1.1", "asgi": {"version": "3.0"}}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


class NoDatabase:
    def __call__(self):
        raise AssertionError("the request must be refused before any database access")


class IdleDatabase:
    """A session that is opened (the request dependencies run before the body is checked) but never used."""

    def __call__(self):
        return self

    def execute(self, *_a, **_k):
        raise AssertionError("no query expected")

    def close(self):
        pass

    def rollback(self):
        pass


@unittest.skipUnless(HAVE_API, "needs fastapi")
class BodyLimitTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()))
        self.app = create_public_app(settings, session_factory=NoDatabase())

    def test_a_declared_length_over_the_limit_is_refused_without_reading(self):
        body = Body(40)
        status, headers, out = call(self.app, "POST", "/api/auth/login", body,
                                    [(b"content-length", str(40 * CHUNK).encode())])
        self.assertEqual((status, body.taken), (413, 0))
        self.assertIn(b'"detail": "too_large"', out)
        self.assertEqual(headers[b"cache-control"], b"no-store")

    def test_a_chunked_body_is_cut_as_soon_as_it_passes_the_limit(self):
        body = Body(40)  # 2.5 MB, no Content-Length
        status, _h, _out = call(self.app, "POST", "/api/auth/login", body)
        self.assertEqual(status, 413)
        self.assertEqual(body.taken, JSON_LIMIT // CHUNK + 1)  # 17 pieces read, 23 never touched

    def test_a_body_under_the_limit_reaches_the_app_whole(self):
        seen = {}

        async def app(scope, receive, send):
            message = await receive()
            seen["body"], seen["more"] = len(message["body"]), message["more_body"]
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        status, _h, _out = call(BodyLimitMiddleware(app), "POST", "/x", Body(10))
        self.assertEqual((status, seen["body"], seen["more"]), (200, 10 * CHUNK, False))

    def test_exempt_uploads_read_their_own_body(self):
        mini = FastAPI()
        api_errors.install(mini)

        @mini.post("/upload")
        async def upload(request: Request):
            require_type(request, "application/zip")
            out = io.BytesIO()
            return {"bytes": await save_body(request, out, 5 * CHUNK)}

        app = BodyLimitMiddleware(mini, limit=CHUNK, exempt=frozenset({("POST", "/upload")}))
        zip_type = [(b"content-type", b"application/zip")]

        def post(chunks, length=None, kind=zip_type):
            body = Body(chunks)
            extra = [(b"content-length", str(length).encode())] if length is not None else []
            scope_headers = [h for h in kind] + extra
            status, _h, out = call(app, "POST", "/upload", body, scope_headers)
            return status, body.taken, out

        self.assertEqual(post(4)[:2], (200, 4))  # over the JSON limit, under its own
        self.assertEqual(post(40)[:2], (413, 6))  # counted: stops right after the limit
        self.assertEqual(post(40, length=40 * CHUNK)[:2], (413, 0))  # declared: nothing read
        status, taken, out = post(2, kind=[(b"content-type", b"text/html")])
        self.assertEqual((status, taken), (415, 0))
        self.assertIn(b"unsupported_media_type", out)


    def test_only_the_exact_upload_routes_are_exempt(self):
        """Variants of an upload path keep the 1 MB limit: the body is cut after 17 pieces of 64 KB."""
        from app.admin_api import UPLOADS
        self.assertEqual(UPLOADS, {("POST", "/api/admin/problems"), ("POST", "/api/admin/users/import")})
        variants = [
            ("POST", "/api/admin/problems/"), ("POST", "//api/admin/problems"), ("POST", "/api/admin//problems"),
            ("POST", "/api/admin/problems/x"), ("POST", "/api/admin/problemsx"), ("POST", "/API/admin/problems"),
            ("POST", "/api/admin%2Fproblems"),  # %2F as a server that does not decode it would pass it on
            ("PUT", "/api/admin/problems"), ("PATCH", "/api/admin/problems"), ("DELETE", "/api/admin/problems"),
            ("POST", "/api/admin/users/import/"), ("POST", "/api/admin/users/import2"),
            ("POST", "/api/admin/users//import"), ("PUT", "/api/admin/users/import"),
        ]
        for method, path in variants:
            with self.subTest(method=method, path=path):
                body = Body(40)
                status, _h, _out = call(self.app, method, path, body)
                self.assertEqual((status, body.taken), (413, JSON_LIMIT // CHUNK + 1))
        seen = []

        async def handler(scope, receive, send):  # stands for the upload handler: what did it get?
            seen.append(body.taken)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        layer = BodyLimitMiddleware(handler, exempt=UPLOADS)
        for method, path in sorted(UPLOADS):  # the exact pairs: the body reaches the handler untouched
            with self.subTest(exact=path):
                body = Body(40)
                status, _h, _out = call(layer, method, path, body)
                self.assertEqual((status, seen[-1]), (200, 0))


    def test_only_the_upload_pattern_itself_is_exempt(self):
        """/api/admin/items/<id>/attachments (POST) reads its own body; every neighbour keeps the 1 MB limit."""
        from app.admin_api import UPLOAD_PATTERNS
        self.assertEqual([(m, p.pattern) for m, p in UPLOAD_PATTERNS],
                         [("POST", "/api/admin/items/[1-9][0-9]{0,9}/attachments")])
        base = "/api/admin/items/5/attachments"
        variants = [
            ("POST", base + "/"), ("POST", "/" + base), ("POST", "/api/admin//items/5/attachments"),
            ("POST", "/api/admin/items//5/attachments"), ("POST", "/api/admin/items/5//attachments"),
            ("POST", "/api/admin/items/007/attachments"), ("POST", "/api/admin/items/0/attachments"),
            ("POST", "/api/admin/items/-5/attachments"), ("POST", "/api/admin/items/+5/attachments"),
            ("POST", "/api/admin/items/x/attachments"), ("POST", "/api/admin/items/12345678901/attachments"),
            ("POST", "/api/admin/items/5/attachmentsx"), ("POST", "/api/admin/items/5/attachments/9"),
            ("POST", "/api/admin/items/5/attachments/order"), ("POST", "/api/admin/items/5/links"),
            ("POST", "/api/admin/items/5%2Fattachments"), ("POST", "/API/admin/items/5/attachments"),
            ("POST", "/api/admin/items/5/Attachments"), ("POST", "/api/admin/items/5/attachments\n"),
            ("POST", "/api/admin/items/5/attachments/9/delete"), ("POST", "/x/api/admin/items/5/attachments"),
            ("PUT", base), ("PATCH", base), ("DELETE", base), ("GET", base), ("PUT", base + "/order"),
        ]
        for method, path in variants:
            with self.subTest(method=method, path=path):
                body = Body(40)
                status, _h, _out = call(self.app, method, path, body)
                self.assertEqual((status, body.taken), (413, JSON_LIMIT // CHUNK + 1))
        seen = []

        async def handler(scope, receive, send):
            seen.append(body.taken)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        layer = BodyLimitMiddleware(handler, exempt_patterns=UPLOAD_PATTERNS)
        for path in ("/api/admin/items/1/attachments", base, "/api/admin/items/2147483647/attachments",
                     "/api/admin/items/9999999999/attachments"):
            with self.subTest(exact=path):
                body = Body(40)
                status, _h, _out = call(layer, "POST", path, body)
                self.assertEqual((status, seen[-1]), (200, 0))


@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class ErrorFormatTests(unittest.TestCase):
    def setUp(self):
        settings = Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()))
        self.client = TestClient(create_public_app(settings, session_factory=IdleDatabase()),
                                 base_url="https://testserver", headers={"Origin": ORIGIN})

    def test_one_format_everywhere(self):
        r = self.client.get("/nowhere")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "not_found"}))
        r = self.client.get("/api/auth/login")
        self.assertEqual((r.status_code, r.json()), (405, {"detail": "method_not_allowed"}))
        r = self.client.post("/api/auth/login", json={"login": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]), (422, "invalid_request"))
        self.assertEqual(r.json()["problems"], ["password: Field required"])
        r = self.client.post("/api/auth/login", json={}, headers={"Origin": "https://evil.example"})
        self.assertEqual((r.status_code, r.json()), (403, {"detail": "origin_not_allowed"}))


if __name__ == "__main__":
    unittest.main()
