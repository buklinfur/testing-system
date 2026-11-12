"""/api/admin comment threads (docs/ADMIN_API.md §5.9, CLASSROOM.md §9, D-077) on a real PostgreSQL: every route, a
private thread found only by the pair (item, student) of the path, up_to of another thread refused with the mark
untouched, read marks of two admins independent, untrusted text byte for byte, times with Z, both cursors, soft
deletion, the inbox, and a lone surrogate or NUL in a body refused with 422 (never 500).

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import tempfile
import unittest

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

HOSTILE = "<script>alert('x')</script> \x1b[2J \u202egpj.exe\u00a0NBSP"  # ESC, U+202E, NBSP
RAW = "<script>alert('x')</script> \\u001b[2J \u202egpj.exe\u00a0NBSP".encode()  # in JSON: only ESC escaped

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app import groups as g
    from app.admin_api.feed import STATUS
    from app.classroom import comments
    from app.models import AuditLog, Comment, Contest, FeedItem, ThreadRead, User
    from app.settings import AuthSettings, Settings


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminCommentTests(ApiCase):
    def settings(self):
        self.blob_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.blob_tmp.cleanup)
        return Settings(public_origin=ORIGIN, blob_dir=self.blob_tmp.name, timezone="Asia/Novosibirsk",
                        auth=AuthSettings(secret_key=Fernet.generate_key().decode()))

    def setUp(self):
        super().setUp()
        self.boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))
        self.ivanov, _ = self.make_student("Иванов", "Пётр")
        self.petrova, _ = self.make_student("Петрова", "Анна")
        self.sidorov, _ = self.make_student("Сидоров", "Иван")
        self.group = g.create(self.s, self.cli, "7Б", self.now)
        g.add_members(self.s, self.cli, self.group, [self.ivanov, self.petrova], self.now)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def post(self, path, b=None, h=None, **kw):
        return (b or self.b).post(f"/api/admin{path}", headers=h or self.h, **kw)

    def put(self, path, **kw):
        return self.b.put(f"/api/admin{path}", headers=self.h, **kw)

    def get(self, path, b=None, **kw):
        return (b or self.b).get(f"/api/admin{path}", **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        return r.json()

    def refused(self, r, code, status=None):
        self.assertEqual((r.status_code, r.json()["detail"]), (status or STATUS.get(code, 422), code), r.text)
        return r.json()

    def published(self, **kw):
        body = {"kind": "assignment", "title": "Блок-схема", "body_md": "Нарисуй", "grading": "none",
                "due_at": "2026-10-29 23:59", "group_ids": [self.group.id], **kw}
        made = self.ok(self.post("/items", json=body), 201)
        return self.ok(self.post(f"/items/{made['id']}/publish"))["item"]

    def student_says(self, user, item_id, text, private=False):
        """A student's comment through the service (the student endpoints are 4b.2)."""
        self.s.expire_all()
        post = comments.post_private if private else comments.post_shared
        c = post(self.s, self.s.get(User, user.id), self.s.get(FeedItem, item_id), self.now, text, limit=0)
        self.s.commit()
        self.tick(seconds=1)
        return c.id

    def second_admin(self):
        _boss2, password, secret = self.make_admin("boss2")
        b = self.browser()
        r = self.login(b, "boss2", password)
        r = b.post("/api/auth/totp", json={"code": self.code(secret)}, headers=self.csrf(r))
        self.assertEqual((r.status_code, r.json()["state"]), (200, "active"), r.text)
        return b, self.csrf(r)

    # ---------------------------------------------------------------- the shared thread
    def test_shared_thread_untrusted_text_times_and_reading_sets_no_mark(self):
        d = self.published()
        path = f"/items/{d['id']}/comments"
        empty = self.ok(self.get(path))
        self.assertEqual((empty["thread_id"], empty["comments"], empty["unread"], empty["kind"], empty["can_post"],
                          empty["comments_enabled"]), (None, [], 0, "shared", True, False))
        out = self.ok(self.put(f"/items/{d['id']}", json={"comments_enabled": True}))
        self.assertEqual((out["changed"], out["item"]["comments_enabled"]), (["comments_enabled"], True))
        self.refused(self.put(f"/items/{d['id']}", json={"comments_enabled": None}), "item_invalid")
        self.refused(self.put(f"/items/{d['id']}", json={"comments_enabled": "yes"}), "invalid_request")

        mine = self.ok(self.post(path, json={"body": HOSTILE}), 201)
        self.assertEqual((mine["body"], mine["kind"], mine["item"], mine["student"], mine["author"]["login"],
                          mine["deleted"], mine["at"][-1]), (HOSTILE, "shared", d["id"], None, "boss", False, "Z"))
        theirs = self.student_says(self.ivanov, d["id"], HOSTILE + " вопрос")
        r = self.get(path)
        self.assertIn(RAW, r.content)  # byte for byte: <, >, ', U+202E and NBSP neither escaped nor stripped
        view = self.ok(r)
        self.assertEqual([c["id"] for c in view["comments"]], [mine["id"], theirs])
        self.assertEqual((view["thread_id"], view["unread"], view["comments"][1]["author"]["login"]),
                         (mine["thread_id"], 1, "ivanov.p"))
        self.assertTrue(all(c["at"].endswith("Z") for c in view["comments"]))
        self.assertEqual(self.ok(self.get(path))["unread"], 1)  # GET sets no mark
        self.assertEqual(self.ok(self.post(f"{path}/read")), {"unread": 0})
        self.assertEqual(self.ok(self.get(path))["unread"], 0)
        audit = self.s.execute(select(AuditLog).where(AuditLog.action == "comment.post")).scalar_one()
        self.assertEqual((audit.actor_user_id, audit.details["item"]), (self.boss.id, d["id"]))
        self.assertNotIn("script", str(audit.details))  # the text never goes to the audit log

    def test_a_contest_item_and_a_draft(self):
        slug = self.s.get(Contest, self.seed["cp"].contest_id).slug
        cid = self.ok(self.post("/items", json={"kind": "contest", "title": "К", "contest": slug,
                                                "group_ids": [self.group.id]}), 201)["id"]
        self.refused(self.post(f"/items/{cid}/comments", json={"body": "до публикации"}), "not_published")
        self.refused(self.put(f"/items/{cid}", json={"comments_enabled": True}), "item_invalid")
        self.ok(self.post(f"/items/{cid}/publish"))
        out = self.ok(self.post(f"/items/{cid}/comments", json={"body": "Объявление"}), 201)  # teachers write
        self.assertEqual(out["kind"], "shared")
        self.refused(self.get(f"/items/{cid}/students/{self.ivanov.id}/comments"), "thread_not_found")
        self.refused(self.post(f"/items/{cid}/students/{self.ivanov.id}/comments", json={"body": "x"}),
                     "not_an_assignment")

    # ---------------------------------------------------------------- private threads
    def test_a_private_thread_is_the_pair_of_the_path(self):
        d1, d2 = self.published(), self.published(group_ids=[], user_ids=[self.sidorov.id])
        q = self.student_says(self.ivanov, d1["id"], "можно сдать позже?", private=True)
        mine = self.ok(self.post(f"/items/{d1['id']}/students/{self.ivanov.id}/comments",
                                 json={"body": "Да"}), 201)
        self.assertEqual((mine["kind"], mine["student"]["login"], mine["item"]),
                         ("private", "ivanov.p", d1["id"]))
        view = self.ok(self.get(f"/items/{d1['id']}/students/{self.ivanov.id}/comments"))
        self.assertEqual(([c["id"] for c in view["comments"]], view["student"]["id"]),
                         ([q, mine["id"]], self.ivanov.id))
        # another addressee of the same item: their own (empty) thread, never Ivanov's
        other = self.ok(self.get(f"/items/{d1['id']}/students/{self.petrova.id}/comments"))
        self.assertEqual((other["thread_id"], other["comments"], other["can_post"]), (None, [], True))
        # Ivanov's thread asked through another item, a student not addressed, an admin, nobody: 404
        self.refused(self.get(f"/items/{d2['id']}/students/{self.ivanov.id}/comments"), "thread_not_found")
        self.refused(self.get(f"/items/{d1['id']}/students/{self.sidorov.id}/comments"), "thread_not_found")
        self.refused(self.get(f"/items/{d1['id']}/students/{self.boss.id}/comments"), "thread_not_found")
        self.refused(self.get(f"/items/{d1['id']}/students/999999/comments"), "user_not_found")
        self.refused(self.post(f"/items/{d1['id']}/students/{self.sidorov.id}/comments", json={"body": "x"}),
                     "not_addressed")
        self.refused(self.post(f"/items/{d1['id']}/students/{self.boss.id}/comments", json={"body": "x"}),
                     "not_a_student")
        # the first message to a student makes their thread
        first = self.ok(self.post(f"/items/{d2['id']}/students/{self.sidorov.id}/comments",
                                  json={"body": "Привет"}), 201)
        self.assertNotEqual(first["thread_id"], mine["thread_id"])
        # up_to of another thread: 404 and the mark stays where it was
        read = f"/items/{d1['id']}/students/{self.ivanov.id}/comments/read"
        self.assertEqual(self.ok(self.post(read, json={"up_to": q})), {"unread": 0})
        self.refused(self.post(read, json={"up_to": first["id"]}), "comment_not_found")
        self.refused(self.post(f"/items/{d1['id']}/comments/read", json={"up_to": q}), "comment_not_found")
        mark = self.s.execute(select(ThreadRead.last_read_comment_id)
                              .where(ThreadRead.user_id == self.boss.id)).scalars().all()
        self.assertEqual(mark, [q])
        later = self.student_says(self.ivanov, d1["id"], "спасибо", private=True)
        self.assertEqual(self.ok(self.get(f"/items/{d1['id']}/students/{self.ivanov.id}/comments"))["unread"], 1)
        self.assertEqual(self.ok(self.post(read, json={"up_to": q})), {"unread": 1})  # the mark only grows
        self.assertEqual(self.ok(self.post(read)), {"unread": 0})
        self.assertEqual(self.s.execute(select(ThreadRead.last_read_comment_id)
                                        .where(ThreadRead.user_id == self.boss.id)).scalar_one(), later)

    def test_read_marks_belong_to_each_admin(self):
        d = self.published(comments_enabled=True)
        path = f"/items/{d['id']}/comments"
        first = self.student_says(self.ivanov, d["id"], "раз")
        self.student_says(self.petrova, d["id"], "два")
        b2, h2 = self.second_admin()
        self.assertEqual((self.ok(self.get(path))["unread"], self.ok(self.get(path, b=b2))["unread"]), (2, 2))
        self.assertEqual(self.ok(self.post(f"{path}/read")), {"unread": 0})
        self.assertEqual(self.ok(self.get(path, b=b2))["unread"], 2)
        self.assertEqual(self.ok(self.post(f"{path}/read", b=b2, h=h2, json={"up_to": first})), {"unread": 1})
        self.assertEqual(self.ok(self.get(path))["unread"], 0)
        unread = {"unread": "true"}
        self.assertEqual([t["unread"] for t in self.ok(self.get("/comments/threads", params=unread))["items"]], [])
        self.assertEqual([t["unread"] for t in self.ok(self.get("/comments/threads", b=b2, params=unread))["items"]],
                         [1])
        # an admin's own comment is never unread for them, but is for the other one
        self.ok(self.post(path, b=b2, h=h2, json={"body": "ответ"}), 201)
        self.assertEqual((self.ok(self.get(path))["unread"], self.ok(self.get(path, b=b2))["unread"]), (1, 1))

    # ---------------------------------------------------------------- cursors
    def test_the_thread_pages_after_and_the_inbox_pages_newest_first(self):
        d = self.published(comments_enabled=True)
        ids = [self.student_says(self.ivanov, d["id"], f"№{i}") for i in range(3)]
        path = f"/items/{d['id']}/comments"
        page = self.ok(self.get(path, params={"limit": 2}))
        self.assertEqual(([c["id"] for c in page["comments"]], page["next_after"]), (ids[:2], ids[1]))
        page = self.ok(self.get(path, params={"limit": 2, "after": page["next_after"]}))
        self.assertEqual(([c["id"] for c in page["comments"]], page["next_after"]), (ids[2:], None))
        self.refused(self.get(path, params={"after": -1}), "invalid_request")
        self.refused(self.get(path, params={"limit": 0}), "invalid_request")
        # three threads; the inbox is newest first with cursor / next_cursor
        p = self.student_says(self.petrova, d["id"], "лично", private=True)
        s = self.student_says(self.ivanov, d["id"], "лично", private=True)
        inbox = self.ok(self.get("/comments/threads", params={"limit": 2}))
        self.assertEqual([t["last_id"] for t in inbox["items"]], [s, p])
        self.assertEqual(inbox["next_cursor"], p)
        rest = self.ok(self.get("/comments/threads", params={"limit": 2, "cursor": inbox["next_cursor"]}))
        self.assertEqual(([t["kind"] for t in rest["items"]], rest["next_cursor"]), (["shared"], None))
        self.assertTrue(all(t["last"]["at"].endswith("Z") for t in inbox["items"] + rest["items"]))

    # ---------------------------------------------------------------- deletion and the inbox
    def test_deletion_and_the_inbox(self):
        d = self.published(comments_enabled=True)
        q = self.student_says(self.ivanov, d["id"], "вопрос <b>!</b>", private=True)
        threads = "/comments/threads"
        awaiting = self.ok(self.get(threads, params={"awaiting": "true"}))["items"]
        self.assertEqual([(t["kind"], t["student"]["login"], t["awaiting_reply"]) for t in awaiting],
                         [("private", "ivanov.p", True)])
        reply = self.ok(self.post(f"/items/{d['id']}/students/{self.ivanov.id}/comments", json={"body": "ответ"}),
                        201)
        self.assertEqual(self.ok(self.get(threads, params={"awaiting": "true"}))["items"], [])
        self.assertEqual(len(self.ok(self.get(threads, params={"awaiting": "false"}))["items"]), 1)
        # deleting the teacher's reply brings the thread back to the inbox
        out = self.ok(self.post(f"/comments/{reply['id']}/delete", json={"reason": "  не  туда  "}))
        self.assertEqual((out["deleted"], out["delete_reason"], out["deleted_by"]["login"], out["body"],
                          out["kind"], out["item"]), (True, "не туда", "boss", "ответ", "private", d["id"]))
        self.assertTrue(out["deleted_at"].endswith("Z"))
        self.assertEqual(len(self.ok(self.get(threads, params={"awaiting": "true"}))["items"]), 1)
        self.refused(self.post(f"/comments/{reply['id']}/delete"), "already_deleted")
        self.refused(self.post("/comments/999999/delete"), "comment_not_found")
        self.refused(self.post(f"/comments/{q}/delete", json={"reason": "я" * 201}), "reason_too_long")
        out = self.ok(self.post(f"/comments/{q}/delete", json={}))
        self.assertEqual((out["deleted"], out["delete_reason"], out["author"]["login"]), (True, None, "ivanov.p"))
        # teachers still see both, with their text
        view = self.ok(self.get(f"/items/{d['id']}/students/{self.ivanov.id}/comments"))
        self.assertEqual([(c["body"], c["deleted"]) for c in view["comments"]],
                         [("вопрос <b>!</b>", True), ("ответ", True)])
        row = self.s.execute(select(AuditLog).where(AuditLog.action == "comment.delete",
                                                    AuditLog.object_id == str(reply["id"]))).scalar_one()
        self.assertEqual((row.actor_user_id, row.details["reason"]), (self.boss.id, "не туда"))
        # filters: the item, the group (private threads of its current members), unknown ones
        self.assertEqual(len(self.ok(self.get(threads, params={"item_id": d["id"]}))["items"]), 1)
        self.assertEqual(len(self.ok(self.get(threads, params={"group_id": self.group.id}))["items"]), 1)
        other = g.create(self.s, self.cli, "8А", self.now)
        self.s.commit()
        self.assertEqual(self.ok(self.get(threads, params={"group_id": other.id}))["items"], [])
        self.refused(self.get(threads, params={"item_id": 999999}), "item_not_found")
        self.refused(self.get(threads, params={"group_id": 999999}), "group_not_found")

    # ---------------------------------------------------------------- bodies the database cannot hold
    def test_a_lone_surrogate_or_nul_is_422_never_500(self):
        d = self.published(comments_enabled=True)
        raw = {**self.h, "Content-Type": "application/json"}
        for path in (f"/items/{d['id']}/comments", f"/items/{d['id']}/students/{self.ivanov.id}/comments"):
            with self.subTest(path=path):
                r = self.post(path, h=raw, content=b'{"body": "a\\ud800b"}')
                self.refused(r, "invalid_request")  # Pydantic: string_unicode (ADMIN_API §5.9)
                self.assertIn("body", r.json()["problems"][0])
                self.refused(self.post(path, h=raw, content=b'{"body": "a\\u0000b"}'), "comment_invalid")
                self.refused(self.post(path, json={"body": "​  ﻿"}), "comment_required")
                self.refused(self.post(path, json={"body": "я" * 4001}), "comment_too_long")
                self.refused(self.post(path, json={}), "invalid_request")
        r = self.post("/comments/1/delete", h=raw, content=b'{"reason": "\\udfff"}')
        self.refused(r, "invalid_request")
        self.assertEqual(self.s.execute(select(Comment.id)).all(), [])
