"""judgectl comment ..., item --comments and dev_seed --comment (docs/JUDGECTL.md, CLASSROOM.md §9, D-076) on a real
PostgreSQL: the inbox of the console (awaiting reply, never unread), whole threads, writing and deleting as the
console, refusals with their code and nothing written, --json as it is, and students' text printed without control
characters.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import contextlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from unittest import mock

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 10:00 in Novosibirsk
DUE = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
HOSTILE = "вопрос\x1b[2J\x1b]0;pwned\x07 abc\rXYZ ‮txt.exe\x9b\n<script>alert(1)</script>"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import db, dev_seed
    from app import groups as g
    from app.auth import audit
    from app.classroom import comments, items
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import AuditLog, Comment, Contest, FeedItem, Role, User
    from app.settings import Settings


def controls(text):
    """Raw control characters in judgectl's output (none may get through)."""
    return [hex(ord(ch)) for ch in text if ch not in "\n" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                                or 0x202A <= ord(ch) <= 0x202E)]


@requires_pg
class JudgectlCommentTests(PgCase):
    def setUp(self):
        super().setUp()
        self.actor = audit.Actor(user_id=self.seed["admin"].id)
        self.a, self.b, self.c = self.student(1), self.student(2), self.student(3)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], T0)
        self.s.commit()
        self.clock = T0

    # ---------------------------------------------------------------- helpers
    def run_cli(self, *argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        settings = Settings(timezone="Asia/Novosibirsk")
        ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:judge"),
                      out=out, err=err, stdin=io.BytesIO(stdin), interactive=lambda: False, clock=lambda: self.clock)
        code = main([str(a) for a in argv], ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, err)
        self.assertEqual(controls(out + err), [])
        return out, err

    def js(self, *argv, **kw):
        """--json: the data as it is (students' control characters included, so no controls() check)."""
        code, out, err = self.run_cli(*argv, "--json", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def refused(self, code, *argv, **kw):
        """Exit 1, `judgectl: ... (<code>)` on stderr, nothing written (audit_log and comments as witnesses)."""
        def counts():
            self.s.expire_all()
            return [self.s.execute(select(func.count()).select_from(t)).scalar() for t in (AuditLog, Comment)]

        before = counts()
        status, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(status, 1, out + err)
        self.assertTrue(err.startswith("judgectl: "), err)
        if code:
            self.assertIn(f"({code})", err)
        self.assertEqual(counts(), before)
        return err

    def assignment(self, users=(), groups=None, **fields):
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Схема", body_md="Нарисуй",
                                 grading="points", max_points=10, due_at=DUE, comments_enabled=True, **fields)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group] if groups is None else groups,
                           users=list(users))
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item.id

    def contest_item(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        item = items.create_item(self.s, self.actor, T0, kind="contest", title="Контест", contest=contest)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item.id

    def student_says(self, user, item_id, body, private=False):
        post = comments.post_private if private else comments.post_shared
        row = post(self.s, user, self.s.get(FeedItem, item_id), T0, body)
        self.s.commit()
        return row.id

    # ---------------------------------------------------------------- list and show
    def test_inbox_and_threads_print_students_text_escaped_and_json_as_is(self):
        item = self.assignment()
        shared = self.student_says(self.a, item, HOSTILE)
        private = self.student_says(self.b, item, HOSTILE, private=True)
        out, _ = self.ok("comment", "list")
        self.assertIn("AWAITING", out)
        self.assertNotIn("UNREAD", out)
        rows = self.js("comment", "list")
        self.assertEqual([(r["kind"], r["awaiting_reply"], r["unread"]) for r in rows],
                         [("private", True, None), ("shared", True, None)])  # the console has no read marks
        self.assertEqual(rows[0]["student"]["login"], self.b.login)

        out, _ = self.ok("comment", "show", item)
        self.assertIn("shared thread", out)
        self.assertIn(f"#{shared} ", out)
        self.assertIn("    | вопрос\\x1b[2J\\x1b]0;pwned\\x07 abc\\x0dXYZ \\u202etxt.exe\\x9b", out)
        self.assertIn("    | <script>alert(1)</script>", out)
        self.assertEqual(self.js("comment", "show", item)["comments"][0]["body"], HOSTILE)
        out, _ = self.ok("comment", "show", item, "--student", self.b.login)
        self.assertIn(f"private thread of {self.b.login}", out)
        view = self.js("comment", "show", item, "--student", self.b.login)
        self.assertEqual(([c["id"] for c in view["comments"]], view["comments"][0]["body"], view["unread"]),
                         ([private], HOSTILE, None))

    def test_show_reads_every_page_and_an_empty_thread(self):
        item = self.assignment()
        out, _ = self.ok("comment", "show", item)
        self.assertIn("no comments yet", out)
        for n in range(comments.PAGE_MAX + 3):  # teachers have no rate: more than one page
            comments.teacher_post(self.s, self.actor, self.s.get(FeedItem, item), T0, f"объявление {n}")
        self.s.commit()
        self.assertEqual(len(self.js("comment", "show", item)["comments"]), comments.PAGE_MAX + 3)

    def test_inbox_filters(self):
        first, second = self.assignment(), self.assignment(groups=[], users=[self.c])
        self.student_says(self.a, first, "общий вопрос")
        self.student_says(self.c, second, "личный вопрос", private=True)
        self.assertEqual([r["item"]["id"] for r in self.js("comment", "list", "--item", second)], [second])
        self.assertEqual([r["item"]["id"] for r in self.js("comment", "list", "--group", "7Б")], [first])
        self.ok("comment", "post", second, "--student", self.c.login, "--text", "ответ")
        self.assertEqual([r["item"]["id"] for r in self.js("comment", "list", "--awaiting")], [first])
        self.assertEqual([r["item"]["id"] for r in self.js("comment", "list", "--answered")], [second])
        rows = self.js("comment", "list", "--last", 1)
        self.assertEqual(len(rows), 1)
        _out, err = self.ok("comment", "list", "--last", 1)
        self.assertIn("--last N shows more", err)

    # ---------------------------------------------------------------- writing and deleting
    def test_post_as_the_console_shared_and_private_from_stdin(self):
        item = self.assignment()
        self.student_says(self.a, item, "можно ли карандашом?", private=True)
        card = self.js("comment", "post", item, "--text", "Всем: срок продлён")
        self.assertEqual((card["kind"], card["author"]["console"], card["body"]), ("shared", True,
                                                                                    "Всем: срок продлён"))
        out, _ = self.ok("comment", "post", item, "--student", self.a.login, "--file", "-",
                         stdin="Можно.\n\x1b[31mкрасным\n".encode())
        self.assertIn(f"in the private thread of item #{item} ({self.a.login})", out)
        view = self.js("comment", "show", item, "--student", self.a.login)
        self.assertEqual(view["comments"][-1]["body"], "Можно.\n\x1b[31mкрасным\n")
        self.assertEqual(self.js("comment", "list", "--item", item, "--awaiting"), [])
        self.s.expire_all()
        logged = self.s.execute(select(AuditLog).where(AuditLog.action == "comment.post")
                                .order_by(AuditLog.id)).scalars().all()
        self.assertEqual([(r.actor_cli, r.details["thread"]) for r in logged],
                         [("cli:judge", "shared"), ("cli:judge", "private")])
        self.assertTrue(all("Можно" not in json.dumps(r.details, ensure_ascii=False) for r in logged))

    def test_delete_with_a_reason_is_marked_and_once_only(self):
        item = self.assignment()
        cid = self.student_says(self.a, item, "спойлер: ответ 42")
        out, _ = self.ok("comment", "delete", cid, "--reason", "  готовый   ответ ")
        self.assertIn(f"comment #{cid} of {self.a.login}", out)
        out, _ = self.ok("comment", "show", item)
        self.assertIn("[deleted ", out)
        self.assertIn("by console: готовый ответ]", out)
        self.assertIn("    | спойлер: ответ 42", out)  # teachers still see the text
        self.refused("already_deleted", "comment", "delete", cid)
        other = self.student_says(self.b, item, "ещё")
        self.refused("reason_too_long", "comment", "delete", other, "--reason", "x" * 201)
        self.refused("comment_not_found", "comment", "delete", 999999)
        self.assertEqual(self.js("comment", "list", "--item", item)[0]["deleted"], 1)

    def test_refusals_carry_their_code(self):
        item = self.assignment()
        self.refused("not_addressed", "comment", "post", item, "--student", self.c.login, "--text", "привет")
        self.refused("thread_not_found", "comment", "show", item, "--student", self.c.login)
        self.refused("comment_required", "comment", "post", item, "--text", "  ​ ")
        self.refused("comment_too_long", "comment", "post", item, "--text", "я" * 4001)
        self.refused("comment_invalid", "comment", "post", item, "--file", "-", stdin=b"a\x00b")
        err = self.refused(None, "comment", "post", item, "--file", "-", stdin=b"\xff\xfe")
        self.assertIn("not UTF-8", err)
        self.refused("not_an_assignment", "comment", "post", self.contest_item(), "--student", self.a.login,
                     "--text", "привет")
        draft = items.create_item(self.s, self.actor, T0, kind="assignment", title="Черновик", grading="none")
        self.s.commit()
        self.refused("not_published", "comment", "post", draft.id, "--text", "привет")
        self.refused("item_not_found", "comment", "show", 999999)
        self.refused(None, "comment", "show", item, "--student", "nobody")

    # ---------------------------------------------------------------- item --comments
    def test_item_comments_on_off(self):
        made = self.js("item", "create", "--kind", "assignment", "--title", "Т", "--grading", "none",
                       "--comments", "on")
        self.assertTrue(made["comments_enabled"])
        changed = self.js("item", "edit", made["id"], "--comments", "off")
        self.assertEqual((changed["changed"], changed["item"]["comments_enabled"]), (["comments_enabled"], False))
        out, _ = self.ok("item", "show", made["id"])
        self.assertIn("comments of students: off", out)
        contest = self.contest_item()
        self.refused("item_invalid", "item", "edit", contest, "--comments", "on")

    # ---------------------------------------------------------------- dev_seed --comment
    def test_dev_seed_writes_as_a_student_through_the_rules(self):
        blob_dir = tempfile.TemporaryDirectory()
        self.addCleanup(blob_dir.cleanup)

        def seed(*argv, dev="1", rate="2"):
            out, err = io.StringIO(), io.StringIO()
            env = {"JUDGE_DEV": dev, "JUDGE_COMMENT_RATE_PER_MIN": rate, "JUDGE_BLOB_DIR": blob_dir.name}
            with mock.patch.dict(os.environ, env), mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dev_seed.main([str(a) for a in argv])
            return code, out.getvalue(), err.getvalue()

        dev = User(login="dev-student-1", password_hash="!", display_name="Dev 1", role=Role.student)
        self.s.add(dev)
        self.s.flush()
        item = self.assignment(groups=[], users=[dev])
        items.update_item(self.s, self.actor, self.s.get(FeedItem, item), T0, comments_enabled=False)
        contest = self.contest_item()
        items.set_audience(self.s, self.actor, self.s.get(FeedItem, contest), T0, groups=[self.group], users=[dev])
        self.s.commit()

        self.assertEqual(seed("--item", item, "--comment", "x", dev="0")[0], 2)
        code, _out, err = seed("--item", item, "--comment", "можно вопрос?")
        self.assertEqual(code, 1)
        self.assertIn("refused: comments_disabled", err)
        code, _out, err = seed("--item", contest, "--comment", "как решать B?")
        self.assertIn("refused: comments_teacher_only", err)
        items.update_item(self.s, self.actor, self.s.get(FeedItem, item), T0, comments_enabled=True)
        self.s.commit()
        code, out, err = seed("--item", item, "--comment", HOSTILE)
        self.assertEqual(code, 0, err)
        made = json.loads(out)
        self.assertEqual((made["kind"], made["login"]), ("shared", "dev-student-1"))
        self.s.expire_all()
        self.assertEqual(self.s.get(Comment, made["comment_id"]).body, HOSTILE)
        code, out, err = seed("--item", item, "--comment", "лично", "--private")
        self.assertEqual((code, json.loads(out)["kind"]), (0, "private"), err)
        code, _out, err = seed("--item", item, "--comment", "третий за минуту")
        self.assertEqual(code, 1)
        self.assertIn("refused: rate_limited", err)
        code, _out, err = seed("--item", item, "--comment", "чужая ветка", "--private", "--user", 2)
        self.assertIn("refused: no_access", err)  # dev-student-2 is not addressed
        self.assertEqual(self.s.execute(select(func.count()).select_from(Comment)).scalar(), 2)
