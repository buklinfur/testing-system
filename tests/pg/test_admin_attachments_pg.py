"""/api/admin files and links of a feed item and the files of a work (docs/ADMIN_API.md §5.10, CLASSROOM.md §10,
D-082) on a real PostgreSQL: uploads of the raw body, every cheap refusal before a byte of the body is read (and
nothing left in the blob store's tmp), 413 at the limit of the settings (env) +-1, the item locked only inside the
service after the body, downloads with the headers of docs/FILES.md and 304, a file of another item or work is 404,
untrusted names and titles byte for byte, ids out of the int4 range are 422.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import asyncio
import dataclasses
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

CHUNK = 64 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"%" * 64
HTML = b"<html><script>alert(1)</script></html>"
SCRIPT = "<script>alert('x')</script> \"кавычки\" & <b>"  # untrusted text the rules accept
RAW = {"Content-Type": "application/octet-stream"}

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, func, select, text

    from app import blobstore
    from app import groups as g
    from app.admin_api.feed import STATUS
    from app.blobstore import BlobStore
    from app.classroom import attachments, items, works
    from app.classroom.attachments import LinkIn
    from app.models import Attachment, AuditLog, Blob, FeedItem, User
    from app.settings import AuthSettings, Settings


class Body:
    """An ASGI receive() serving `chunks` pieces of CHUNK bytes; counts how many were taken. `on_take(n)` runs
    before piece n is served (a probe while the upload is in progress)."""

    def __init__(self, chunks, data=b"x", on_take=None):
        self.chunks, self.taken, self.data, self.on_take = chunks, 0, data, on_take

    async def __call__(self):
        if self.taken >= self.chunks:
            return {"type": "http.disconnect"}
        if self.on_take is not None:
            self.on_take(self.taken)
        self.taken += 1
        return {"type": "http.request", "body": self.data * CHUNK, "more_body": self.taken < self.chunks}


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminAttachmentTests(ApiCase):
    ENV = {}

    def settings(self):
        self.blob_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.blob_tmp.cleanup)
        base = Settings.from_env(self.ENV)
        return dataclasses.replace(base, public_origin=ORIGIN, blob_dir=self.blob_tmp.name,
                                   timezone="Asia/Novosibirsk",
                                   auth=AuthSettings(secret_key=Fernet.generate_key().decode()))

    def setUp(self):
        super().setUp()
        self.blobs = BlobStore(self.blob_tmp.name)
        self.boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))
        self.ivanov, self.ivanov_password = self.make_student("Иванов", "Пётр")
        self.petrova, _ = self.make_student("Петрова", "Анна")
        self.group = g.create(self.s, self.cli, "7Б", self.now)
        g.add_members(self.s, self.cli, self.group, [self.ivanov, self.petrova], self.now)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def item(self, publish=True, **kw):
        body = {"kind": "assignment", "title": "Проект", "body_md": "Сделай", "grading": "pass_fail",
                "answer_kind": "text_or_files", "due_at": "2026-10-29 23:59", "group_ids": [self.group.id]}
        body.update(kw)
        r = self.b.post("/api/admin/items", json=body, headers=self.h)
        self.assertEqual(r.status_code, 201, r.text)
        made = r.json()
        if publish:
            self.assertEqual(self.b.post(f"/api/admin/items/{made['id']}/publish", headers=self.h).status_code, 200)
        return made["id"]

    def upload(self, item_id, name, data, headers=None):
        query = "" if name is None else f"?name={quote(name, safe='')}"
        return self.b.post(f"/api/admin/items/{item_id}/attachments{query}", content=data,
                           headers={**self.h, **RAW, **(headers or {})})

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json() if r.content and r.headers["content-type"].startswith("application/json") else None

    def refused(self, r, code, status=None):
        self.assertEqual((r.status_code, r.json()["detail"]), (status or STATUS.get(code, 422), code), r.text)
        return r.json()

    def stored(self):
        """(blob files in the store, files left in its tmp)."""
        root = Path(self.blob_tmp.name)
        blobs = sorted(p.name for p in root.glob("*/*")
                       if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))
        return blobs, sorted(p.name for p in (root / blobstore.TMP_DIR).glob("*"))

    def counts(self):
        self.s.expire_all()
        return [self.s.execute(select(func.count()).select_from(t)).scalar() for t in (Attachment, Blob, AuditLog)]

    def call(self, method, path, receive, query=b"", headers=(), session=True, csrf=True,
             content_type=b"application/octet-stream", origin=ORIGIN.encode()):
        """One request straight into the ASGI app, with the admin's cookie and CSRF token: (status, json)."""
        cookie = f"__Host-session={self.b.cookies.get('__Host-session')}".encode()
        hdrs = [(b"origin", origin), (b"content-type", content_type)]
        if session:
            hdrs.append((b"cookie", cookie))
        if csrf:
            hdrs.append((b"x-csrf-token", self.h["X-CSRF-Token"].encode()))
        hdrs += list(headers)
        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": query,
                 "headers": hdrs, "client": ("127.0.0.1", 5000), "server": ("testserver", 443), "scheme": "https",
                 "root_path": "", "http_version": "1.1", "asgi": {"version": "3.0"}}
        asyncio.run(self.app(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        return start["status"], body

    # ---------------------------------------------------------------- the teacher's files
    def test_upload_list_download_order_delete(self):
        item = self.item()
        made = self.ok(self.upload(item, "Условие задачи.pdf", PDF), 201)
        self.assertEqual((made["kind"], made["name"], made["size"], made["position"]),
                         ("file", "Условие задачи.pdf", len(PDF), 0))
        pic = self.ok(self.upload(item, "C:\\Users\\Пётр\\схема.PNG", PNG), 201)
        self.assertEqual(pic["name"], "схема.PNG")  # the base name (content_files.clean_name)
        link = self.ok(self.b.post(f"/api/admin/items/{item}/links", json={"url": " https://scratch.mit.edu/p/1 ",
                                                                          "title": SCRIPT}, headers=self.h), 201)
        self.assertEqual((link["url"], link["title"]), ("https://scratch.mit.edu/p/1", SCRIPT))
        listed = self.ok(self.b.get(f"/api/admin/items/{item}/attachments"))["items"]
        self.assertEqual([a["id"] for a in listed], [made["id"], pic["id"], link["id"]])
        self.assertIn(b"<script>alert('x')</script>", self.b.get(f"/api/admin/items/{item}/attachments").content)

        r = self.b.get(f"/api/admin/items/{item}/attachments/{made['id']}")
        self.assertEqual((r.status_code, r.content), (200, PDF))
        self.assertEqual(r.headers["content-type"], "application/octet-stream")
        self.assertTrue(r.headers["content-disposition"].startswith('attachment; filename="Uslovie_zadachi.pdf"'))
        self.assertEqual((r.headers["x-content-type-options"], r.headers["content-security-policy"]),
                         ("nosniff", "sandbox; default-src 'none'"))
        etag = r.headers["etag"]
        r2 = self.b.get(f"/api/admin/items/{item}/attachments/{made['id']}", headers={"If-None-Match": etag})
        self.assertEqual((r2.status_code, r2.content, r2.headers["etag"]), (304, b"", etag))
        r = self.b.get(f"/api/admin/items/{item}/attachments/{pic['id']}")
        self.assertEqual((r.headers["content-type"], r.headers["content-disposition"].split(";")[0]),
                         ("image/png", "inline"))

        order = self.ok(self.b.put(f"/api/admin/items/{item}/attachments/order",
                                   json={"ids": [link["id"], made["id"], pic["id"]]}, headers=self.h))
        self.assertEqual([a["id"] for a in order["items"]], [link["id"], made["id"], pic["id"]])
        r = self.b.post(f"/api/admin/items/{item}/attachments/{made['id']}/delete", headers=self.h)
        self.assertEqual(r.status_code, 204)
        self.refused(self.b.get(f"/api/admin/items/{item}/attachments/{made['id']}"), "attachment_not_found")
        self.assertEqual(len(self.stored()[0]), 2)  # the blob stays
        self.s.expire_all()
        actions = list(self.s.execute(select(AuditLog.action).where(AuditLog.action.like("item.attachment%"))
                                      .order_by(AuditLog.id)).scalars())
        self.assertEqual(actions, ["item.attachment_add"] * 3 + ["item.attachment_order", "item.attachment_remove"])
        self.assertEqual(self.s.execute(select(AuditLog.actor_user_id).where(AuditLog.action == "item.attachment_remove"))
                         .scalar(), self.boss.id)

    def test_refusals_and_their_codes(self):
        item, other = self.item(), self.item()
        before = self.stored()
        self.refused(self.upload(item, "pic.svg", b"<svg/>"), "file_type_not_allowed")
        self.refused(self.upload(item, "run.py.exe", b"MZ" * 10), "file_type_not_allowed")
        self.refused(self.upload(item, "fake.png", HTML), "file_content_mismatch")
        self.refused(self.upload(item, "empty.txt", b""), "file_empty")
        self.refused(self.upload(item, None, PDF), "invalid_request")
        self.refused(self.upload(item, "  ", PDF), "invalid_request")
        r = self.b.post(f"/api/admin/items/{item}/attachments?name=a.pdf&name=b.pdf", content=PDF,
                        headers={**self.h, **RAW})
        self.refused(r, "invalid_request")
        r = self.b.post(f"/api/admin/items/{item}/attachments?name=%FF%FE.pdf", content=PDF,
                        headers={**self.h, **RAW})
        self.assertIn("not UTF-8", self.refused(r, "invalid_request")["problems"][0])
        r = self.b.post(f"/api/admin/items/{item}/attachments?name=a.pdf", content=PDF,
                        headers={**self.h, "Content-Type": "application/pdf"})
        self.refused(r, "unsupported_media_type", 415)
        self.assertEqual(self.stored(), (before[0], []))

        made = self.ok(self.upload(item, "a.pdf", PDF), 201)
        self.refused(self.upload(item, "A.PDF", PDF + b"2"), "duplicate_name")
        self.refused(self.upload(999999, "a.pdf", PDF), "item_not_found")
        self.refused(self.upload(2 ** 31, "a.pdf", PDF), "invalid_request")
        self.refused(self.b.get(f"/api/admin/items/{other}/attachments/{made['id']}"), "attachment_not_found")
        self.refused(self.b.post(f"/api/admin/items/{other}/attachments/{made['id']}/delete", headers=self.h),
                     "attachment_not_found")
        link = self.ok(self.b.post(f"/api/admin/items/{item}/links", json={"url": "https://a.ru"}, headers=self.h),
                       201)
        self.refused(self.b.get(f"/api/admin/items/{item}/attachments/{link['id']}"), "attachment_not_found")
        self.refused(self.b.get(f"/api/admin/items/{item}/attachments/{2 ** 31}"), "invalid_request")
        for url in ("javascript:alert(1)", "https://good.ru@evil.example/", "ftp://a.ru", "h" * 2001):
            self.refused(self.b.post(f"/api/admin/items/{item}/links", json={"url": url}, headers=self.h),
                         "link_invalid")
        self.refused(self.b.post(f"/api/admin/items/{item}/links", json={"url": "https://a.ru", "title": "я" * 201},
                                 headers=self.h), "link_title_invalid")
        bad = self.refused(self.b.put(f"/api/admin/items/{item}/attachments/order", json={"ids": [made["id"]]},
                                      headers=self.h), "attachment_order_invalid")
        self.assertEqual((bad["missing"], bad["extra"], bad["repeated"]), ([link["id"]], [], []))
        self.refused(self.b.put(f"/api/admin/items/{item}/attachments/order", json={"ids": ["1"]}, headers=self.h),
                     "invalid_request")
        self.ok(self.b.post(f"/api/admin/items/{item}/archive", headers=self.h))
        self.refused(self.upload(item, "c.pdf", PDF), "item_archived")
        self.refused(self.b.post(f"/api/admin/items/{item}/links", json={"url": "https://b.ru"}, headers=self.h),
                     "item_archived")
        self.assertEqual(self.stored()[1], [])

    # ---------------------------------------------------------------- before the body
    def test_every_cheap_refusal_comes_before_the_body(self):
        """2.5 MB of body (over the JSON limit too): each refusal reads none of it, answers its own code rather than
        413, and leaves the blob store's tmp empty."""
        item = self.item()
        archived = self.item()
        self.ok(self.b.post(f"/api/admin/items/{archived}/archive", headers=self.h))
        student = self.browser(peer="student")
        r = self.login(student, self.ivanov.login, self.ivanov_password)
        student_cookie = f"__Host-session={student.cookies.get('__Host-session')}".encode()
        student_csrf = r.json()["csrf_token"].encode()
        path = f"/api/admin/items/{item}/attachments"
        cases = [
            ("another Origin", dict(origin=b"https://evil.example"), b"name=a.pdf", path, 403, "origin_not_allowed"),
            ("no session", dict(session=False), b"name=a.pdf", path, 401, "not_authenticated"),
            ("a student", dict(session=False, csrf=False,
                               headers=[(b"cookie", student_cookie), (b"x-csrf-token", student_csrf)]),
             b"name=a.pdf", path, 403, "admin_required"),
            ("no CSRF token", dict(csrf=False), b"name=a.pdf", path, 403, "csrf_token_required"),
            ("another Content-Type", dict(content_type=b"application/json"), b"name=a.pdf", path, 415,
             "unsupported_media_type"),
            ("no name", {}, b"", path, 422, "invalid_request"),
            ("name twice", {}, b"name=a.pdf&name=b.pdf", path, 422, "invalid_request"),
            ("broken UTF-8", {}, b"name=%C3%28.pdf", path, 422, "invalid_request"),
            ("not allowed by its name", {}, b"name=run.exe", path, 422, "file_type_not_allowed"),
            ("no such item", {}, b"name=a.pdf", "/api/admin/items/999999/attachments", 404, "item_not_found"),
            ("an archived item", {}, b"name=a.pdf", f"/api/admin/items/{archived}/attachments", 409,
             "item_archived"),
            ("an id over int4", {}, b"name=a.pdf", "/api/admin/items/2147483648/attachments", 422,
             "invalid_request"),
        ]
        for label, kw, query, where, status, code in cases:
            with self.subTest(label):
                body = Body(40)
                got, out = self.call("POST", where, body, query=query, **kw)
                self.assertEqual((got, body.taken), (status, 0), out[:200])
                self.assertIn(f'"detail":"{code}"'.encode(), out.replace(b" ", b""))
                self.assertEqual(self.stored()[1], [])
        # a duplicate name and a full item, decided from the database before the body as well
        self.ok(self.upload(item, "a.txt", b"1"), 201)
        body = Body(40)
        got, out = self.call("POST", path, body, query=b"name=A.TXT")
        self.assertEqual((got, body.taken), (422, 0), out)
        self.assertIn(b"duplicate_name", out)

    def test_the_item_is_locked_only_after_the_body(self):
        """While the body is being received the request holds no transaction: no other connection has any lock on
        feed_items of this schema (a transaction that read the item would hold AccessShareLock until its end), and
        another connection locks the row of the item with NOWAIT; the upload then succeeds."""
        item = self.item()
        engine = create_engine(URL, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(engine.dispose)
        probes = []

        def probe(n):
            if n == 2:
                with engine.begin() as conn:
                    conn.execute(text("SELECT id FROM feed_items WHERE id = :i FOR UPDATE NOWAIT"), {"i": item})
                    probes.append(conn.execute(text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                        "WHERE c.relname = 'feed_items' AND c.relnamespace = to_regnamespace(:s) "
                        "AND l.pid <> pg_backend_pid()"), {"s": self.schema}).scalar())

        body = Body(4, on_take=probe)
        got, out = self.call("POST", f"/api/admin/items/{item}/attachments", body, query=b"name=big.txt")
        self.assertEqual((got, body.taken), (201, 4), out)
        self.assertEqual(probes, [0])
        self.assertEqual(self.stored()[1], [])

    def test_a_change_during_the_upload_is_decided_under_the_lock(self):
        """precheck_file passed, then - while the body is being received - another teacher archives the item, attaches
        a file of the same name, or fills the limit: add_file decides on what is there under the lock, not on the
        early check, and the refused upload leaves nothing in the store or its tmp."""
        def change(kind, item_id):
            def on_take(n):
                if n != 1:
                    return
                with self.Session() as db:
                    item = db.get(FeedItem, item_id)
                    if kind == "archive":
                        items.archive(db, self.cli, item, self.now)
                    elif kind == "duplicate":
                        with self.blobs.incoming("Отчёт.TXT") as up:
                            up.write(b"other")
                            attachments.add_file(db, self.cli, item, up, self.now)
                    else:
                        for n in range(20):
                            attachments.add_link(db, self.cli, item, f"https://a.ru/{n}", self.now)
                    db.commit()
            return on_take

        for kind, status, code in (("archive", 409, "item_archived"), ("duplicate", 422, "duplicate_name"),
                                   ("limit", 409, "too_many_attachments")):
            with self.subTest(kind):
                item = self.item()
                blobs_before = self.stored()[0]
                body = Body(3, on_take=change(kind, item))
                got, out = self.call("POST", f"/api/admin/items/{item}/attachments", body, query=b"name=%D0%BE%D1%82%D1%87%D1%91%D1%82.txt")
                self.assertEqual((got, body.taken), (status, 3), out)
                self.assertIn(code.encode(), out)
                self.assertEqual(self.stored()[1], [])
                added = [b for b in self.stored()[0] if b not in blobs_before]
                self.assertEqual(len(added), 1 if kind == "duplicate" else 0)  # only the other teacher's file
                self.s.expire_all()
                names = list(self.s.execute(select(Attachment.name).where(Attachment.item_id == item,
                                                                          Attachment.name.is_not(None))).scalars())
                self.assertEqual(names, ["Отчёт.TXT"] if kind == "duplicate" else [])

    # ---------------------------------------------------------------- files of a work
    def test_work_files_by_the_pair_only(self):
        item, other = self.item(), self.item()
        with self.blobs.incoming("main.py") as py, self.blobs.incoming("отчёт.pdf") as pdf:
            py.write(b"print(1)\n")
            pdf.write(PDF)
            works.submit_work(self.s, self.s.get(User, self.ivanov.id), self.s.get(FeedItem, item), self.now,
                              blobs=self.blobs, text="Готово <b>", files=[py, pdf],
                              links=[LinkIn("https://github.com/x/y", SCRIPT)])
            self.s.commit()
        card = self.ok(self.b.get(f"/api/admin/items/{item}/works/{self.ivanov.id}"))
        files = {f["name"]: f for f in card["events"][0]["files"]}
        self.assertEqual(sorted(files), ["answer.md", "main.py", "отчёт.pdf"])
        self.assertEqual(card["events"][0]["links"][0]["title"], SCRIPT)
        base = f"/api/admin/items/{item}/works/{self.ivanov.id}/files"
        r = self.b.get(f"{base}/{files['answer.md']['id']}")
        self.assertEqual((r.status_code, r.content.decode()), (200, "Готово <b>"))
        self.assertEqual(r.headers["content-type"], "text/plain; charset=utf-8")
        r = self.b.get(f"{base}/{files['отчёт.pdf']['id']}")
        self.assertEqual((r.status_code, r.content, r.headers["content-type"]), (200, PDF, "application/octet-stream"))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        r2 = self.b.get(f"{base}/{files['отчёт.pdf']['id']}", headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual(r2.status_code, 304)
        fid = files["main.py"]["id"]
        self.refused(self.b.get(f"/api/admin/items/{item}/works/{self.petrova.id}/files/{fid}"), "file_not_found")
        self.refused(self.b.get(f"/api/admin/items/{other}/works/{self.ivanov.id}/files/{fid}"), "file_not_found")
        self.refused(self.b.get(f"{base}/999999"), "file_not_found")
        self.refused(self.b.get(f"{base}/{2 ** 31}"), "invalid_request")
        self.refused(self.b.get(f"/api/admin/items/{item}/works/999999/files/{fid}"), "user_not_found")


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminAttachmentLimitTests(AdminAttachmentTests):
    """The limits of the env reach the API channel: JUDGE_ATTACHMENT_MAX_MB=1 (413 too_large at 1 MB + 1, the body
    read only up to the limit), JUDGE_ITEM_ATTACHMENTS_MAX=2."""
    ENV = {"JUDGE_ATTACHMENT_MAX_MB": "1", "JUDGE_ITEM_ATTACHMENTS_MAX": "2"}

    def test_the_limits_of_the_env(self):
        self.assertEqual(self.app.state.settings.files.attachment_max_bytes, 1024 * 1024)
        item = self.item()
        mib = 1024 * 1024
        before = self.counts()
        r = self.upload(item, "big.txt", b"x" * (mib + 1))
        self.assertEqual((r.status_code, r.json()["detail"]), (413, "too_large"))
        self.assertEqual((self.counts(), self.stored()[1]), (before, []))
        body = Body(40)  # without Content-Length: cut right after the limit
        got, out = self.call("POST", f"/api/admin/items/{item}/attachments", body, query=b"name=big.txt")
        self.assertEqual((got, body.taken), (413, mib // CHUNK + 1), out)
        self.assertEqual(self.stored()[1], [])
        self.ok(self.upload(item, "exact.txt", b"x" * mib), 201)
        self.ok(self.upload(item, "less.txt", b"x" * (mib - 1)), 201)
        self.refused(self.upload(item, "third.txt", b"x"), "too_many_attachments")
        self.refused(self.b.post(f"/api/admin/items/{item}/links", json={"url": "https://a.ru"}, headers=self.h),
                     "too_many_attachments")

    # the inherited scenarios run with the default limits only
    test_upload_list_download_order_delete = None
    test_refusals_and_their_codes = None
    test_every_cheap_refusal_comes_before_the_body = None
    test_the_item_is_locked_only_after_the_body = None
    test_work_files_by_the_pair_only = None
    test_a_change_during_the_upload_is_decided_under_the_lock = None
