"""judgectl item / work and group archive with the class feed (docs/JUDGECTL.md, CLASSROOM.md) on a real PostgreSQL:
every command, times in JUDGE_TIMEZONE, refusals with their code and nothing written, --json, and text typed by
students or teachers printed without control characters.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import io
import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 10:00 in Novosibirsk
HOSTILE = "ответ\x1b[2J\x1b]0;pwned\x07 abc\rXYZ ‮txt.exe\x9b"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import groups as g
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.classroom import items, works
    from app.contests import deadlines
    from app.contests.service import get_contest
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import (AssignmentReview, AuditLog, Contest, DeadlineExtension, FeedItem, FeedItemAudience,
                            Group)
    from app.settings import Settings


def controls(text):
    """Raw control characters in judgectl's output (none may get through)."""
    return [hex(ord(ch)) for ch in text if ch not in "\n" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                                or 0x202A <= ord(ch) <= 0x202E)]


@requires_pg
class JudgectlFeedTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blob_dir = self.tmp / "blobs"
        self.actor = audit.Actor(user_id=self.seed["admin"].id)
        self.a, self.b, self.c = self.student(1), self.student(2), self.student(3)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], T0)
        self.s.commit()
        self.clock = T0

    def run_cli(self, *argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        settings = Settings(timezone="Asia/Novosibirsk", blob_dir=str(self.blob_dir))
        ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:judge"),
                      out=out, err=err, stdin=io.BytesIO(stdin), interactive=lambda: False, clock=lambda: self.clock)
        code = main([str(a) for a in argv], ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, err)
        return out, err

    def js(self, *argv, **kw):
        return json.loads(self.ok(*argv, "--json", **kw)[0])

    def refused(self, code, *argv, **kw):
        """Exit 1, `judgectl: ... (<code>)` on stderr, nothing written (audit_log as the witness)."""
        before = self.s.execute(select(func.count()).select_from(AuditLog)).scalar()
        status, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(status, 1, out + err)
        self.assertTrue(err.startswith("judgectl: "), err)
        self.assertIn(f"({code})", err)
        self.s.expire_all()
        self.assertEqual(self.s.execute(select(func.count()).select_from(AuditLog)).scalar(), before)
        return err

    def create(self, *extra):
        return self.js("item", "create", "--kind", "assignment", "--title", "Блок-схема", "--group", "7Б",
                       "--due", "2026-11-12 23:59", "--grading", "points", "--max-points", "10", *extra)["id"]

    def hand_in(self, user, item_id, text="ответ", now=T0):
        works.submit_work(self.s, user, self.s.get(FeedItem, item_id), now, blobs=BlobStore(str(self.blob_dir)),
                          text=text)
        self.s.commit()

    # ---------------------------------------------------------------- items
    def test_create_with_audience_in_one_command_times_in_judge_timezone(self):
        body = self.tmp / "body.md"
        body.write_text("Нарисуй **блок-схему**", encoding="utf-8")
        d = self.js("item", "create", "--kind", "assignment", "--title", "  Блок-схема ", "--body-file", body,
                    "--group", "7Б", "--student", "student3", "--available-from", "2026-11-11 08:00",
                    "--due", "2026-11-12 23:59", "--after-due-submit", "no", "--grading", "points",
                    "--max-points", "5.5", "--answer", "mark", "--release", "immediate")
        self.assertEqual((d["title"], d["state"], d["body_md"], d["max_points"], d["answer_kind"],
                          d["grades_release"], d["after_due_submit"]),
                         ("Блок-схема", "draft", "Нарисуй **блок-схему**", "5.50", "mark", "immediate", "no"))
        self.assertEqual((d["available_from"], d["due_at"]), ("2026-11-11T01:00:00Z", "2026-11-12T16:59:00Z"))
        self.assertEqual(([x["name"] for x in d["audience"]["groups"]], [x["login"] for x in d["audience"]["users"]]),
                         (["7Б"], ["student3"]))
        actions = list(self.s.execute(select(AuditLog.action).where(AuditLog.object_type == "feed_item")
                                      .order_by(AuditLog.id)).scalars())
        self.assertEqual(actions, ["item.create", "item.audience"])
        # the text on stdin
        d = self.js("item", "create", "--kind", "assignment", "--title", "Из stdin", "--body-file", "-",
                    "--grading", "none", stdin="текст".encode())
        self.assertEqual(d["body_md"], "текст")
        out, _ = self.ok("item", "create", "--kind", "assignment", "--title", "Ещё", "--grading", "pass_fail")
        self.assertIn("draft (publish: judgectl item publish", out)

    def test_create_refusals_keep_nothing(self):
        self.refused("grading_invalid", "item", "create", "--kind", "assignment", "--title", "З")
        self.refused("max_points_invalid", "item", "create", "--kind", "assignment", "--title", "З",
                     "--grading", "points", "--max-points", "1e1")
        self.refused("deadline_invalid", "item", "create", "--kind", "assignment", "--title", "З", "--grading", "none",
                     "--available-from", "2026-11-12 10:00", "--due", "2026-11-12 10:00")
        self.refused("contest_required", "item", "create", "--kind", "contest", "--title", "К")
        # the audience fails after the item was made: the one transaction keeps neither
        self.refused("not_a_student", "item", "create", "--kind", "assignment", "--title", "З", "--grading", "none",
                     "--student", self.seed["admin"].login)
        self.assertEqual(self.s.execute(select(func.count()).select_from(FeedItem)).scalar(), 0)
        status, _out, err = self.run_cli("item", "create", "--kind", "assignment", "--title", "З", "--grading",
                                         "none", "--due", "завтра")
        self.assertEqual(status, 1)
        self.assertIn("--due 'завтра': not a time", err)
        status, _out, err = self.run_cli("item", "create", "--kind", "assignment", "--title", "З", "--grading",
                                         "none", "--group", "нет такой")
        self.assertEqual(status, 1)

    def test_contest_item_warns_and_gives_no_access(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        contest.access_mode = "groups"
        self.s.commit()
        code, out, err = self.run_cli("item", "create", "--kind", "contest", "--title", "Олимпиада", "--contest",
                                      contest.slug, "--group", "7Б")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: groups not attached to contest", err)

    def test_edit_publish_archive_delete(self):
        item_id = self.create()
        out = self.js("item", "edit", item_id, "--title", "Новое", "--due", "2026-11-13 12:00")
        self.assertEqual((out["changed"], out["item"]["due_at"]), (["title", "due_at"], "2026-11-13T05:00:00Z"))
        out = self.js("item", "edit", item_id, "--no-due", "--grading", "pass_fail")  # drops max_points itself
        self.assertEqual((sorted(out["changed"]), out["item"]["max_points"]), (["due_at", "grading", "max_points"],
                                                                              None))
        # the rule of a new scale is the service's (the admin API has the same): another scale with max_points and
        # points without it are refused
        self.refused("max_points_invalid", "item", "edit", item_id, "--grading", "none", "--max-points", "5")
        self.refused("max_points_invalid", "item", "edit", item_id, "--grading", "points")
        out = self.js("item", "edit", item_id, "--grading", "points", "--max-points", "4.5")
        self.assertEqual(out["item"]["max_points"], "4.50")
        self.assertIn("nothing changed", self.ok("item", "edit", item_id, "--title", "Новое")[0])
        status, _o, err = self.run_cli("item", "edit", item_id)
        self.assertEqual(status, 1)
        self.assertIn("nothing to change", err)
        self.refused("item_not_published", "item", "archive", item_id)
        self.assertIn("published", self.ok("item", "publish", item_id)[0])
        self.assertIn("already published", self.ok("item", "publish", item_id)[0])
        self.refused("item_not_draft", "item", "delete", item_id, "--yes")
        self.assertIn("archived", self.ok("item", "archive", item_id)[0])
        self.refused("item_archived", "item", "publish", item_id)
        self.assertIn("published again", self.ok("item", "unarchive", item_id)[0])
        # delete: only a draft, confirmed
        draft = self.create()
        status, _o, err = self.run_cli("item", "delete", draft)
        self.assertEqual(status, 1)
        self.assertIn("pass --yes", err)
        self.assertIn(f"item #{draft} deleted", self.ok("item", "delete", draft, "--yes")[0])
        self.refused("item_not_found", "item", "show", draft)
        # the scale is fixed once graded
        works.grade_work(self.s, self.actor, self.s.get(FeedItem, item_id), self.a, T0, score=1)
        self.s.commit()
        self.refused("grading_locked", "item", "edit", item_id, "--grading", "none")

    def test_list_show_and_hostile_text(self):
        first = self.create()
        second = self.js("item", "create", "--kind", "assignment", "--title", HOSTILE, "--grading", "none",
                         "--body-file", "-", stdin=HOSTILE.encode())["id"]
        self.ok("item", "publish", first)
        out, _ = self.ok("item", "list")
        self.assertEqual(controls(out), [])
        self.assertIn("\\x1b[2J", out)
        self.assertIn("\\u202e", out)
        rows = self.js("item", "list")
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(rows[0]["title"], " ".join(HOSTILE.split()))  # --json: the data as is
        self.assertEqual([r["id"] for r in self.js("item", "list", "--group", "7Б")], [first])
        self.assertEqual([r["id"] for r in self.js("item", "list", "--state", "draft")], [second])
        self.ok("item", "archive", first)
        self.assertEqual([r["id"] for r in self.js("item", "list")], [second])
        self.assertEqual([r["id"] for r in self.js("item", "list", "--all")], [second, first])
        out, err = self.ok("item", "show", second)
        self.assertEqual(controls(out + err), [])
        self.assertIn("    ответ\\x1b[2J", out)
        self.assertEqual(self.js("item", "show", second)["body_md"], HOSTILE)

    def test_audience_add_remove(self):
        item_id = self.create()
        out = self.js("item", "audience", "add", item_id, "--student", "student3")
        self.assertEqual(out["added"], ["student student3"])
        self.ok("item", "publish", item_id)
        out, _ = self.ok("item", "audience", "remove", item_id, "--group", "7Б")
        self.assertIn("removed group 7Б", out)
        self.refused("no_audience", "item", "audience", "remove", item_id, "--student", "student3")
        status, _o, err = self.run_cli("item", "audience", "add", item_id)
        self.assertEqual(status, 1)
        self.assertIn("at least one --group or --student", err)

    def test_extensions(self):
        item_id = self.create()
        out = self.js("item", "extension", "add", item_id, "--user", "student1", "--due", "2026-11-14 23:59",
                      "--reason", "болел")
        self.assertEqual((out["due_at"], out["replaced"]), ("2026-11-14T16:59:00Z", None))
        out, _ = self.ok("item", "extension", "add", item_id, "--user", "student1", "--due", "2026-11-15 23:59")
        self.assertIn("was 2026-11-14 23:59", out)
        self.refused("extension_not_later", "item", "extension", "add", item_id, "--group", "7Б",
                     "--due", "2026-11-11 00:00")
        rows = self.js("item", "extension", "list", item_id)
        self.assertEqual([(r["user"], r["reason"]) for r in rows], [("student1", None)])
        # an extension of a contest problem is not removed here
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        cp = self.seed["cp"]
        cp.due_at = T0 + timedelta(days=1)
        self.s.commit()
        row, _ = deadlines.add_extension(self.s, self.actor, contest, cp.label, T0 + timedelta(days=2), T0,
                                         user=self.a)
        self.s.commit()
        status, _o, err = self.run_cli("item", "extension", "remove", row.id)
        self.assertEqual(status, 1)
        self.assertIn("judgectl extension remove", err)
        out, _ = self.ok("item", "extension", "remove", rows[0]["id"])
        self.assertIn("extension of student student1 removed", out)
        status, _o, err = self.run_cli("item", "extension", "remove", rows[0]["id"])
        self.assertEqual(status, 1)
        no_due = self.js("item", "create", "--kind", "assignment", "--title", "Без срока", "--grading", "none")["id"]
        self.refused("no_deadline", "item", "extension", "add", no_due, "--user", "student1", "--due", "2026-11-14")

    # ---------------------------------------------------------------- works
    def test_work_list_show_grade_return_release(self):
        item_id = self.create()
        self.ok("item", "audience", "add", item_id, "--student", "student3")
        self.ok("item", "publish", item_id)
        self.hand_in(self.a, item_id, text=HOSTILE)
        self.hand_in(self.b, item_id, now=datetime(2026, 11, 13, tzinfo=timezone.utc))  # late
        self.clock = datetime(2026, 11, 14, tzinfo=timezone.utc)
        rows = self.js("work", "list", "--item", item_id)
        self.assertEqual([(r["login"], r["status"], r["late"], r["missing"]) for r in rows],
                         [("student3", "assigned", False, True), ("student2", "submitted", True, False),
                          ("student1", "submitted", False, False)])
        self.assertEqual([r["login"] for r in self.js("work", "list", "--item", item_id, "--missing")], ["student3"])
        self.assertEqual([r["login"] for r in self.js("work", "list", "--late")], ["student2"])
        self.assertEqual([r["login"] for r in self.js("work", "list", "--group", "7Б", "--status", "submitted")],
                         ["student2", "student1"])
        self.refused("missing_needs_item", "work", "list", "--missing")
        out, _ = self.ok("work", "list", "--item", item_id)
        self.assertIn("assigned", out)
        # work show: the answer escaped, the card of an addressee without work, strangers refused
        out, _ = self.ok("work", "show", item_id, "student1")
        self.assertEqual(controls(out), [])
        self.assertIn("| ответ\\x1b[2J\\x1b]0;pwned\\x07 abc\\x0dXYZ \\u202etxt.exe\\x9b", out)
        card = self.js("work", "show", item_id, "student1")
        self.assertEqual(card["events"][0]["text"], HOSTILE)
        card = self.js("work", "show", item_id, "student3")
        self.assertEqual((card["status"], card["events"], card["missing"]), ("assigned", [], True))
        other = self.student(4)
        self.s.commit()
        status, _o, err = self.run_cli("work", "show", item_id, other.login)
        self.assertEqual(status, 1)
        self.assertIn("(work_not_found)", err)
        # grades
        self.refused("score_invalid", "work", "grade", item_id, "student1", "--score", "1e1")
        self.refused("score_invalid", "work", "grade", item_id, "student1", "--score", "4.555")
        self.refused("score_out_of_range", "work", "grade", item_id, "student1", "--score", "11")
        self.refused("grade_kind_mismatch", "work", "grade", item_id, "student1", "--pass")
        self.refused("not_addressed", "work", "grade", item_id, other.login, "--score", "1")
        out, err = self.ok("work", "grade", item_id, "student1", "--score", "8.5", "--comment", "хорошо")
        self.assertIn("graded 8.5", out)
        self.assertIn(f"judgectl work release {item_id} student1", err)
        result = self.js("work", "grade", item_id, "student3", "--score", "0", "--comment", "не сдано", "--release")
        self.assertEqual((result["status"], result["released"]), ("graded", True))
        self.refused("comment_required", "work", "return", item_id, "student2", "--comment", "  ")
        self.assertIn("returned", self.ok("work", "return", item_id, "student2", "--comment", "доделай")[0])
        self.refused("already_returned", "work", "return", item_id, "student2", "--comment", "ещё")
        results = self.js("work", "release", item_id)
        self.assertEqual({r["login"]: r["result"] for r in results},
                         {"student1": "released", "student2": "nothing_to_release", "student3": "already_released"})
        out, err = self.ok("work", "release", item_id, "student1", "student4")
        self.assertIn("already_released", out)
        self.assertIn("released 0 of 2", err)
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentReview)
                                        .where(AssignmentReview.reviewer_id.is_(None))).scalar(), 5)
        # grading none: --accept; immediate: nothing to release
        quick = self.js("item", "create", "--kind", "assignment", "--title", "Быстро", "--grading", "none",
                        "--group", "7Б")["id"]
        self.ok("item", "publish", quick)
        out, _ = self.ok("work", "grade", quick, "student1", "--accept")
        self.assertIn("graded accepted, seen at once", out)
        self.refused("release_not_needed", "work", "release", quick)

    def test_group_archive_names_its_feed_items(self):
        item_id = self.js("item", "create", "--kind", "assignment", "--title", "Тема\x1b[31m", "--grading", "none",
                          "--group", "7Б")["id"]
        out, err = self.ok("group", "archive", "7Б")
        self.assertIn("group 7Б: archived", out)
        self.assertIn(f"#{item_id} assignment draft Тема\\x1b[31m", err)
        self.assertEqual(controls(err), [])
