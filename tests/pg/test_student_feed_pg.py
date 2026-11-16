"""The read services of a student's class feed (app/student/feed.py, app/student/classroom.py; 4b.2, D-100,
docs/STUDENT_API.md §5) on a real PostgreSQL: the feed (their groups and personal addressing, each item once, newest
first, by a cursor), the page of an item (the statement where it is open, the own work, hand-ins and draft always),
their own works, and what the thin wrappers refuse; nothing of another student, no sha256, no unreleased grade.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import json
from datetime import timedelta

from tests.pg.pgbase import HAVE_SA, URL, requires_pg
from tests.pg.test_attachments_pg import DUE, PDF, T0, AttachmentServiceTests

if URL and HAVE_SA:
    from app import groups as g
    from app.classroom import comments, drafts, items, works
    from app.classroom.attachments import LinkIn
    from app.materials import service as ms
    from app.models import Material
    from app.student import classroom as sc
    from app.student import feed as sf

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
S = timedelta(seconds=1)


@requires_pg
class StudentFeedTests(AttachmentServiceTests):
    """The helpers of the attachment tests (items, uploads, the group 7Б of a and b); none of their tests run here."""

    # ---------------------------------------------------------------- helpers
    def item(self, publish=True, answer_kind="text_or_files", body_md="Сделай", **fields):
        base = dict(grading="pass_fail", due_at=DUE, answer_kind=answer_kind)
        base.update(fields)
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Проект", body_md=body_md, **base)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def material(self, slug="cycles", title="Циклы", body="Цикл for", **kw):
        result = ms.create_material(self.s, self.actor, T0, slug=slug, title=title, body_md=body, **kw)
        self.s.commit()
        return self.s.get(Material, result.version.material_id)

    def material_item(self, material):
        item = items.create_item(self.s, self.actor, T0, kind="material", title="", material=material)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def hand_in(self, item, user=None, text="ответ", **kw):
        works.submit_work(self.s, user or self.a, item, kw.pop("now", T0), blobs=self.blobs, text=text, **kw)
        self.s.commit()

    def page(self, item, user=None, now=T0):
        return sf.item_page(self.s, user or self.a, item.id, now, blobs=self.blobs)

    def ids(self, user=None, now=T0, **kw):
        return [row["id"] for row in sf.my_feed(self.s, user or self.a, now, **kw)["items"]]

    # ---------------------------------------------------------------- the feed
    def test_the_feed_is_the_students_own_newest_first(self):
        old = self.item(available_from=T0 - timedelta(days=2))
        new = self.item(available_from=T0 - timedelta(days=1))
        self.item(publish=False)  # a draft
        self.item(available_from=T0 + timedelta(hours=1))  # not yet
        personal = items.create_item(self.s, self.actor, T0, kind="assignment", title="Лично", grading="none",
                                     answer_kind="text", available_from=T0 - timedelta(hours=1))
        items.set_audience(self.s, self.actor, personal, T0, users=[self.a])
        items.publish(self.s, self.actor, personal, T0)
        both = self.item(available_from=T0 - timedelta(days=3))
        items.add_audience(self.s, self.actor, both, T0, users=[self.a])  # the group and personally: once
        self.s.commit()
        self.assertEqual(self.ids(), [personal.id, new.id, old.id, both.id])
        self.assertEqual(self.ids(user=self.b), [new.id, old.id, both.id])
        self.assertEqual(self.ids(user=self.outsider), [])
        self.refused("not_a_student", sf.my_feed, self.s, self.admin, T0)

    def test_pages_by_a_cursor(self):
        made = [self.item(available_from=T0 - timedelta(hours=n)) for n in range(1, 6)]
        first = sf.my_feed(self.s, self.a, T0, limit=2)
        self.assertEqual([r["id"] for r in first["items"]], [made[0].id, made[1].id])
        second = sf.my_feed(self.s, self.a, T0, limit=2, cursor=first["next_cursor"])
        self.assertEqual([r["id"] for r in second["items"]], [made[2].id, made[3].id])
        items.archive(self.s, self.actor, made[2], T0)  # an item leaving between pages moves nothing else
        self.s.commit()
        last = sf.my_feed(self.s, self.a, T0, limit=2, cursor=second["next_cursor"])
        self.assertEqual(([r["id"] for r in last["items"]], last["next_cursor"]), ([made[4].id], None))
        self.assertEqual(sf.my_feed(self.s, self.a, T0, cursor=2_147_483_647)["items"], [])

    def test_rows_of_each_kind(self):
        task = self.item(grading="points", max_points=10, answer_kind="files", comments_enabled=True)
        items.add_extension(self.s, self.actor, task, DUE + timedelta(days=1), T0, user=self.a)
        contest = self.contest_item()
        m = self.material(library="all_students")
        note = self.material_item(m)
        comments.teacher_post(self.s, self.actor, task, T0, "Вопросы сюда")
        self.s.commit()
        rows = {r["id"]: r for r in sf.my_feed(self.s, self.a, T0)["items"]}
        a = rows[task.id]["assignment"]
        self.assertEqual((a["answer_kind"], a["grading"], a["due_at"], a["status"], a["missing"]),
                         ("files", "points", DUE + timedelta(days=1), None, False), "no work: no status")
        self.assertEqual(rows[task.id]["unread"], 1)
        self.assertEqual(sf.my_feed(self.s, self.b, T0)["items"][-1]["assignment"]["due_at"], DUE,
                         "another student's extension is not theirs")
        self.assertEqual(rows[contest.id]["contest"]["slug"], "c")
        self.assertIn("has_access", rows[contest.id]["contest"])
        self.assertEqual(rows[note.id]["material"], {"id": m.id, "title": "Циклы"})

    # ---------------------------------------------------------------- the page of an item
    def test_the_page_of_an_assignment(self):
        m = self.material(library="all_students")
        task = self.item(body_md="Читай [[памятка:cycles]] и [[памятка:nope]]", answer_kind="text_or_files")
        self.attach(task, "схема.pdf", PDF)
        self.link(task)
        with self.upload("a.jpg", JPEG) as up:
            works.submit_work(self.s, self.a, task, T0, blobs=self.blobs, text="мой ответ", files=[up],
                              links=[LinkIn("https://e.ru/1")])
        self.s.commit()
        with self.upload("b.jpg", JPEG + b"!") as up:
            drafts.add_file(self.s, self.a, task, up, T0)
        self.s.commit()
        page = self.page(task)
        self.assertFalse(page["statement_hidden"])
        self.assertEqual(page["body_md"], task.body_md)
        self.assertEqual([(x["kind"], x.get("name")) for x in page["attachments"]], [("file", "схема.pdf"),
                                                                                    ("link", None)])
        self.assertEqual(page["links"], [{"slug": "cycles", "available": True, "id": m.id, "title": "Циклы"},
                                         {"slug": "nope", "available": False}])
        self.assertEqual(page["can_submit"], {"allowed": True, "reason": "ok", "late": False})
        [hand_in] = page["hand_ins"]
        self.assertEqual((hand_in["text"], [f["name"] for f in hand_in["files"]], len(hand_in["links"])),
                         ("мой ответ", ["a.jpg"], 1), "the text answer is text, not a file")
        self.assertEqual([(d["name"], d["expires_at"]) for d in page["draft"]], [("b.jpg", DUE + timedelta(days=14))])
        self.assertEqual(page["assignment"]["status"], "submitted")
        self.assertEqual(page["comments"], {"enabled": False, "shared_unread": 0, "private_unread": 0})
        dumped = json.dumps(page, default=str)
        self.assertNotIn("sha256", dumped)
        self.assertNotIn("created_by", dumped)
        other = self.page(task, user=self.b)
        self.assertEqual((other["hand_ins"], other["draft"], other["assignment"]["status"]), ([], [], None))

    def test_a_hidden_statement_keeps_the_own_work(self):
        task = self.item(after_due_statement="hidden", after_due_submit="no", body_md="Секрет [[памятка:cycles]]")
        file = self.attach(task)
        self.hand_in(task)
        after = DUE + S
        page = self.page(task, now=after)
        self.assertEqual((page["statement_hidden"], page["body_md"], page["attachments"], page["links"]),
                         (True, None, None, []))
        self.assertNotIn("Секрет", json.dumps(page, default=str))
        self.assertEqual(len(page["hand_ins"]), 1)
        self.assertEqual(page["can_submit"]["reason"], "deadline_passed")
        self.assertEqual(sc.item_file(self.s, self.a, task.id, file.id, DUE).name, "схема.pdf")
        self.refused("attachment_not_found", sc.item_file, self.s, self.a, task.id, file.id, after)

    def test_what_the_student_does_not_see_is_not_found(self):
        task = self.item()
        draft = self.item(publish=False)
        later = self.item(available_from=T0 + timedelta(days=1))
        for item, user in ((task, self.outsider), (draft, self.a), (later, self.a)):
            with self.subTest(item=item.id, user=user.login):
                self.refused("item_not_found", sf.item_page, self.s, user, item.id, T0, blobs=self.blobs)
                self.refused("item_not_found", sc.my_thread, self.s, user, item.id, T0)
                self.refused("item_not_found", sc.work_file, self.s, user, item.id, 1, T0)
        self.refused("item_not_found", sf.item_page, self.s, self.a, 2_147_483_647, T0, blobs=self.blobs)

    def test_an_archived_item_or_a_removed_student_keeps_the_own_work(self):
        task = self.item()
        self.hand_in(task)
        items.archive(self.s, self.actor, task, T0)
        g.remove_members(self.s, self.actor, self.group, [self.a], T0)
        self.s.commit()
        self.assertEqual(self.ids(), [])
        page = self.page(task)
        self.assertEqual((page["archived"], page["statement_hidden"], len(page["hand_ins"])), (True, True, 1))
        self.assertEqual(page["can_submit"]["reason"], "item_archived")
        self.refused("item_not_found", sf.item_page, self.s, self.b, task.id, T0, blobs=self.blobs)

    def test_an_unreleased_grade_is_not_seen_anywhere(self):
        task = self.item(grading="points", max_points=10, grades_release="manual")
        self.hand_in(task)
        works.grade_work(self.s, self.actor, task, self.a, T0 + S, score=7, comment="почти")
        self.s.commit()
        for view in (self.page(task)["assignment"], sf.my_feed(self.s, self.a, T0 + S)["items"][0]["assignment"],
                     sf.my_works(self.s, self.a, T0 + S)[0]):
            self.assertEqual((view["status"], view["grade"]), ("submitted", None))
        self.assertNotIn("почти", json.dumps(self.page(task), default=str))
        works.release_grades(self.s, self.actor, task, T0 + 2 * S)
        self.s.commit()
        self.assertEqual(sf.my_works(self.s, self.a, T0 + 2 * S)[0]["grade"]["comment"], "почти")

    # ---------------------------------------------------------------- my works
    def test_my_works_by_my_own_hand_ins(self):
        first, second = self.item(), self.item()
        self.hand_in(first, now=T0)
        self.hand_in(second, now=T0 + S)
        self.hand_in(second, user=self.b, now=T0 + 2 * S)
        works.return_work(self.s, self.actor, first, self.a, T0 + 3 * S, "переделай")  # a review moves nothing
        items.archive(self.s, self.actor, second, T0)
        self.s.commit()
        mine = sf.my_works(self.s, self.a, T0 + 4 * S)
        self.assertEqual([w["item"]["id"] for w in mine], [second.id, first.id])
        self.assertEqual((mine[0]["item"]["archived"], mine[1]["status"]), (True, "returned"))
        self.assertEqual(sf.my_works(self.s, self.outsider, T0), [])

    # ---------------------------------------------------------------- comments, materials
    def test_the_rate_of_comments_says_when_to_retry(self):
        task = self.item(comments_enabled=True)
        sc.post_shared(self.s, self.a, task.id, T0, "раз", limit=2)
        sc.post_private(self.s, self.a, task.id, T0 + 10 * S, "два", limit=2)
        self.s.commit()
        caught = self.refused("rate_limited", sc.post_shared, self.s, self.a, task.id, T0 + 20 * S, "три", limit=2)
        self.assertEqual(caught.data["retry_after_s"], 40)
        caught = self.refused("rate_limited", sc.post_private, self.s, self.a, task.id, T0 + 59 * S, "три", limit=2)
        self.assertEqual(caught.data["retry_after_s"], 1)
        card = sc.post_shared(self.s, self.a, task.id, T0 + 60 * S, "три", limit=2)
        self.assertEqual((card["body"], card["author"]["display_name"], card["mine"]), ("три", "S1", True))
        self.assertNotIn("login", json.dumps(card, default=str))

    def test_threads_and_marks_are_the_students_own(self):
        task = self.item(comments_enabled=True)
        sc.post_private(self.s, self.b, task.id, T0, "мой вопрос")
        comments.teacher_post(self.s, self.actor, task, T0, "ответ", student=self.a)
        self.s.commit()
        mine = sc.my_thread(self.s, self.a, task.id, T0)
        self.assertEqual([c["body"] for c in mine["comments"]], ["ответ"])
        self.assertEqual(sc.read_mine(self.s, self.a, task.id, T0), 0)
        theirs = sc.my_thread(self.s, self.b, task.id, T0)["comments"][0]["id"]
        self.refused("comment_not_found", sc.read_mine, self.s, self.a, task.id, T0, up_to=theirs)
        self.refused("comment_not_found", sc.delete_comment, self.s, self.a, theirs, T0)

    def test_a_material_without_hashes(self):
        m = self.material(library="all_students")
        with self.upload("пример.py", b"print(1)") as up:
            ms.add_file(self.s, self.actor, m, up, T0)
        self.s.commit()
        view = sc.material(self.s, self.a, m.id, T0)
        self.assertEqual([a["name"] for a in view["attachments"]], ["пример.py"])
        self.assertNotIn("sha256", json.dumps(view, default=str))
        self.assertEqual(sc.material_file(self.s, self.a, m.id, view["attachments"][0]["id"], T0).name, "пример.py")
        hidden = self.material(slug="secret", title="Секрет")
        self.refused("material_not_found", sc.material, self.s, self.a, hidden.id, T0)
        self.refused("material_not_found", sc.material, self.s, self.a, 2_147_483_647, T0)
        self.assertEqual([c["slug"] for c in sc.library(self.s, self.a, T0)["unsorted"]], ["cycles"])


# The attachment tests run in their own module: here only their helpers are inherited (a None attribute is not
# collected), and the imported class is not left in this module for the loader to find.
for _name in dir(AttachmentServiceTests):
    if _name.startswith("test_") and _name not in vars(StudentFeedTests):
        setattr(StudentFeedTests, _name, None)
del AttachmentServiceTests, _name
