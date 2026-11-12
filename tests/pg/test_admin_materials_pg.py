"""/api/admin the library of materials and «Справочник» (docs/ADMIN_API.md §5.11, CLASSROOM.md §11, D-087) on a real
PostgreSQL: materials and their versions, a fixed slug (PATCH: 422 slug_immutable), link warnings that never refuse,
the admin's resolution ok / archived / unknown, the library groups, archive answered with changed; files of a version
uploaded as in §5.10 - every cheap refusal before a byte of the body is read (and nothing left in the blob store's
tmp), the material locked only inside the service after the body, a change during the upload decided under the lock,
413 at the limit of the settings - downloaded with the headers of docs/FILES.md, of an older version with ?version=;
sections two levels deep, their order, the materials in them, the search by title in any case; ids beyond int4 are
422; untrusted text byte for byte.

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
    from app.materials import service as materials
    from app.models import Attachment, AuditLog, Blob, Material, MaterialVersion
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
class AdminMaterialTests(ApiCase):
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
        self.boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))
        self.ivanov, self.ivanov_password = self.make_student("Иванов", "Пётр")
        self.group = g.create(self.s, self.cli, "7Б", self.now)
        self.old_group = g.create(self.s, self.cli, "6А", self.now)
        g.archive(self.s, self.cli, self.old_group, self.now)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json() if r.content and r.headers["content-type"].startswith("application/json") else None

    def refused(self, r, code, status=None):
        self.assertEqual((r.status_code, r.json()["detail"]), (status or STATUS.get(code, 422), code), r.text)
        return r.json()

    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers=self.h, **kw)

    def put(self, path, **kw):
        return self.b.put(f"/api/admin{path}", headers=self.h, **kw)

    def patch(self, path, **kw):
        return self.b.patch(f"/api/admin{path}", headers=self.h, **kw)

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def material(self, slug="cycles", title="Циклы", body_md="Цикл for", **kw):
        return self.ok(self.post("/materials", json={"slug": slug, "title": title, "body_md": body_md, **kw}), 201)

    def upload(self, material_id, name, data, headers=None):
        query = "" if name is None else f"?name={quote(name, safe='')}"
        return self.b.post(f"/api/admin/materials/{material_id}/attachments{query}", content=data,
                           headers={**self.h, **RAW, **(headers or {})})

    def stored(self):
        """(blob files in the store, files left in its tmp)."""
        root = Path(self.blob_tmp.name)
        blobs = sorted(p.name for p in root.glob("*/*")
                       if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))
        return blobs, sorted(p.name for p in (root / blobstore.TMP_DIR).glob("*"))

    def counts(self):
        self.s.expire_all()
        return [self.s.execute(select(func.count()).select_from(t)).scalar()
                for t in (Attachment, Blob, AuditLog, MaterialVersion)]

    def call(self, method, path, receive, query=b"", headers=(), session=True, csrf=True,
             content_type=b"application/octet-stream", origin=ORIGIN.encode()):
        """One request straight into the ASGI app, with the admin's cookie and CSRF token: (status, body)."""
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

    # ---------------------------------------------------------------- materials and versions
    def test_materials_versions_slug_and_links(self):
        lists = self.material("lists", "Списки", "")
        made = self.material(body_md="См. [[памятка:lists]], [[памятка:later]] и [[памятка:Bad Slug]]",
                             library="groups", group_ids=[self.group.id])
        self.assertEqual((made["slug"], made["version"], made["versions"], made["library"]),
                         ("cycles", 1, 1, "groups"))
        self.assertEqual([x["name"] for x in made["groups"]], ["7Б"])
        self.assertEqual(len(made["warnings"]), 2)  # unknown later, malformed Bad Slug
        self.assertEqual(made["warnings"], made["link_warnings"])
        self.assertEqual([(r["slug"], r["status"]) for r in made["links"]], [("lists", "ok"), ("later", "unknown")])
        mid = made["id"]

        # the slug is fixed; the title and the audience change in place
        self.refused(self.patch(f"/materials/{mid}", json={"slug": "loops"}), "slug_immutable")
        self.assertEqual(self.ok(self.patch(f"/materials/{mid}", json={"slug": "cycles"}))["changed"], [])
        self.refused(self.patch(f"/materials/{mid}", json={"title": None}), "invalid_request")
        self.refused(self.patch(f"/materials/{mid}", json={"version": 3}), "invalid_request")
        out = self.ok(self.patch(f"/materials/{mid}", json={"title": "Циклы и ветвления", "library": "all_students"}))
        self.assertEqual((sorted(out["changed"]), out["material"]["title"]), (["library", "title"],
                                                                               "Циклы и ветвления"))
        self.refused(self.patch(f"/materials/{mid}", json={"library": "все"}), "library_invalid")

        # versions: a new text, the same text again, the history, an older one
        v2 = self.ok(self.post(f"/materials/{mid}/versions", json={"body_md": "Только [[памятка:lists]]"}))
        self.assertEqual((v2["version"], v2["created"], v2["warnings"]), (2, True, []))
        same = self.ok(self.post(f"/materials/{mid}/versions", json={"body_md": "Только [[памятка:lists]]"}))
        self.assertEqual((same["version"], same["created"]), (2, False))
        self.assertEqual([v["version"] for v in self.ok(self.get(f"/materials/{mid}/versions"))["items"]], [2, 1])
        v1 = self.ok(self.get(f"/materials/{mid}/versions/1"))
        self.assertTrue(v1["body_md"].startswith("См. "))
        self.refused(self.get(f"/materials/{mid}/versions/3"), "version_not_found")
        self.refused(self.get(f"/materials/{mid}/versions/{2 ** 31}"), "invalid_request")
        self.refused(self.get(f"/materials/{2 ** 31}"), "invalid_request")
        self.refused(self.get("/materials/999999"), "material_not_found")

        # resolve: the admin's view, nothing written
        before = self.counts()
        res = self.ok(self.post("/materials/resolve", json={
            "text": "[[памятка:cycles]] [[памятка:lists|списки]] [[памятка:nope]] [[памятка:]]"}))
        self.assertEqual([(r["slug"], r["status"]) for r in res["links"]],
                         [("cycles", "ok"), ("lists", "ok"), ("nope", "unknown")])
        self.assertEqual(len(res["warnings"]), 2)
        self.assertEqual(self.counts(), before)

        # archive: idempotent, read only, still resolved as archived; groups of the library
        self.assertTrue(self.ok(self.post(f"/materials/{lists['id']}/archive"))["changed"])
        self.assertFalse(self.ok(self.post(f"/materials/{lists['id']}/archive"))["changed"])
        res = self.ok(self.post("/materials/resolve", json={"text": "[[памятка:lists]]"}))
        self.assertEqual(res["links"][0]["status"], "archived")
        self.refused(self.post(f"/materials/{lists['id']}/versions", json={"body_md": "x"}), "material_archived")
        self.refused(self.patch(f"/materials/{lists['id']}", json={"title": "Новое"}), "material_archived")
        self.assertEqual([m["slug"] for m in self.ok(self.get("/materials"))["items"]], ["cycles"])
        self.assertEqual([m["slug"] for m in self.ok(self.get("/materials?archived=true"))["items"]],
                         ["cycles", "lists"])
        self.assertTrue(self.ok(self.post(f"/materials/{lists['id']}/unarchive"))["changed"])
        self.assertFalse(self.ok(self.post(f"/materials/{lists['id']}/unarchive"))["changed"])
        self.refused(self.put(f"/materials/{mid}/groups", json={"group_ids": [self.old_group.id]}), "group_archived")
        self.refused(self.put(f"/materials/{mid}/groups", json={"group_ids": [999999]}), "group_not_found")
        self.assertEqual(self.ok(self.put(f"/materials/{mid}/groups", json={"group_ids": []}))["groups"], [])
        self.refused(self.post("/materials", json={"slug": "cycles", "title": "Ещё"}), "slug_taken")

        # where it is used; a material item through the feed
        item = self.ok(self.post("/items", json={"kind": "material", "title": "", "material": "cycles"}), 201)
        self.assertEqual((item["title"], item["material"]["slug"]), ("Циклы и ветвления", "cycles"))
        task = self.ok(self.post("/items", json={"kind": "assignment", "title": "З", "grading": "none",
                                                 "body_md": "Читай [[памятка:cycles]] и [[памятка:zzz]]"}), 201)
        self.assertEqual(len(task["link_warnings"]), 1)
        where = self.ok(self.get(f"/materials/{mid}/where"))
        self.assertEqual(([i["id"] for i in where["material_items"]], [i["id"] for i in where["linking_items"]]),
                         ([item["id"]], [task["id"]]))
        self.assertEqual(self.ok(self.get(f"/materials/{lists['id']}/where"))["materials"], ["cycles"])

    def test_untrusted_text_byte_for_byte(self):
        made = self.material("script", SCRIPT, SCRIPT + "\n[[памятка:x|" + SCRIPT.replace("<b>", "") + "]]")
        got = self.ok(self.get(f"/materials/{made['id']}"))
        self.assertEqual((got["title"], got["body_md"].split("\n")[0]), (SCRIPT, SCRIPT))
        link = self.ok(self.post(f"/materials/{made['id']}/links",
                                 json={"url": "https://a.ru/?q=<script>", "title": SCRIPT}), 201)
        self.assertEqual(link["attachments"][0]["title"], SCRIPT)
        raw = b'{"slug": "lone", "title": "M", "body_md": "\\ud800"}'  # a lone surrogate (JSON escape)
        self.refused(self.b.post("/api/admin/materials", content=raw,
                                 headers={**self.h, "Content-Type": "application/json"}), "invalid_request")

    # ---------------------------------------------------------------- attachments of a version
    def test_attachments_make_versions_and_files_are_served(self):
        mid = self.material()["id"]
        a = self.ok(self.upload(mid, "Условие.pdf", PDF), 201)
        self.assertEqual((a["version"], [x["name"] for x in a["attachments"]]), (2, ["Условие.pdf"]))
        b = self.ok(self.upload(mid, "схема.png", PNG), 201)
        link = self.ok(self.post(f"/materials/{mid}/links", json={"url": " https://a.ru ", "title": " Пример "}), 201)
        self.assertEqual((link["version"], link["attachments"][-1]["url"], link["attachments"][-1]["title"]),
                         (4, "https://a.ru", "Пример"))
        ids = [x["id"] for x in link["attachments"]]
        order = self.ok(self.put(f"/materials/{mid}/attachments/order", json={"ids": [ids[2], ids[0], ids[1]]}))
        self.assertEqual((order["version"], order["created"]), (5, True))
        new_ids = [x["id"] for x in order["attachments"]]
        same = self.ok(self.put(f"/materials/{mid}/attachments/order", json={"ids": new_ids}))
        self.assertEqual((same["version"], same["created"]), (5, False))
        bad = self.refused(self.put(f"/materials/{mid}/attachments/order", json={"ids": ids}),
                           "attachment_order_invalid")
        self.assertEqual(sorted(bad["missing"]), sorted(new_ids))

        pdf = new_ids[1]
        r = self.get(f"/materials/{mid}/attachments/{pdf}")
        self.assertEqual((r.status_code, r.content), (200, PDF))
        self.assertEqual(r.headers["x-content-type-options"], "nosniff")
        r2 = self.get(f"/materials/{mid}/attachments/{pdf}", headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual(r2.status_code, 304)
        self.refused(self.get(f"/materials/{mid}/attachments/{a['attachments'][0]['id']}"), "attachment_not_found")
        r = self.get(f"/materials/{mid}/attachments/{a['attachments'][0]['id']}?version=2")
        self.assertEqual((r.status_code, r.content), (200, PDF))
        self.refused(self.get(f"/materials/{mid}/attachments/{pdf}?version=9"), "version_not_found")
        self.refused(self.get(f"/materials/{mid}/attachments/{new_ids[0]}"), "attachment_not_found")  # a link
        other = self.material("other", "Другой", "")["id"]
        self.refused(self.get(f"/materials/{other}/attachments/{pdf}"), "attachment_not_found")

        gone = self.ok(self.post(f"/materials/{mid}/attachments/{pdf}/delete"))
        self.assertEqual((gone["version"], len(gone["attachments"])), (6, 2))
        self.refused(self.post(f"/materials/{mid}/attachments/{pdf}/delete"), "attachment_not_found")
        self.assertEqual(len(self.stored()[0]), 2)  # the blobs stay with the old versions
        self.assertEqual(b["version"], 3)

    def test_refusals_and_their_codes(self):
        mid = self.material()["id"]
        before = self.stored()
        self.refused(self.upload(mid, "pic.svg", b"<svg/>"), "file_type_not_allowed")
        self.refused(self.upload(mid, "fake.png", HTML), "file_content_mismatch")
        self.refused(self.upload(mid, "empty.txt", b""), "file_empty")
        self.refused(self.upload(mid, None, PDF), "invalid_request")
        r = self.b.post(f"/api/admin/materials/{mid}/attachments?name=%FF%FE.pdf", content=PDF,
                        headers={**self.h, **RAW})
        self.assertIn("not UTF-8", self.refused(r, "invalid_request")["problems"][0])
        r = self.b.post(f"/api/admin/materials/{mid}/attachments?name=a.pdf", content=PDF,
                        headers={**self.h, "Content-Type": "application/pdf"})
        self.refused(r, "unsupported_media_type", 415)
        self.assertEqual(self.stored(), (before[0], []))
        self.ok(self.upload(mid, "a.pdf", PDF), 201)
        self.refused(self.upload(mid, "A.PDF", PDF + b"2"), "duplicate_name")
        self.refused(self.upload(999999, "a.pdf", PDF), "material_not_found")
        self.refused(self.upload(2 ** 31, "a.pdf", PDF), "invalid_request")
        for url in ("javascript:alert(1)", "ftp://a.ru"):
            self.refused(self.post(f"/materials/{mid}/links", json={"url": url}), "link_invalid")
        self.ok(self.post(f"/materials/{mid}/archive"))
        self.refused(self.upload(mid, "c.pdf", PDF), "material_archived")
        self.refused(self.post(f"/materials/{mid}/links", json={"url": "https://b.ru"}), "material_archived")
        self.assertEqual(self.stored()[1], [])

    def test_every_cheap_refusal_comes_before_the_body(self):
        """2.5 MB of body (over the JSON limit too): each refusal reads none of it, answers its own code rather than
        413, and leaves the blob store's tmp empty."""
        mid = self.material()["id"]
        archived = self.material("old", "Старый", "")["id"]
        self.ok(self.post(f"/materials/{archived}/archive"))
        student = self.browser(peer="student")
        r = self.login(student, self.ivanov.login, self.ivanov_password)
        student_cookie = f"__Host-session={student.cookies.get('__Host-session')}".encode()
        student_csrf = r.json()["csrf_token"].encode()
        path = f"/api/admin/materials/{mid}/attachments"
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
            ("no such material", {}, b"name=a.pdf", "/api/admin/materials/999999/attachments", 404,
             "material_not_found"),
            ("an archived material", {}, b"name=a.pdf", f"/api/admin/materials/{archived}/attachments", 409,
             "material_archived"),
            ("an id over int4", {}, b"name=a.pdf", "/api/admin/materials/2147483648/attachments", 422,
             "invalid_request"),
        ]
        for label, kw, query, where, status, code in cases:
            with self.subTest(label):
                body = Body(40)
                got, out = self.call("POST", where, body, query=query, **kw)
                self.assertEqual((got, body.taken), (status, 0), out[:200])
                self.assertIn(f'"detail":"{code}"'.encode(), out.replace(b" ", b""))
                self.assertEqual(self.stored()[1], [])
        # a duplicate name, decided from the latest version before the body as well
        self.ok(self.upload(mid, "a.txt", b"1"), 201)
        body = Body(40)
        got, out = self.call("POST", path, body, query=b"name=A.TXT")
        self.assertEqual((got, body.taken), (422, 0), out)
        self.assertIn(b"duplicate_name", out)

    def test_the_material_is_locked_only_after_the_body(self):
        """While the body is being received the request holds no transaction: no other connection has any lock on
        materials of this schema, and another connection locks the row of the material with NOWAIT."""
        mid = self.material()["id"]
        engine = create_engine(URL, connect_args={"options": f"-csearch_path={self.schema}"})
        self.addCleanup(engine.dispose)
        probes = []

        def probe(n):
            if n == 2:
                with engine.begin() as conn:
                    conn.execute(text("SELECT id FROM materials WHERE id = :i FOR UPDATE NOWAIT"), {"i": mid})
                    probes.append(conn.execute(text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                        "WHERE c.relname = 'materials' AND c.relnamespace = to_regnamespace(:s) "
                        "AND l.pid <> pg_backend_pid()"), {"s": self.schema}).scalar())

        body = Body(4, on_take=probe)
        got, out = self.call("POST", f"/api/admin/materials/{mid}/attachments", body, query=b"name=big.txt")
        self.assertEqual((got, body.taken), (201, 4), out)
        self.assertEqual(probes, [0])
        self.assertEqual(self.stored()[1], [])

    def test_a_change_during_the_upload_is_decided_under_the_lock(self):
        """precheck_file passed, then - while the body is being received - another teacher archives the material or
        attaches a file of the same name: add_file decides under the lock, and the refused upload leaves nothing."""
        def change(kind, material_id):
            def on_take(n):
                if n != 1:
                    return
                with self.Session() as db:
                    material = db.get(Material, material_id)
                    if kind == "archive":
                        materials.archive(db, self.cli, material, self.now)
                    else:
                        with blobstore.BlobStore(self.blob_tmp.name).incoming("Отчёт.TXT") as up:
                            up.write(b"other")
                            materials.add_file(db, self.cli, material, up, self.now)
                    db.commit()
            return on_take

        for n, (kind, status, code) in enumerate((("archive", 409, "material_archived"),
                                                  ("duplicate", 422, "duplicate_name"))):
            with self.subTest(kind):
                mid = self.material(f"race-{n}", "Гонка", "")["id"]
                blobs_before = self.stored()[0]
                body = Body(3, on_take=change(kind, mid))
                got, out = self.call("POST", f"/api/admin/materials/{mid}/attachments", body,
                                     query=b"name=%D0%BE%D1%82%D1%87%D1%91%D1%82.txt")
                self.assertEqual((got, body.taken), (status, 3), out)
                self.assertIn(code.encode(), out)
                self.assertEqual(self.stored()[1], [])
                added = [x for x in self.stored()[0] if x not in blobs_before]
                self.assertEqual(len(added), 1 if kind == "duplicate" else 0)
                versions = self.ok(self.get(f"/materials/{mid}/versions"))["items"]
                self.assertEqual(len(versions), 2 if kind == "duplicate" else 1)

    # ---------------------------------------------------------------- the library
    def test_sections_order_and_search(self):
        cycles = self.material(library="all_students")["id"]
        lists = self.material("lists", "Списки", "")["id"]
        pct = self.material("pct", "100% и a_b", "")["id"]
        top = self.ok(self.post("/library/sections", json={"title": "Основы"}), 201)["id"]
        other = self.ok(self.post("/library/sections", json={"title": "Ещё"}), 201)["id"]
        sub = self.ok(self.post("/library/sections", json={"title": "Циклы", "parent_id": top}), 201)["id"]
        self.refused(self.post("/library/sections", json={"title": "Глубже", "parent_id": sub}), "section_too_deep")
        self.assertTrue(self.ok(self.post(f"/library/sections/{sub}/materials", json={"material_id": cycles}))
                        ["changed"])
        self.ok(self.post(f"/library/sections/{sub}/materials", json={"material_id": lists}))
        self.assertFalse(self.ok(self.post(f"/library/sections/{sub}/materials", json={"material_id": lists}))
                         ["changed"])
        self.refused(self.post(f"/library/sections/{sub}/materials", json={"material_id": 999999}),
                     "material_not_found")
        self.ok(self.put(f"/library/sections/{sub}/materials/order", json={"ids": [lists, cycles]}))
        bad = self.refused(self.put(f"/library/sections/{sub}/materials/order", json={"ids": [lists]}),
                           "order_invalid")
        self.assertEqual(bad["missing"], [cycles])
        self.ok(self.put("/library/sections/order", json={"ids": [other, top]}))
        self.refused(self.put("/library/sections/order", json={"ids": [sub], "parent_id": None}), "order_invalid")
        self.ok(self.put("/library/sections/order", json={"ids": [sub], "parent_id": top}))
        view = self.ok(self.get("/library"))
        self.assertEqual([s["title"] for s in view["sections"]], ["Ещё", "Основы"])
        self.assertEqual([m["slug"] for m in view["sections"][1]["sections"][0]["materials"]], ["lists", "cycles"])
        self.assertEqual([m["slug"] for m in view["unsorted"]], ["pct"])

        self.refused(self.post(f"/library/sections/{top}/move", json={"parent_id": other}), "section_too_deep")
        self.refused(self.post(f"/library/sections/{top}/move", json={"parent_id": top}), "section_invalid")
        self.refused(self.post(f"/library/sections/{top}/move", json={}), "invalid_request")
        self.assertTrue(self.ok(self.post(f"/library/sections/{sub}/move", json={"parent_id": None}))["changed"])
        self.assertFalse(self.ok(self.post(f"/library/sections/{sub}/move", json={"parent_id": None}))["changed"])
        self.ok(self.post(f"/library/sections/{sub}/move", json={"parent_id": top}))
        self.assertTrue(self.ok(self.patch(f"/library/sections/{top}", json={"title": "Начало"}))["changed"])
        self.refused(self.patch(f"/library/sections/{top}", json={"title": " "}), "section_title_invalid")
        self.refused(self.post(f"/library/sections/{top}/delete", json={"confirm_id": top}), "section_not_empty")
        self.refused(self.post(f"/library/sections/{sub}/delete", json={"confirm_id": top}), "confirmation_mismatch")
        gone = self.ok(self.post(f"/library/sections/{sub}/delete", json={"confirm_id": sub}))
        self.assertEqual(gone["materials"], ["lists", "cycles"])
        self.refused(self.post(f"/library/sections/{sub}/delete", json={"confirm_id": sub}), "section_not_found")
        self.ok(self.post(f"/library/sections/{other}/materials", json={"material_id": pct}))
        self.assertTrue(self.ok(self.post(f"/library/sections/{other}/materials/{pct}/delete"))["changed"])
        self.assertFalse(self.ok(self.post(f"/library/sections/{other}/materials/{pct}/delete"))["changed"])
        self.refused(self.get(f"/library/sections/{2 ** 31}/materials/order"), "method_not_allowed", 405)

        for q, slugs in (("циклы", ["cycles"]), ("ЦИКЛ", ["cycles"]), ("%", ["pct"]), ("_", ["pct"]),
                         ("\\", [])):
            with self.subTest(q=q):
                found = self.ok(self.get("/library", params={"q": q}))
                self.assertEqual([m["slug"] for m in found["materials"]], slugs)
        self.refused(self.get("/library", params={"q": "я" * 201}), "invalid_request")


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminMaterialLimitTests(AdminMaterialTests):
    """JUDGE_ATTACHMENT_MAX_MB and JUDGE_ITEM_ATTACHMENTS_MAX of the env hold for files of a version too."""
    ENV = {"JUDGE_ATTACHMENT_MAX_MB": "1", "JUDGE_ITEM_ATTACHMENTS_MAX": "2"}

    def test_the_limits_of_the_env(self):
        mid = self.material()["id"]
        mib = 1024 * 1024
        before = self.counts()
        r = self.upload(mid, "big.txt", b"x" * (mib + 1))
        self.assertEqual((r.status_code, r.json()["detail"]), (413, "too_large"))
        self.assertEqual((self.counts(), self.stored()[1]), (before, []))
        body = Body(40)  # without Content-Length: cut right after the limit
        got, out = self.call("POST", f"/api/admin/materials/{mid}/attachments", body, query=b"name=big.txt")
        self.assertEqual((got, body.taken), (413, mib // CHUNK + 1), out)
        self.assertEqual(self.stored()[1], [])
        self.ok(self.upload(mid, "exact.txt", b"x" * mib), 201)
        self.ok(self.upload(mid, "less.txt", b"x" * (mib - 1)), 201)
        self.refused(self.upload(mid, "third.txt", b"x"), "too_many_attachments")
        self.refused(self.post(f"/materials/{mid}/links", json={"url": "https://a.ru"}), "too_many_attachments")

    # the inherited scenarios run with the default limits only
    test_materials_versions_slug_and_links = None
    test_untrusted_text_byte_for_byte = None
    test_attachments_make_versions_and_files_are_served = None
    test_refusals_and_their_codes = None
    test_every_cheap_refusal_comes_before_the_body = None
    test_the_material_is_locked_only_after_the_body = None
    test_a_change_during_the_upload_is_decided_under_the_lock = None
    test_sections_order_and_search = None
