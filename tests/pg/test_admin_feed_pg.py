"""/api/admin class feed and works (docs/ADMIN_API.md §5.8, CLASSROOM.md) on a real PostgreSQL: every endpoint, the
codes of its refusals (each code of STATUS reached by a request), untrusted text byte for byte, times, the review
queue, the card of a work found only by its pair (item, student), score input, bulk release per student, the
feed items in the answer of a group's archiving, and the refusals of files and links (§5.10; the scenarios are in
test_admin_attachments_pg.py).

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

SCRIPT = "<script>alert('x')</script> <b>жирно</b>"

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select

    from app import groups as g
    from app.admin_api.feed import COMMENT_STUDENT_ONLY, DEFENSIVE, HAND_IN_ONLY, NO_ROUTE_YET, RESULT_ONLY, STATUS
    from app.blobstore import BlobStore
    from app.classroom import works
    from app.models import AssignmentReview, AuditLog, Contest, FeedItem, Material, User
    from app.settings import AuthSettings, Settings


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminFeedTests(ApiCase):
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
        self.reached = set()

    # ---------------------------------------------------------------- helpers
    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers=self.h, **kw)

    def put(self, path, **kw):
        return self.b.put(f"/api/admin{path}", headers=self.h, **kw)

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        if r.content:
            self.assertTrue(r.headers["content-type"].startswith("application/json"))
            return r.json()
        return None

    def refused(self, r, code):
        self.assertEqual((r.status_code, r.json()["detail"]), (STATUS.get(code, 422), code), r.text)
        self.reached.add(code)
        return r.json()

    def item(self, **kw):
        body = {"kind": "assignment", "title": "Блок-схема", "body_md": "Нарисуй", "grading": "points",
                "max_points": 10, "due_at": "2026-10-29 23:59", "group_ids": [self.group.id]}
        body.update(kw)
        return self.ok(self.post("/items", json=body), 201)

    def published(self, **kw):
        made = self.item(**kw)
        return self.ok(self.post(f"/items/{made['id']}/publish"))["item"]

    def hand_in(self, user, item_id, text="ответ", at=None):
        works.submit_work(self.s, self.s.get(User, user.id), self.s.get(FeedItem, item_id), at or self.now,
                          blobs=BlobStore(self.blob_tmp.name), text=text)
        self.s.commit()

    def audit_count(self):
        self.s.expire_all()
        return self.s.execute(select(func.count()).select_from(AuditLog)).scalar()

    # ---------------------------------------------------------------- items
    def test_create_read_edit_times_and_untrusted_text(self):
        d = self.item(title=SCRIPT, body_md=SCRIPT + "\n![x](javascript:alert(1))", user_ids=[self.sidorov.id],
                      available_from="2026-10-28 08:00", max_points="5.5")
        self.assertEqual((d["title"], d["body_md"]), (SCRIPT, SCRIPT + "\n![x](javascript:alert(1))"))
        self.assertEqual((d["state"], d["available_from"], d["due_at"], d["max_points"]),
                         ("draft", "2026-10-28T01:00:00Z", "2026-10-29T16:59:00Z", "5.50"))
        self.assertEqual(([x["name"] for x in d["audience"]["groups"]], [x["id"] for x in d["audience"]["users"]]),
                         (["7Б"], [self.sidorov.id]))
        r = self.get(f"/items/{d['id']}")
        self.assertIn(SCRIPT.encode(), r.content)  # byte for byte, not escaped
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        # times with an offset stay what they say
        d2 = self.item(due_at="2026-10-29T23:59:00+03:00")
        self.assertEqual(d2["due_at"], "2026-10-29T20:59:00Z")
        # PUT: only the keys sent change, null clears
        out = self.ok(self.put(f"/items/{d['id']}", json={"due_at": None, "title": "Новое"}))
        self.assertEqual((sorted(out["changed"]), out["item"]["due_at"], out["item"]["body_md"]),
                         (["due_at", "title"], None, d["body_md"]))
        self.assertEqual(self.ok(self.put(f"/items/{d['id']}", json={"title": "Новое"}))["changed"], [])
        self.refused(self.put(f"/items/{d['id']}", json={"kind": "contest"}), "invalid_request")
        self.refused(self.put(f"/items/{d['id']}", json={"due_at": "завтра"}), "due_at_invalid")
        self.refused(self.put(f"/items/{d['id']}", json={"available_from": "x"}), "available_from_invalid")
        actions = list(self.s.execute(select(AuditLog.action).where(AuditLog.object_id == str(d["id"]),
                                                                    AuditLog.object_type == "feed_item")
                                      .order_by(AuditLog.id)).scalars())
        self.assertEqual(actions, ["item.create", "item.audience", "item.update"])
        self.assertEqual(self.s.execute(select(AuditLog.actor_user_id).where(AuditLog.action == "item.update"))
                         .scalar(), self.boss.id)

    def test_scale_change_one_rule_in_the_service(self):
        d = self.item()
        out = self.ok(self.put(f"/items/{d['id']}", json={"grading": "pass_fail"}))
        self.assertEqual((out["item"]["grading"], out["item"]["max_points"]), ("pass_fail", None))
        self.refused(self.put(f"/items/{d['id']}", json={"grading": "none", "max_points": 5}), "max_points_invalid")
        self.refused(self.put(f"/items/{d['id']}", json={"grading": "points"}), "max_points_invalid")
        out = self.ok(self.put(f"/items/{d['id']}", json={"grading": "points", "max_points": "4.5"}))
        self.assertEqual(out["item"]["max_points"], "4.50")
        self.ok(self.put(f"/items/{d['id']}", json={"grading": "none", "max_points": None}))

    def test_states_delete_list_and_group_feed(self):
        a = self.item(title="первое")
        b = self.item(title="второе", group_ids=[], user_ids=[self.sidorov.id])
        self.refused(self.post(f"/items/{a['id']}/archive"), "item_not_published")
        self.assertTrue(self.ok(self.post(f"/items/{a['id']}/publish"))["changed"])
        self.assertFalse(self.ok(self.post(f"/items/{a['id']}/publish"))["changed"])
        self.refused(self.post(f"/items/{a['id']}/delete", json={"confirm_id": a["id"]}), "item_not_draft")
        self.ok(self.post(f"/items/{a['id']}/archive"))
        self.refused(self.post(f"/items/{a['id']}/publish"), "item_archived")
        self.assertEqual(self.ok(self.post(f"/items/{a['id']}/unarchive"))["item"]["state"], "published")
        # list: cursor by id, filters
        page = self.ok(self.get("/items", params={"limit": 1}))
        self.assertEqual(([i["id"] for i in page["items"]], page["next_cursor"]), ([b["id"]], b["id"]))
        page = self.ok(self.get("/items", params={"limit": 1, "cursor": page["next_cursor"]}))
        self.assertEqual(([i["id"] for i in page["items"]], page["next_cursor"]), ([a["id"]], None))
        self.assertEqual([i["id"] for i in self.ok(self.get("/items", params={"group_id": self.group.id}))["items"]],
                         [a["id"]])
        self.assertEqual([i["id"] for i in self.ok(self.get("/items", params={"state": "draft"}))["items"]],
                         [b["id"]])
        self.refused(self.get("/items", params={"state": "gone"}), "item_invalid")
        feed = self.ok(self.get(f"/groups/{self.group.id}/feed"))["items"]
        self.assertEqual([i["id"] for i in feed], [a["id"]])
        # delete a draft: confirmation, then 204
        self.refused(self.post(f"/items/{b['id']}/delete", json={"confirm_id": a["id"]}), "confirmation_mismatch")
        self.assertIsNone(self.ok(self.post(f"/items/{b['id']}/delete", json={"confirm_id": b["id"]}), 204))
        self.refused(self.get(f"/items/{b['id']}"), "item_not_found")

    def test_audience_and_contest_warning(self):
        d = self.published()
        out = self.ok(self.put(f"/items/{d['id']}/audience", json={"group_ids": [self.group.id],
                                                                    "user_ids": [self.sidorov.id]}))
        self.assertEqual((out["added"], out["removed"]), ([f"student {self.sidorov.login}"], []))
        self.refused(self.put(f"/items/{d['id']}/audience", json={}), "no_audience")
        self.refused(self.put(f"/items/{d['id']}/audience", json={"user_ids": [self.boss.id]}), "not_a_student")
        before = self.audit_count()
        r = self.refused(self.put(f"/items/{d['id']}/audience", json={"user_ids": [self.ivanov.id, 999999, 999998]}),
                         "user_not_found")
        self.assertEqual(len(r["problems"]), 2)
        self.assertEqual(self.audit_count(), before)
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        contest.access_mode = "groups"
        self.s.commit()
        link = self.ok(self.post("/items", json={"kind": "contest", "title": "Олимпиада", "contest": contest.slug,
                                                  "group_ids": [self.group.id]}), 201)
        self.assertIn("groups not attached to contest", link["warning"])

    def test_extensions(self):
        d = self.published()
        r = self.post(f"/items/{d['id']}/extensions", json={"user_id": self.ivanov.id, "due_at": "2026-10-30 23:59",
                                                             "reason": SCRIPT})
        made = self.ok(r, 201)
        self.assertEqual((made["extension"]["due_at"], made["extension"]["reason"], made["replaced"]),
                         ("2026-10-30T16:59:00Z", SCRIPT, None))
        again = self.ok(self.post(f"/items/{d['id']}/extensions", json={"user_id": self.ivanov.id,
                                                                         "due_at": "2026-10-31 23:59"}), 200)
        self.assertEqual(again["replaced"], "2026-10-30T16:59:00Z")
        self.assertEqual(len(self.ok(self.get(f"/items/{d['id']}/extensions"))["items"]), 1)
        self.assertIsNone(self.ok(self.post(f"/items/{d['id']}/extensions/{made['extension']['id']}/delete"), 204))
        self.refused(self.post(f"/items/{d['id']}/extensions/{made['extension']['id']}/delete"), "extension_not_found")

    # ---------------------------------------------------------------- works
    def test_work_card_by_the_pair_only(self):
        d = self.published(due_at="2026-10-27 18:00")  # 11:00 UTC: past by the end of this test
        other = self.published()
        self.ok(self.put(f"/items/{other['id']}/audience", json={"group_ids": [self.group.id],
                                                                  "user_ids": [self.sidorov.id]}))
        self.hand_in(self.sidorov, other["id"], text=SCRIPT)
        self.tick(hours=3)
        # an addressee without work: 200 assigned, missing computed
        card = self.ok(self.get(f"/items/{d['id']}/works/{self.ivanov.id}"))
        self.assertEqual((card["work_id"], card["status"], card["events"], card["missing"]),
                         (None, "assigned", [], True))
        self.assertEqual(card["student_sees"]["missing"], True)
        # not addressed and no work - and a work of the same student on another item - 404
        self.refused(self.get(f"/items/{d['id']}/works/{self.sidorov.id}"), "work_not_found")
        self.refused(self.get(f"/items/999999/works/{self.sidorov.id}"), "item_not_found")
        self.refused(self.get(f"/items/{d['id']}/works/999999"), "user_not_found")
        # the right pair shows the answer byte for byte
        r = self.get(f"/items/{other['id']}/works/{self.sidorov.id}")
        self.assertEqual(self.ok(r)["events"][0]["text"], SCRIPT)
        self.assertIn(SCRIPT.encode(), r.content)
        # a grade without a hand-in from the card of the addressee
        out = self.ok(self.post(f"/items/{d['id']}/works/{self.ivanov.id}/grade",
                                json={"score": 0, "comment": SCRIPT}))
        self.assertEqual((out["result"]["status"], out["work"]["status"], out["work"]["events"][0]["comment"]),
                         ("graded", "graded", SCRIPT))
        self.refused(self.post(f"/items/{d['id']}/works/{self.sidorov.id}/grade", json={"score": 1}),
                     "not_addressed")

    def test_score_input(self):
        d = self.published()
        url = f"/items/{d['id']}/works/{self.ivanov.id}/grade"
        for bad in ("4.555", "1e1", "NaN", "Infinity", 4.555, "4,5"):
            with self.subTest(score=bad):
                self.refused(self.post(url, json={"score": bad}), "score_invalid")
        self.refused(self.post(url, json={"score": True}), "invalid_request")  # StrictBool is no number
        raw = self.b.post(f"/api/admin{url}", content=b'{"score": NaN}',
                          headers={**self.h, "Content-Type": "application/json"})
        self.refused(raw, "score_invalid")  # a NaN literal parses to a float: parse_points refuses it
        self.refused(self.post(url, json={"score": "-1"}), "score_out_of_range")
        self.assertEqual(self.ok(self.post(url, json={"score": "4.5"}))["work"]["events"][0]["score"], "4.50")
        self.assertEqual(self.ok(self.post(url, json={"score": 4.5}))["work"]["events"][1]["score"], "4.50")
        scores = list(self.s.execute(select(AssignmentReview.score).order_by(AssignmentReview.id)).scalars())
        self.assertEqual([str(s) for s in scores], ["4.50", "4.50"])
        self.refused(self.post("/items", json={"kind": "assignment", "title": "З", "grading": "points",
                                               "max_points": "1e1"}), "max_points_invalid")

    def test_queue_return_release(self):
        d = self.published(user_ids=[self.sidorov.id], due_at="2026-10-27 18:00")
        self.hand_in(self.ivanov, d["id"], text="ответ")
        self.tick(hours=3)  # past due
        self.hand_in(self.petrova, d["id"], text="поздно")
        rows = self.ok(self.get("/works", params={"item_id": d["id"]}))["items"]
        self.assertEqual([(r["login"], r["status"], r["late"], r["missing"]) for r in rows],
                         [(self.sidorov.login, "assigned", False, True), (self.petrova.login, "submitted", True, False),
                          (self.ivanov.login, "submitted", False, False)])
        page = self.ok(self.get("/works", params={"item_id": d["id"], "limit": 2}))
        self.assertEqual(page["next_cursor"], self.petrova.id)
        page = self.ok(self.get("/works", params={"item_id": d["id"], "limit": 2, "cursor": page["next_cursor"]}))
        self.assertEqual([r["login"] for r in page["items"]], [self.ivanov.login])
        self.assertEqual([r["login"] for r in self.ok(self.get("/works", params={
            "item_id": d["id"], "missing": "true"}))["items"]], [self.sidorov.login])
        self.assertEqual([r["login"] for r in self.ok(self.get("/works", params={"late": "true"}))["items"]],
                         [self.petrova.login])
        self.assertEqual([r["login"] for r in self.ok(self.get("/works", params={
            "group_id": self.group.id, "status": "submitted"}))["items"]], [self.petrova.login, self.ivanov.login])
        self.refused(self.get("/works", params={"missing": "true"}), "missing_needs_item")
        self.refused(self.get("/works", params={"status": "lost"}), "invalid_request")
        # return, regrade, release per student
        base = f"/items/{d['id']}/works"
        self.refused(self.post(f"{base}/{self.petrova.id}/return", json={"comment": " "}), "comment_required")
        out = self.ok(self.post(f"{base}/{self.petrova.id}/return", json={"comment": SCRIPT}))
        self.assertEqual((out["result"]["status"], out["work"]["student_sees"]["returned_comment"]),
                         ("returned", SCRIPT))
        self.refused(self.post(f"{base}/{self.petrova.id}/return", json={"comment": "ещё"}), "already_returned")
        self.ok(self.post(f"{base}/{self.ivanov.id}/grade", json={"score": 8}))
        self.ok(self.post(f"{base}/{self.sidorov.id}/grade", json={"score": 0, "release": True}))
        before = self.audit_count()
        self.refused(self.post(f"/items/{d['id']}/release", json={"user_ids": [self.ivanov.id, 999999]}),
                     "user_not_found")
        self.assertEqual(self.audit_count(), before)
        results = self.ok(self.post(f"/items/{d['id']}/release", json={}))["results"]
        self.assertEqual({r["login"]: r["result"] for r in results},
                         {self.ivanov.login: "released", self.petrova.login: "nothing_to_release",
                          self.sidorov.login: "already_released"})
        results = self.ok(self.post(f"/items/{d['id']}/release", json={"user_ids": [self.ivanov.id]}))["results"]
        self.assertEqual(results, [{"user_id": self.ivanov.id, "login": self.ivanov.login,
                                    "result": "already_released"}])
        card = self.ok(self.get(f"{base}/{self.ivanov.id}"))
        self.assertEqual(card["student_sees"]["grade"]["score"], "8.00")

    def test_group_archive_adds_feed_items_only(self):
        d = self.item(title=SCRIPT)
        other = g.create(self.s, self.cli, "8В", self.now)
        self.s.commit()
        old = self.ok(self.post(f"/groups/{other.id}/archive"))
        self.assertEqual(set(old), {"group", "contests", "feed_items"})
        self.assertEqual(old["feed_items"], [])
        out = self.ok(self.post(f"/groups/{self.group.id}/archive"))
        self.assertEqual(set(out), set(old))
        self.assertEqual(set(out["group"]), set(old["group"]))
        self.assertEqual(out["feed_items"], [{"id": d["id"], "kind": "assignment", "state": "draft",
                                              "title": SCRIPT}])

    # ---------------------------------------------------------------- every code
    def test_every_code_with_an_http_status_is_reached(self):
        """Each code of STATUS but those only a student's hand-in or comment produces (4b.2), the per-student results
        of a release and the defensive ones is the answer of at least one request here."""
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        archived = g.create(self.s, self.cli, "6А", self.now)
        g.archive(self.s, self.cli, archived, self.now)
        self.s.commit()
        P = self.post

        def create(code, **kw):
            body = {"kind": "assignment", "title": "З", **kw}
            self.refused(P("/items", json=body), code)

        create("title_invalid", title="  ", grading="none")
        create("material_required", kind="material")
        create("material_not_found", kind="material", material="no-such")
        self.s.add(Material(slug="old-memo", title="Старая", archived_at=self.now, created_at=self.now,
                            updated_at=self.now))
        self.s.commit()
        create("material_archived", kind="material", material="old-memo")
        create("contest_required", kind="contest")
        create("item_invalid", kind="lesson")
        create("grading_invalid")
        create("max_points_invalid", grading="points")
        create("answer_kind_invalid", grading="none", answer_kind="photo")
        create("deadline_invalid", grading="none", available_from="2026-10-29 10:00", due_at="2026-10-29 10:00")
        create("due_at_invalid", grading="none", due_at="потом")
        create("available_from_invalid", grading="none", available_from="потом")
        create("contest_not_found", kind="contest", contest="no-such")
        create("group_not_found", grading="none", group_ids=[999999])
        create("user_not_found", grading="none", user_ids=[999999])
        create("not_a_student", grading="none", user_ids=[self.boss.id])
        create("group_archived", grading="none", group_ids=[archived.id])
        self.refused(self.get("/items/999999"), "item_not_found")

        draft = self.item()
        empty = self.item(group_ids=[])
        self.refused(P(f"/items/{empty['id']}/publish"), "no_audience")
        self.refused(P(f"/items/{draft['id']}/archive"), "item_not_published")
        self.refused(P(f"/items/{draft['id']}/delete", json={"confirm_id": 0}), "confirmation_mismatch")
        self.ok(P(f"/items/{draft['id']}/extensions", json={"user_id": self.ivanov.id, "due_at": "2026-10-31"}), 201)
        self.refused(P(f"/items/{draft['id']}/delete", json={"confirm_id": draft["id"]}), "item_has_extensions")
        self.refused(P(f"/items/{draft['id']}/works/{self.ivanov.id}/grade", json={"score": 1}), "not_published")

        d = self.published()
        ext = f"/items/{d['id']}/extensions"
        self.refused(P(ext, json={"user_id": self.ivanov.id, "group_id": self.group.id, "due_at": "2026-10-31"}),
                     "extension_subject")
        self.refused(P(ext, json={"user_id": self.ivanov.id, "due_at": "2026-10-28"}), "extension_not_later")
        self.refused(P(f"{ext}/999999/delete"), "extension_not_found")
        no_due = self.published(due_at=None)
        self.refused(P(f"/items/{no_due['id']}/extensions", json={"user_id": self.ivanov.id,
                                                                   "due_at": "2026-10-31"}), "no_deadline")
        link = self.ok(P("/items", json={"kind": "contest", "title": "К", "contest": contest.slug}), 201)
        self.refused(P(f"/items/{link['id']}/extensions", json={"user_id": self.ivanov.id, "due_at": "2026-10-31"}),
                     "not_an_assignment")
        self.ok(P(f"/items/{d['id']}/archive"))
        self.refused(P(f"/items/{d['id']}/publish"), "item_archived")
        self.ok(P(f"/items/{d['id']}/unarchive"))
        self.refused(P(f"/items/{d['id']}/delete", json={"confirm_id": d["id"]}), "item_not_draft")

        w = f"/items/{d['id']}/works"
        self.refused(self.get(f"{w}/{self.sidorov.id}"), "work_not_found")
        self.refused(P(f"{w}/{self.sidorov.id}/grade", json={"score": 1}), "not_addressed")
        self.refused(P(f"{w}/{self.ivanov.id}/grade", json={"score": "1e1"}), "score_invalid")
        self.refused(P(f"{w}/{self.ivanov.id}/grade", json={}), "score_required")
        self.refused(P(f"{w}/{self.ivanov.id}/grade", json={"score": 11}), "score_out_of_range")
        self.refused(P(f"{w}/{self.ivanov.id}/grade", json={"score": 1, "passed": True}), "grade_kind_mismatch")
        self.refused(P(f"{w}/{self.ivanov.id}/grade", json={"score": 1, "comment": "я" * 2001}), "comment_too_long")
        self.refused(P(f"{w}/{self.ivanov.id}/return", json={"comment": "  "}), "comment_required")
        self.refused(P(f"{w}/{self.ivanov.id}/return", json={"comment": "доделай"}), "nothing_to_return")
        self.hand_in(self.ivanov, d["id"])
        self.ok(P(f"{w}/{self.ivanov.id}/return", json={"comment": "доделай"}))
        self.refused(P(f"{w}/{self.ivanov.id}/return", json={"comment": "ещё"}), "already_returned")
        self.ok(P(f"{w}/{self.ivanov.id}/grade", json={"score": 5}))
        self.refused(self.put(f"/items/{d['id']}", json={"max_points": 20}), "grading_locked")
        self.refused(self.get("/works", params={"missing": "true"}), "missing_needs_item")

        pf = self.published(grading="pass_fail", max_points=None)
        self.refused(P(f"/items/{pf['id']}/works/{self.ivanov.id}/grade", json={}), "passed_required")
        quick = self.published(grading="none", max_points=None)
        self.refused(P(f"/items/{quick['id']}/works/{self.ivanov.id}/grade", json={"score": 1}),
                     "grade_not_expected")
        self.refused(P(f"/items/{quick['id']}/release", json={}), "release_not_needed")
        results = self.ok(P(f"/items/{d['id']}/release", json={"user_ids": [self.ivanov.id, self.petrova.id]}))
        self.reached |= {r["result"] for r in results["results"]} - {"released"}
        results = self.ok(P(f"/items/{d['id']}/release", json={"user_ids": [self.ivanov.id]}))
        self.reached |= {r["result"] for r in results["results"]}

        # comments (§5.9)
        c = f"/items/{d['id']}/comments"
        self.refused(self.get(f"/items/{d['id']}/students/{self.sidorov.id}/comments"), "thread_not_found")
        self.refused(P(c, json={"body": "a\x00"}), "comment_invalid")
        posted = self.ok(P(c, json={"body": "Объявление"}), 201)
        self.refused(P(f"{c}/read", json={"up_to": 999999}), "comment_not_found")
        self.refused(P(f"/comments/{posted['id']}/delete", json={"reason": "я" * 201}), "reason_too_long")
        self.ok(P(f"/comments/{posted['id']}/delete", json={}))
        self.refused(P(f"/comments/{posted['id']}/delete", json={}), "already_deleted")

        # files and links (§5.10)
        a = f"/items/{d['id']}/attachments"

        def upload(name, data):
            return self.b.post(f"/api/admin{a}?name={name}", content=data,
                               headers={**self.h, "Content-Type": "application/octet-stream"})

        self.refused(upload("x.svg", b"<svg/>"), "file_type_not_allowed")
        self.refused(upload("x.png", b"<html></html>"), "file_content_mismatch")
        self.refused(upload("x.txt", b""), "file_empty")
        self.ok(upload("x.txt", b"1"), 201)
        self.refused(upload("X.TXT", b"2"), "duplicate_name")
        self.refused(P(f"/items/{d['id']}/links", json={"url": "javascript:alert(1)"}), "link_invalid")
        self.refused(P(f"/items/{d['id']}/links", json={"url": "https://a.ru", "title": "я" * 201}),
                     "link_title_invalid")
        self.refused(self.put(f"{a}/order", json={"ids": []}), "attachment_order_invalid")
        self.refused(self.get(f"{a}/999999"), "attachment_not_found")
        for n in range(19):
            self.ok(P(f"/items/{d['id']}/links", json={"url": f"https://a.ru/{n}"}), 201)
        self.refused(P(f"/items/{d['id']}/links", json={"url": "https://b.ru"}), "too_many_attachments")
        self.refused(self.get(f"/items/{d['id']}/works/{self.ivanov.id}/files/999999"), "file_not_found")

        # the library of materials (§5.11)
        self.refused(P("/materials", json={"slug": "Bad Slug", "title": "М"}), "slug_invalid")
        self.refused(P("/materials", json={"slug": "memo", "title": "М", "body_md": "\x00"}), "body_invalid")
        self.refused(P("/materials", json={"slug": "memo", "title": "М", "body_md": "я" * 100_001}),
                     "body_too_long")
        self.refused(P("/materials", json={"slug": "memo", "title": "М", "library": "everyone"}), "library_invalid")
        m = self.ok(P("/materials", json={"slug": "memo", "title": "Памятка"}), 201)
        self.refused(P("/materials", json={"slug": "memo", "title": "Ещё"}), "slug_taken")
        self.refused(self.b.patch(f"/api/admin/materials/{m['id']}", json={"slug": "other"}, headers=self.h),
                     "slug_immutable")
        self.refused(self.get(f"/materials/{m['id']}/versions/9"), "version_not_found")
        self.refused(P("/library/sections", json={"title": "Тема", "parent_id": 999999}), "section_not_found")
        self.refused(P("/library/sections", json={"title": "  "}), "section_title_invalid")
        top = self.ok(P("/library/sections", json={"title": "Тема"}), 201)
        sub = self.ok(P("/library/sections", json={"title": "Подтема", "parent_id": top["id"]}), 201)
        self.refused(P("/library/sections", json={"title": "Глубже", "parent_id": sub["id"]}), "section_too_deep")
        self.refused(P(f"/library/sections/{top['id']}/move", json={"parent_id": top["id"]}), "section_invalid")
        self.refused(self.put("/library/sections/order", json={"ids": []}), "order_invalid")
        self.refused(P(f"/library/sections/{top['id']}/delete", json={"confirm_id": top["id"]}), "section_not_empty")

        expected = set(STATUS) - HAND_IN_ONLY - COMMENT_STUDENT_ONLY - DEFENSIVE - NO_ROUTE_YET
        self.assertEqual(NO_ROUTE_YET, set())
        self.assertEqual(sorted(expected - self.reached), [])
        self.assertLessEqual(RESULT_ONLY, self.reached)
