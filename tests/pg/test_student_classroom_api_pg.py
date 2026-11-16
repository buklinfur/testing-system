"""The HTTP API of a student's class feed (4b.2 stage 5, D-101, docs/STUDENT_API.md §6) on a real PostgreSQL: the
feed, the page of an item, a teacher's file, the hand-in, the files of the own work, the draft uploaded one file at a
time, from-last, the list of own works. Every code of CLASSROOM_STATUS reached by a request; identifiers of others
404 on every route; nothing of another student, no sha256, author or unreleased grade in the JSON; every cheap
refusal of an upload before a byte of the body is read and with nothing left in the store's tmp; no lock and no
transaction while the body arrives; 413 file_too_large by Content-Length and without it; a HEIC photo never inline.
Stage 6 (D-103): comments, materials and the library - their codes reached too (429 with Retry-After), no comment of
another thread reached by up_to or delete, a material only with a basis, a file only of its latest version, nothing
archived in the library.

Skipped unless JUDGE_TEST_DATABASE_URL is set."""
import dataclasses
import json
import tempfile
import unittest
from datetime import timedelta

from tests.pg.apibase import HAVE_API, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select, text

    from app import groups as g
    from app import locks
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.classroom import attachments as svc
    from app.classroom import comments, items, works
    from app.materials import library
    from app.materials import service as ms
    from app.models import AssignmentWork, Contest, Material, WorkDraftFile
    from app.settings import AuthSettings, FileSettings, Settings
    from app.student_api.deps import CLASSROOM_STATUS, UNREACHABLE
    from tests.pg.apibase import ORIGIN

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"%" * 64
FILE_MAX, TOTAL_MAX, FILES_MAX, TEXT_MAX = 4096, 6000, 3, 100
UPLOAD = {"Content-Type": "application/octet-stream"}
# keys that must never reach a student: hashes, who of the teachers did something, the review's own fields
FORBIDDEN_KEYS = {"sha256", "blob_sha256", "created_by", "author_id", "reviewer_id", "login", "ip", "user_id",
                  "password_hash", "csrf_token"}


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


def never():
    raise AssertionError("the body was read")
    yield b""  # pragma: no cover


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class StudentClassroomApiTests(ApiCase):
    def settings(self):
        return Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()),
                        blob_dir=self.blob_dir, assignment_text_max_bytes=TEXT_MAX,
                        files=FileSettings(submission_files_max=FILES_MAX, submission_file_max_bytes=FILE_MAX,
                                           submission_total_max_bytes=TOTAL_MAX, student_quota_bytes=50_000))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blob_dir = tmp.name
        super().setUp()
        self.blobs = BlobStore(self.blob_dir)
        self.reached = set()
        self.admin = self.seed["admin"]
        self.actor = audit.Actor(user_id=self.admin.id)
        self.a, self.ab = self.student_browser("Алексеев", "Антон")
        self.b, self.bb = self.student_browser("Борисов", "Борис")
        self.c, self.cb = self.student_browser("Васильев", "Виктор")  # another class
        self.group = g.create(self.s, self.actor, "7Б", self.now)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], self.now)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def student_browser(self, surname, first):
        user, password = self.make_student(surname, first)
        b = self.browser()
        r = self.login(b, user.login, password)
        self.assertEqual(r.status_code, 200, r.text)
        b.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return user, b

    def item(self, answer_kind="text_or_files", group=None, publish=True, **fields):
        body_md = fields.pop("body_md", "Сделай")
        base = dict(grading="pass_fail", due_at=self.now + timedelta(days=30), answer_kind=answer_kind)
        base.update(fields)
        item = items.create_item(self.s, self.actor, self.now, kind="assignment", title="Проект", body_md=body_md,
                                 **base)
        items.set_audience(self.s, self.actor, item, self.now, groups=[group or self.group])
        if publish:
            items.publish(self.s, self.actor, item, self.now)
        self.s.commit()
        return item

    def contest_item(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        item = items.create_item(self.s, self.actor, self.now, kind="contest", title="Контест", contest=contest)
        items.set_audience(self.s, self.actor, item, self.now, groups=[self.group])
        items.publish(self.s, self.actor, item, self.now)
        self.s.commit()
        return item

    def expect(self, r, status, code=None):
        self.assertEqual(r.status_code, status, r.text)
        if code is not None:
            self.assertEqual(r.json()["detail"], code, r.text)
            self.reached.add(code)
        return r

    def upload(self, b, item, name, data, **kw):
        return b.post(f"/api/feed/{item.id}/draft/files", params={"name": name}, content=data,
                      headers={**UPLOAD, **kw.pop("headers", {})}, **kw)

    def put(self, b, item, name, data) -> dict:
        return self.expect(self.upload(b, item, name, data), 201).json()["file"]

    def hand_in(self, b, item, **body):
        return b.post(f"/api/feed/{item.id}/work", json=body)

    def tmp_files(self) -> list:
        root = self.blobs.root / "tmp"
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def drafts(self) -> int:
        self.s.rollback()
        return self.s.execute(select(func.count()).select_from(WorkDraftFile)).scalar_one()

    def files_setting(self, **kw):
        settings = self.app.state.settings
        self.app.state.settings = dataclasses.replace(settings, files=dataclasses.replace(settings.files, **kw))

    # ---------------------------------------------------------------- one path through the API
    def test_a_student_reads_uploads_hands_in_and_sees_it(self):
        task = self.item()
        feed = self.expect(self.ab.get("/api/feed"), 200).json()
        self.assertEqual([row["id"] for row in feed["items"]], [task.id])
        self.assertEqual(feed["items"][0]["assignment"]["status"], None)
        self.assertTrue(feed["server_time"].endswith("Z"))
        page = self.expect(self.ab.get(f"/api/feed/{task.id}"), 200).json()
        self.assertEqual((page["body_md"], page["draft"], page["can_submit"]["allowed"]), ("Сделай", [], True))

        photo = self.put(self.ab, task, "фото.png", PNG)
        r = self.expect(self.upload(self.ab, task, "IMG_0001.HEIC", HEIC), 201).json()
        self.assertEqual([f["name"] for f in r["draft"]], ["фото.png", "IMG_0001.HEIC"])
        self.assertEqual(len({f["expires_at"] for f in r["draft"]}), 1)  # the draft lives as a whole
        heic = r["file"]
        extra = self.put(self.ab, task, "лишнее.pdf", PDF)
        self.assertEqual(self.tmp_files(), [])

        shown = self.expect(self.ab.get(f"/api/feed/{task.id}/draft/files/{photo['id']}"), 200)
        self.assertEqual((shown.content, shown.headers["content-type"]), (PNG, "image/png"))
        self.assertTrue(shown.headers["content-disposition"].startswith("inline"))
        self.assertEqual(shown.headers["x-content-type-options"], "nosniff")
        heif = self.expect(self.ab.get(f"/api/feed/{task.id}/draft/files/{heic['id']}"), 200)
        self.assertEqual(heif.headers["content-type"], "application/octet-stream")  # never inline
        self.assertTrue(heif.headers["content-disposition"].startswith("attachment"))

        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/files/{extra['id']}/delete"), 204)
        self.expect(self.ab.get(f"/api/feed/{task.id}/draft/files/{extra['id']}"), 404, "draft_file_not_found")
        made = self.expect(self.hand_in(self.ab, task, text="Готово", files=[heic["id"], photo["id"]],
                                        links=[{"url": "https://scratch.mit.edu/projects/1", "title": "Проект"}]),
                           201).json()
        self.assertEqual((made["submission"]["late"], made["work"]["status"]), (False, "submitted"))
        self.assertEqual(self.expect(self.ab.get(f"/api/feed/{task.id}/draft"), 200).json(), {"draft": []})

        page = self.expect(self.ab.get(f"/api/feed/{task.id}"), 200).json()
        (sub,) = page["hand_ins"]
        self.assertEqual((sub["text"], [f["name"] for f in sub["files"]], sub["links"][0]["title"]),
                         ("Готово", ["IMG_0001.HEIC", "фото.png"], "Проект"))
        got = self.expect(self.ab.get(f"/api/feed/{task.id}/work/files/{sub['files'][1]['id']}"), 200)
        self.assertEqual(got.content, PNG)
        mine = self.expect(self.ab.get("/api/works"), 200).json()["items"]
        self.assertEqual([(w["item"]["id"], w["status"]) for w in mine], [(task.id, "submitted")])

    def test_from_last_after_a_return_and_the_draft_moment(self):
        task = self.item(due_at=None)
        first = self.put(self.ab, task, "a.png", PNG)
        expires = self.expect(self.ab.get(f"/api/feed/{task.id}/draft"), 200).json()["draft"][0]["expires_at"]
        self.tick(days=1)
        # nothing to copy (no hand-in yet): no change of the draft, its expires_at stays
        r = self.expect(self.ab.post(f"/api/feed/{task.id}/draft/from-last"), 200).json()
        self.assertEqual([(f["id"], f["expires_at"]) for f in r["draft"]], [(first["id"], expires)])
        self.expect(self.hand_in(self.ab, task, files=[first["id"]]), 201)
        works.return_work(self.s, self.actor, task, self.a, self.now, "Переделай")
        self.s.commit()
        self.tick(days=1)
        r = self.expect(self.ab.post(f"/api/feed/{task.id}/draft/from-last"), 200).json()
        self.assertEqual([f["name"] for f in r["draft"]], ["a.png"])
        self.assertGreater(r["draft"][0]["expires_at"], expires)  # a copy is a change
        made = self.expect(self.hand_in(self.ab, task, files=[r["draft"][0]["id"]]), 201).json()
        self.assertEqual(made["work"]["status"], "submitted")

    # ---------------------------------------------------------------- every code
    def test_every_code_is_reached_by_a_request(self):
        admin_user, boss_password, secret = self.make_admin()
        admin = self.browser()
        self.admin_login(admin, boss_password, secret)
        self.expect(admin.get("/api/feed"), 403, "student_required")

        task = self.item()
        self.expect(self.ab.get("/api/feed/999999"), 404, "item_not_found")
        self.expect(self.ab.get(f"/api/feed/{task.id}/attachments/999"), 404, "attachment_not_found")
        self.expect(self.ab.get(f"/api/feed/{task.id}/work/files/999"), 404, "file_not_found")
        self.expect(self.ab.get(f"/api/feed/{task.id}/draft/files/999"), 404, "draft_file_not_found")
        self.expect(self.ab.get(f"/api/feed/{self.contest_item().id}/draft"), 409, "not_an_assignment")
        self.expect(self.hand_in(self.ab, task), 422, "answer_required")
        self.expect(self.hand_in(self.ab, task, text="x" * (TEXT_MAX + 1)), 422, "text_too_long")
        self.expect(self.hand_in(self.ab, task, text="x", extra=1), 422, "invalid_request")
        self.expect(self.hand_in(self.ab, task, links=[{"url": "javascript:alert(1)"}]), 422, "link_invalid")
        self.expect(self.hand_in(self.ab, task, links=[{"url": "https://e.ru", "title": "т" * 300}]), 422,
                    "link_title_invalid")
        self.expect(self.hand_in(self.ab, task, files=[999]), 404, "draft_file_not_found")
        self.expect(self.upload(self.ab, task, "a.exe", b"MZ"), 422, "file_type_not_allowed")
        self.expect(self.upload(self.ab, task, "a.png", b"not a png"), 422, "file_content_mismatch")
        self.expect(self.upload(self.ab, task, "a.png", b""), 422, "file_empty")
        self.expect(self.upload(self.ab, task, "a.png", b"x" * (FILE_MAX + 1)), 413, "file_too_large")
        self.put(self.ab, task, "a.png", PNG)
        self.expect(self.upload(self.ab, task, "A.PNG", PNG), 422, "duplicate_name")
        self.put(self.ab, task, "b.pdf", PDF + b"x" * 3000)
        self.expect(self.upload(self.ab, task, "c.pdf", PDF + b"y" * 3000), 422, "submission_too_large")
        self.put(self.ab, task, "c.png", PNG + b"z")
        self.expect(self.upload(self.ab, task, "d.png", PNG), 422, "too_many_files")

        files_only = self.item(answer_kind="files")
        self.expect(self.hand_in(self.ab, files_only, text="x"), 422, "text_not_expected")
        self.expect(self.hand_in(self.ab, files_only), 422, "files_required")
        self.expect(self.upload(self.ab, self.item(answer_kind="text"), "a.png", PNG), 422, "files_not_expected")
        closed = self.item(due_at=self.now - timedelta(hours=1), after_due_submit="no")
        self.expect(self.upload(self.ab, closed, "a.png", PNG), 409, "deadline_passed")

        self.files_setting(student_quota_bytes=100)
        self.expect(self.upload(self.bb, task, "q.pdf", PDF + b"q" * 200), 409, "quota_exceeded")
        self.files_setting(student_quota_bytes=50_000)

        graded = self.item()
        self.expect(self.hand_in(self.bb, graded, text="ответ"), 201)
        works.grade_work(self.s, self.actor, graded, self.b, self.now, passed=True, release=True)
        self.s.commit()
        self.expect(self.upload(self.bb, graded, "a.png", PNG), 409, "already_graded")

        self.comment_codes()

        kept = self.item()
        self.expect(self.hand_in(self.bb, kept, text="ответ"), 201)
        g.remove_members(self.s, self.actor, self.group, [self.b], self.now)
        self.s.commit()
        self.expect(self.upload(self.bb, kept, "a.png", PNG), 403, "no_access")  # seen through the own work
        items.archive(self.s, self.actor, kept, self.now)
        self.s.commit()
        self.expect(self.upload(self.bb, kept, "a.png", PNG), 409, "item_archived")

        self.assertEqual(sorted(set(CLASSROOM_STATUS) - set(UNREACHABLE) - self.reached), [],
                         "codes of CLASSROOM_STATUS no request reached")
        self.assertEqual(self.reached & set(UNREACHABLE), set())

    def comment_codes(self):
        """The codes of comments, materials and the library (D-103), reached by student a of the class."""
        talk = self.item(comments_enabled=True)
        hidden = self.item(due_at=self.now - timedelta(hours=1), after_due_statement="hidden",
                           after_due_submit="late", comments_enabled=True)
        self.expect(self.ab.get(f"/api/feed/{hidden.id}/comments"), 404, "thread_not_found")
        self.expect(self.ab.post("/api/comments/999999/delete"), 404, "comment_not_found")
        self.expect(self.ab.get("/api/materials/999999"), 404, "material_not_found")
        teacher = comments.teacher_post(self.s, self.actor, talk, self.now, "объявление")
        self.s.commit()
        self.expect(self.ab.post(f"/api/comments/{teacher.id}/delete"), 403, "not_your_comment")
        self.expect(self.ab.post(f"/api/feed/{self.item().id}/comments", json={"body": "?"}), 409,
                    "comments_disabled")
        self.expect(self.ab.post(f"/api/feed/{self.contest_item().id}/comments", json={"body": "?"}), 409,
                    "comments_teacher_only")
        self.expect(self.ab.post(f"/api/feed/{talk.id}/comments", json={"body": " \u200b\u3000"}), 422,
                    "comment_required")
        self.expect(self.ab.post(f"/api/feed/{talk.id}/comments", json={"body": "x" * 4001}), 422,
                    "comment_too_long")
        self.expect(self.ab.post(f"/api/feed/{talk.id}/comments", json={"body": "a\x00b"}), 422, "comment_invalid")
        self.expect(self.ab.post(f"/api/feed/{talk.id}/my-thread", json={"body": "вопрос"}), 201)
        settings = self.app.state.settings
        self.app.state.settings = dataclasses.replace(settings, comment_rate_per_min=1)
        r = self.expect(self.ab.post(f"/api/feed/{talk.id}/comments", json={"body": "ещё"}), 429, "rate_limited")
        self.assertEqual((r.headers["Retry-After"], r.json()["retry_after_s"]), ("60", 60))
        self.app.state.settings = settings

    # ---------------------------------------------------------------- identifiers of others
    def test_nothing_of_another_student_on_any_route(self):
        task = self.item(grading="points", max_points=10, grades_release="manual")
        other_class = self.item(group=g.create(self.s, self.actor, "8А", self.now))
        hidden = self.item(due_at=self.now - timedelta(hours=1), after_due_statement="hidden",
                           after_due_submit="late")
        self.s.commit()
        a_file = self.put(self.ab, task, "мой.png", PNG)
        b_file = self.put(self.bb, task, "чужой.jpg", JPEG)
        other = self.item()
        b_other = self.put(self.bb, other, "другой.png", PNG)
        self.expect(self.hand_in(self.ab, task, files=[a_file["id"]], text="ответ"), 201)
        a_work_file = self.expect(self.ab.get(f"/api/feed/{task.id}"), 200).json()["hand_ins"][0]["files"][0]["id"]

        # another class's item, a draft item, nothing
        for path in (f"/api/feed/{other_class.id}", f"/api/feed/{other_class.id}/draft",
                     f"/api/feed/{task.id}"):
            with self.subTest(path=path):
                self.expect(self.cb.get(path), 404, "item_not_found")
        # another's draft file, by its own item and by another item; another's work file
        self.expect(self.ab.get(f"/api/feed/{task.id}/draft/files/{b_file['id']}"), 404, "draft_file_not_found")
        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/files/{b_file['id']}/delete"), 404,
                    "draft_file_not_found")
        self.expect(self.bb.get(f"/api/feed/{task.id}/work/files/{a_work_file}"), 404, "file_not_found")
        self.expect(self.bb.get(f"/api/feed/{other.id}/work/files/{a_work_file}"), 404, "file_not_found")
        self.expect(self.hand_in(self.ab, task, files=[b_file["id"]], text="x"), 404, "draft_file_not_found")
        # the own draft file of another item named in a hand-in
        self.expect(self.hand_in(self.bb, task, files=[b_other["id"]], text="x"), 404, "draft_file_not_found")
        # a teacher's file of an item whose statement is hidden now
        with self.blobs.incoming("лист.pdf") as up:
            up.write(PDF)
            row = svc.add_file(self.s, self.actor, hidden, up, self.now)
        self.s.commit()
        self.expect(self.ab.get(f"/api/feed/{hidden.id}/attachments/{row.id}"), 404, "attachment_not_found")
        self.expect(self.ab.get(f"/api/feed/{task.id}/attachments/{row.id}"), 404, "attachment_not_found")
        page = self.expect(self.ab.get(f"/api/feed/{hidden.id}"), 200).json()
        self.assertEqual((page["statement_hidden"], page["body_md"], page["attachments"]), (True, None, None))

        # what the JSON never carries
        works.grade_work(self.s, self.actor, task, self.a, self.now, score="7.25")  # not released
        self.s.commit()
        for b, path in [(self.ab, "/api/feed"), (self.ab, f"/api/feed/{task.id}"), (self.ab, "/api/works"),
                        (self.ab, f"/api/feed/{task.id}/draft"), (self.bb, f"/api/feed/{task.id}"),
                        (self.bb, f"/api/feed/{task.id}/draft")]:
            with self.subTest(path=path):
                body = self.expect(b.get(path), 200).json()
                self.assertEqual(set(keys(body)) & FORBIDDEN_KEYS, set())
                dump = json.dumps(body, ensure_ascii=False)
                self.assertNotIn("7.25", dump)  # the unreleased grade
                self.assertNotIn(self.admin.login, dump)
        self.assertNotIn("чужой.jpg", json.dumps(self.ab.get(f"/api/feed/{task.id}").json(), ensure_ascii=False))
        page = self.ab.get(f"/api/feed/{task.id}").json()
        self.assertEqual((page["assignment"]["grade"], page["assignment"]["status"]), (None, "submitted"))

    # ---------------------------------------------------------------- comments, materials, the library (D-103)
    def material(self, slug, title="Памятка", body="Текст", **kw):
        result = ms.create_material(self.s, self.actor, self.now, slug=slug, title=title, body_md=body, **kw)
        self.s.commit()
        return self.s.get(Material, result.version.material_id)

    def material_file(self, material, name, data):
        with self.blobs.incoming(name) as up:
            up.write(data)
            ms.add_file(self.s, self.actor, material, up, self.now)
        self.s.commit()
        return self.expect(self.ab.get(f"/api/materials/{material.id}"), 200).json()["attachments"]

    def test_comments_of_others_are_never_reached(self):
        task = self.item(comments_enabled=True)
        other = self.item(comments_enabled=True)
        b_private = self.expect(self.bb.post(f"/api/feed/{task.id}/my-thread", json={"body": "вопрос Б"}), 201).json()
        b_shared = self.expect(self.bb.post(f"/api/feed/{task.id}/comments", json={"body": "всем от Б"}), 201).json()
        a_private = self.expect(self.ab.post(f"/api/feed/{task.id}/my-thread", json={"body": "вопрос А"}), 201).json()
        a_other = self.expect(self.ab.post(f"/api/feed/{other.id}/comments", json={"body": "другое"}), 201).json()
        comments.teacher_post(self.s, self.actor, task, self.now, "ответ Б", student=self.b)
        self.s.commit()

        # the own private thread is the only one: b's question and the teacher's answer to b never show
        mine = self.expect(self.ab.get(f"/api/feed/{task.id}/my-thread"), 200).json()
        self.assertEqual([c["body"] for c in mine["comments"]], ["вопрос А"])
        self.assertNotEqual(mine["thread_id"], b_private["thread_id"])
        dump = json.dumps(mine, ensure_ascii=False)
        self.assertNotIn("Б", dump.replace("Борис", ""))  # no comment of b's thread
        self.assertEqual(set(keys(mine)) & FORBIDDEN_KEYS, set())
        # another's comment in delete: unseen (b's private) is 404, seen (b's shared) is 403; nothing changes
        self.expect(self.ab.post(f"/api/comments/{b_private['id']}/delete"), 404, "comment_not_found")
        self.expect(self.ab.post(f"/api/comments/{b_shared['id']}/delete"), 403, "not_your_comment")
        self.expect(self.cb.post(f"/api/comments/{a_private['id']}/delete"), 404, "comment_not_found")
        self.expect(self.cb.post(f"/api/comments/{b_shared['id']}/delete"), 404, "comment_not_found")
        shared = self.expect(self.ab.get(f"/api/feed/{task.id}/comments"), 200).json()
        self.assertEqual([c["body"] for c in shared["comments"]], ["всем от Б"])
        self.assertEqual([c["mine"] for c in shared["comments"]], [False])
        self.assertEqual(self.expect(self.bb.get(f"/api/feed/{task.id}/my-thread"), 200).json()["comments"][0]["body"],
                         "вопрос Б")
        # up_to taken from another thread: b's private, the shared one of this item, the shared one of another item
        for path, up_to in [(f"/api/feed/{task.id}/my-thread/read", b_private["id"]),
                            (f"/api/feed/{task.id}/my-thread/read", b_shared["id"]),
                            (f"/api/feed/{task.id}/comments/read", a_private["id"]),
                            (f"/api/feed/{task.id}/comments/read", a_other["id"])]:
            with self.subTest(path=path, up_to=up_to):
                self.expect(self.ab.post(path, json={"up_to": up_to}), 404, "comment_not_found")
        self.expect(self.ab.post(f"/api/feed/{task.id}/comments/read", json={"up_to": b_shared["id"]}), 200)
        # another class's student sees no thread of the item, and nothing of a non-assignment is a private thread
        for path in (f"/api/feed/{task.id}/comments", f"/api/feed/{task.id}/my-thread"):
            self.expect(self.cb.get(path), 404, "item_not_found")
        self.expect(self.ab.get(f"/api/feed/{self.contest_item().id}/my-thread"), 409, "not_an_assignment")
        # a deleted own comment is gone for the student, and a second delete is the same 404
        self.expect(self.ab.post(f"/api/comments/{a_private['id']}/delete"), 204)
        self.expect(self.ab.post(f"/api/comments/{a_private['id']}/delete"), 404, "comment_not_found")
        self.assertEqual(self.ab.get(f"/api/feed/{task.id}/my-thread").json()["comments"], [])

    def test_materials_need_a_basis_and_files_are_of_the_latest_version(self):
        shown = self.material("shown", "Циклы", body="См. [[памятка:secret]] и [[памятка:nowhere]]",
                              library="all_students")
        secret = self.material("secret", "Секрет")
        other = self.material("other", "Другая", library="all_students")
        old = self.material_file(shown, "v2.py", b"print(2)")[0]["id"]
        new = self.material_file(shown, "v3.py", b"print(3)")
        self.assertEqual([a["name"] for a in new], ["v2.py", "v3.py"])
        self.assertNotIn(old, [a["id"] for a in new])  # a new version copies its files under new ids
        other_file = self.material_file(other, "чужой.py", b"print(0)")[0]["id"]
        with self.blobs.incoming("секрет.py") as up:
            up.write(b"print(42)")
            ms.add_file(self.s, self.actor, secret, up, self.now)
        ms.add_link(self.s, self.actor, shown, "https://e.ru/x", self.now)
        self.s.commit()
        view = self.expect(self.ab.get(f"/api/materials/{shown.id}"), 200).json()
        link = next(a["id"] for a in view["attachments"] if a["kind"] == "link")
        latest = next(a["id"] for a in view["attachments"] if a.get("name") == "v3.py")
        self.assertNotIn(latest, [a["id"] for a in new])  # the link made one more version
        self.assertEqual(set(keys(view)) & FORBIDDEN_KEYS, set())

        # a material without basis is the same 404 as no material; a link from another material is no basis
        missing = self.expect(self.ab.get("/api/materials/999999"), 404, "material_not_found").json()
        self.assertEqual(self.expect(self.ab.get(f"/api/materials/{secret.id}"), 404).json()["detail"],
                         missing["detail"])
        self.assertEqual(view["links"], [{"slug": "secret", "available": False},
                                         {"slug": "nowhere", "available": False}])
        secret_file = self.s.execute(select(text("max(id) from attachments"))).scalar_one()
        self.expect(self.ab.get(f"/api/materials/{secret.id}/attachments/{secret_file}"), 404, "material_not_found")
        # a file of another version, of another material, a link: 404
        for att in (old, new[1]["id"], other_file, link, secret_file):
            with self.subTest(attachment=att):
                self.expect(self.ab.get(f"/api/materials/{shown.id}/attachments/{att}"), 404,
                            "attachment_not_found")
        r = self.expect(self.ab.get(f"/api/materials/{shown.id}/attachments/{latest}"), 200)
        self.assertEqual((r.content, r.headers["X-Content-Type-Options"]), (b"print(3)", "nosniff"))
        self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"))
        # a link in an item the student sees is a basis; the same link in another class's item is not
        self.item(body_md="Читай [[памятка:secret]]")
        self.expect(self.ab.get(f"/api/materials/{secret.id}"), 200)
        self.expect(self.cb.get(f"/api/materials/{secret.id}"), 404, "material_not_found")
        self.assertEqual(self.ab.get(f"/api/materials/{shown.id}").json()["links"][0],
                         {"slug": "secret", "available": True, "id": secret.id, "title": "Секрет"})

        # the library: no archived material, in the tree or in the search; then its page is 404 too
        section = library.create_section(self.s, self.actor, self.now, "Основы")
        library.place(self.s, self.actor, other, section, self.now)
        self.s.commit()
        tree = self.expect(self.ab.get("/api/library"), 200).json()
        self.assertEqual([m["slug"] for m in tree["sections"][0]["materials"]], ["other"])
        self.assertEqual([m["slug"] for m in tree["unsorted"]], ["shown"])
        self.assertEqual([m["slug"] for m in self.ab.get("/api/library", params={"q": "ДРУГ"}).json()["materials"]],
                         ["other"])
        ms.archive(self.s, self.actor, other, self.now)
        self.s.commit()
        tree = self.expect(self.ab.get("/api/library"), 200).json()
        self.assertEqual((tree["sections"], [m["slug"] for m in tree["unsorted"]]), ([], ["shown"]))
        self.assertEqual(self.ab.get("/api/library", params={"q": "друг"}).json()["materials"], [])
        self.expect(self.ab.get(f"/api/materials/{other.id}"), 404, "material_not_found")
        self.expect(self.ab.get(f"/api/materials/{other.id}/attachments/{other_file}"), 404, "material_not_found")
        self.assertEqual(self.ab.get("/api/library", params={"q": "%"}).json()["materials"], [])
        self.expect(self.ab.get("/api/library", params={"q": "x" * 201}), 422, "invalid_request")

    # ---------------------------------------------------------------- the body of an upload
    def test_every_cheap_refusal_comes_before_the_body_and_leaves_nothing(self):
        task = self.item()
        text_only = self.item(answer_kind="text")
        closed = self.item(due_at=self.now - timedelta(hours=1), after_due_submit="no")
        self.put(self.ab, task, "a.png", PNG)
        self.put(self.ab, task, "b.png", PNG + b"1")
        before = self.drafts()
        cases = [
            (task, "a.png", {"Content-Type": "text/plain"}, 415, "unsupported_media_type"),
            (task, None, UPLOAD, 422, "invalid_request"),
            (task, "x.exe", UPLOAD, 422, "file_type_not_allowed"),
            (task, "A.png", UPLOAD, 422, "duplicate_name"),
            (text_only, "c.png", UPLOAD, 422, "files_not_expected"),
            (closed, "c.png", UPLOAD, 409, "deadline_passed"),
        ]
        for item, name, headers, status, code in cases:
            with self.subTest(code=code):
                r = self.ab.post(f"/api/feed/{item.id}/draft/files", content=never(), headers=headers,
                                 params={} if name is None else {"name": name})
                self.expect(r, status, code)
                self.assertEqual(self.tmp_files(), [])
        for path in ("/api/feed/999999/draft/files", f"/api/feed/{self.contest_item().id}/draft/files"):
            r = self.ab.post(path, content=never(), headers=UPLOAD, params={"name": "c.png"})
            self.assertIn(r.json()["detail"], ("item_not_found", "not_an_assignment"))
        self.files_setting(submission_files_max=2)
        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/files", content=never(), headers=UPLOAD,
                                 params={"name": "c.png"}), 422, "too_many_files")
        self.files_setting(submission_files_max=FILES_MAX, student_quota_bytes=1000)
        # the quota by the declared length, before the body
        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/files", content=never(), params={"name": "c.png"},
                                 headers={**UPLOAD, "Content-Length": "3000"}), 409, "quota_exceeded")
        # over the limit by Content-Length: 413 before any service
        self.expect(self.ab.post("/api/feed/999999/draft/files", content=never(), params={"name": "c.png"},
                                 headers={**UPLOAD, "Content-Length": str(FILE_MAX + 1)}), 413, "file_too_large")
        self.assertEqual((self.drafts(), self.tmp_files()), (before, []))

    def test_413_without_content_length_leaves_nothing(self):
        task = self.item()
        before, stored = self.drafts(), sorted(p for p in self.blobs.root.rglob("*") if p.is_file())

        def chunks(n, size=1024):
            for _ in range(n):
                yield b"%PDF" + b"x" * (size - 4)
        r = self.ab.post(f"/api/feed/{task.id}/draft/files", content=chunks(5), params={"name": "big.pdf"},
                         headers=UPLOAD)
        self.assertNotIn("content-length", {k.lower() for k in r.request.headers})
        self.expect(r, 413, "file_too_large")
        self.assertEqual((self.drafts(), sorted(p for p in self.blobs.root.rglob("*") if p.is_file())),
                         (before, stored))

    def test_no_lock_and_no_transaction_while_the_body_arrives(self):
        task = self.item()
        self.expect(self.hand_in(self.ab, task, text="ответ"), 201)
        work_id = self.s.execute(select(AssignmentWork.id).where(AssignmentWork.user_id == self.a.id)).scalar_one()
        self.s.commit()
        seen = {}

        def body():
            with self.engine.connect() as conn:
                seen["work"] = conn.execute(text("SELECT id FROM assignment_works WHERE id = :i FOR UPDATE NOWAIT"),
                                            {"i": work_id}).scalar()
                seen["student"] = conn.execute(text("SELECT pg_try_advisory_xact_lock(CAST(:k AS integer), "
                                                    "CAST(:s AS integer))"),
                                               {"k": locks.STUDENT_FILES, "s": self.a.id}).scalar()
                seen["open"] = conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> "
                    "pg_backend_pid() AND state LIKE 'idle in transaction%'")).scalar()
                conn.rollback()
            yield PNG

        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/files", content=body(), params={"name": "a.png"},
                                 headers=UPLOAD), 201)
        self.assertEqual(seen, {"work": work_id, "student": True, "open": 0})

    # ---------------------------------------------------------------- D-102: an unreleased grade does not show
    def answers(self, b, item) -> dict:
        """Every answer a student can get about one item without changing anything: the reads, and refusals of a
        hand-in, an upload and from-last that come after the grade's check (server_time aside)."""
        def body(r):
            out = r.json()
            if isinstance(out, dict):
                out.pop("server_time", None)
            return r.status_code, out
        return {
            "feed": body(b.get("/api/feed")), "page": body(b.get(f"/api/feed/{item.id}")),
            "works": body(b.get("/api/works")), "draft": body(b.get(f"/api/feed/{item.id}/draft")),
            "hand-in": body(self.hand_in(b, item, text="  ")),  # answer_required, decided after already_graded
            "upload": body(self.upload(b, item, "a.png", PNG)),  # duplicate_name, after the item's check
            "upload-type": body(self.upload(b, item, "a.svg", PNG)),
            "from-last": body(b.post(f"/api/feed/{item.id}/draft/from-last")),
        }

    def test_an_unreleased_grade_changes_no_answer_and_does_not_close_the_hand_in(self):
        task = self.item(grades_release="manual")
        self.put(self.ab, task, "a.png", PNG)
        first = self.put(self.ab, task, "b.png", PNG + b"b")
        self.expect(self.hand_in(self.ab, task, files=[first["id"]]), 201)
        self.expect(self.ab.post(f"/api/feed/{task.id}/draft/from-last"), 200)  # from now on it copies nothing
        before = self.answers(self.ab, task)
        self.assertEqual(self.answers(self.ab, task), before, "asking changes nothing")
        self.assertEqual((before["hand-in"][1]["detail"], before["upload"][1]["detail"]),
                         ("answer_required", "duplicate_name"))
        self.assertEqual(before["page"][1]["can_submit"], {"allowed": True, "reason": "ok", "late": False})

        works.grade_work(self.s, self.actor, task, self.a, self.now, passed=False, comment="черновая оценка")
        self.s.commit()
        self.assertEqual(self.answers(self.ab, task), before, "the student can tell an unreleased grade exists")

        # the hand-in over it passes and takes it off the work: the teacher sees «пересдано после оценки»
        draft = self.ab.get(f"/api/feed/{task.id}/draft").json()["draft"]
        out = self.expect(self.hand_in(self.ab, task, files=[f["id"] for f in draft]), 201).json()
        self.assertEqual((out["work"]["status"], out["work"]["grade"], out["work"]["submissions"]),
                         ("submitted", None, 2))
        self.s.expire_all()
        card = works.work_card(self.s, self.blobs, task, self.a, self.now)
        self.assertTrue(card["resubmitted_after_grade"])
        self.assertEqual([e["superseded"] for e in card["events"] if e.get("action") == "graded"], [True])
        self.assertEqual(works.release_grades(self.s, self.actor, task, self.now, [self.a])[0]["result"],
                         "nothing_to_release")
        self.s.rollback()
        self.assertNotIn("черновая", self.ab.get(f"/api/feed/{task.id}").text)

        # a released grade closes the hand-in and the upload, as before
        works.grade_work(self.s, self.actor, task, self.a, self.now, passed=True, release=True)
        self.s.commit()
        page = self.ab.get(f"/api/feed/{task.id}").json()
        self.assertEqual((page["assignment"]["status"], page["can_submit"]["reason"]), ("graded", "already_graded"))
        self.expect(self.upload(self.ab, task, "c.png", PNG + b"c"), 409, "already_graded")
        self.expect(self.hand_in(self.ab, task, text="ещё"), 409, "already_graded")


if __name__ == "__main__":
    unittest.main()
