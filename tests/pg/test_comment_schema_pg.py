"""The schema of comments (D-072, migration e73284028c20) on a real PostgreSQL: every CHECK and every partial unique
index refuses a row by its name and accepts one; two shared threads of one item and two private threads of one pair
are impossible; every new foreign key says its ON DELETE in the catalog; what the body CHECK lets through, on a set
of edge strings (the rules of 4v.2 are stricter: tests/test_comment_rules_units.py compares them). The migration
itself: tests/pg/test_migrations_pg.py.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
from datetime import datetime, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import text
    from sqlalchemy.exc import DataError, IntegrityError, StatementError

    from app.auth import audit, users as accounts
    from app.classroom import comment_rules
    from app.models import COMMENT_MAX_CHARS, User

ASSIGNMENT = dict(kind="assignment", state="published", title="Блок-схема", body_md="", grading="none",
                  answer_kind="mark", grades_release="manual", published_at="2026-11-09 10:00+00")

# Edge bodies and whether the CHECKs of `comments` take them. The CHECK refuses only what is surely not a comment
# (empty, only ASCII spaces / tabs / line breaks, over 4000 characters); the rules refuse more (any Unicode
# whitespace, NUL, lone surrogates): everything the rules let through must pass these CHECKs.
NBSP = " "
BODY_CASES = [  # (label, body, accepted by the CHECKs)
    ("empty", "", False),
    ("one space", " ", False),
    ("ascii whitespace only", " \t\r\n \n", False),
    ("line breaks only", "\n\n\n", False),
    ("nbsp only", NBSP * 3, True),            # the CHECK lets it through, check_body refuses it
    ("ideographic space only", "　", True),
    ("zero width space only", "​", True),
    ("one letter", "а", True),
    ("text with line breaks and tabs", "строка 1\n\tстрока 2\r\nконец", True),
    ("text in nbsp", f"{NBSP}да{NBSP}", True),
    ("leading and trailing whitespace", "\n  вопрос  \t\n", True),
    ("markdown and html as data", "<script>alert(1)</script> `a < b`", True),
    ("control characters", "\x1b[31mкрасный\x07", True),
    ("4000 characters", "я" * 4000, True),
    ("4001 characters", "я" * 4001, False),
    ("4000 astral characters", "😀" * 4000, True),   # char_length counts code points, like len() in Python
    ("4001 astral characters", "😀" * 4001, False),
    ("4000 with combining marks", "е́" * 2000, True),  # 4000 code points, 2000 letters on screen
]


@requires_pg
class CommentSchemaTests(PgCase):
    def setUp(self):
        super().setUp()
        self.pupil = self.student(1).id
        self.other = self.student(2).id
        self.item = self.insert("feed_items", **ASSIGNMENT)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def insert(self, table, returning="id", **values):
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        return self.s.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params}) RETURNING {returning}"),
                              values).scalar_one()

    def refused(self, constraint, fn, *args, **kw):
        """The statement fails on exactly this constraint, and the session goes on (a savepoint)."""
        with self.assertRaises(IntegrityError) as caught:
            with self.s.begin_nested():
                fn(*args, **kw)
        self.assertIn(constraint, str(caught.exception.orig))

    def accepted(self, fn, *args, **kw):
        with self.s.begin_nested():
            return fn(*args, **kw)

    def thread(self, item=None, student=None):
        return self.insert("comment_threads", item_id=item or self.item, student_id=student)

    def comment(self, thread, body="вопрос", **kw):
        return self.insert("comments", thread_id=thread, body=body, **kw)

    # ---------------------------------------------------------------- feed_items.comments_enabled
    def test_comments_enabled_defaults_to_false(self):
        enabled = self.s.execute(text("SELECT comments_enabled FROM feed_items WHERE id = :i"), {"i": self.item})
        self.assertIs(enabled.scalar_one(), False)
        default = self.s.execute(text(
            "SELECT column_default, is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'feed_items' AND column_name = 'comments_enabled'")).one()
        self.assertEqual(tuple(default), ("false", "NO"))
        self.refused("comments_enabled", self.insert, "feed_items", **ASSIGNMENT, comments_enabled=None)

    # ---------------------------------------------------------------- threads
    def test_one_shared_thread_per_item(self):
        self.accepted(self.thread)
        self.refused("uq_comment_threads_shared", self.thread)
        other_item = self.insert("feed_items", **ASSIGNMENT)
        self.accepted(self.thread, other_item)

    def test_one_private_thread_per_item_and_student(self):
        self.accepted(self.thread, student=self.pupil)
        self.refused("uq_comment_threads_private", self.thread, student=self.pupil)
        self.accepted(self.thread, student=self.other)        # another student, the same item
        self.accepted(self.thread)                            # the shared thread is not counted with them
        self.accepted(self.thread, self.insert("feed_items", **ASSIGNMENT), self.pupil)  # another item

    # ---------------------------------------------------------------- comments
    def test_comment_checks(self):
        thread = self.thread()
        teacher = self.insert("users", login="teacher", password_hash="x", display_name="T", role="admin",
                              is_active=True)
        cases = [  # (constraint, refused row, accepted row)
            ("ck_comments_body_length", dict(body="я" * (COMMENT_MAX_CHARS + 1)), dict(body="я" * COMMENT_MAX_CHARS)),
            ("ck_comments_body_not_blank", dict(body=" \n\t"), dict(body=" ?\n")),
            ("ck_comments_deleted_fields", dict(deleted_by=teacher), dict(deleted_by=teacher,
                                                                          deleted_at="2026-11-09 11:00+00")),
            ("ck_comments_deleted_fields", dict(delete_reason="спам"),
             dict(delete_reason="спам", deleted_at="2026-11-09 11:00+00")),
        ]
        for constraint, bad, good in cases:
            with self.subTest(constraint=constraint, bad=bad):
                self.refused(constraint, self.comment, thread, **bad)
                self.accepted(self.comment, thread, **good)
        # deleted from the console: deleted_at without deleted_by; written from the console: no author
        self.accepted(self.comment, thread, deleted_at="2026-11-09 11:00+00", delete_reason="дубль")
        self.accepted(self.comment, thread, author_id=None)
        self.accepted(self.comment, thread, author_id=self.pupil)

    def test_body_edge_strings(self):
        """What the CHECKs take, by name of the case; the rules are compared with this list in stage 3/4."""
        thread = self.thread()
        for label, body, ok in BODY_CASES:
            with self.subTest(label):
                if ok:
                    self.assertEqual(self.accepted(self.comment, thread, body=body, returning="body"), body)
                else:
                    self.refused("ck_comments_body_", self.comment, thread, body=body)

    def test_whatever_check_body_accepts_the_database_takes(self):
        """The rules are stricter than the CHECKs (D-074): on every edge string, accepted by check_body => accepted
        by the database; and exactly these are refused by the rules but would pass the CHECKs."""
        thread = self.thread()
        extra = [("only control characters", "\x1b\x07"), ("only format characters", "\ufeff\u200e\u2066"),
                 ("only line and paragraph separators", "\u2028\u2029"), ("3999 spaces and a letter", " " * 3999 + "x"),
                 ("5000 line breaks", "\n" * 5000), ("4000 nbsp", "\u00a0" * 4000)]
        stricter = []
        for label, body in [(label, body) for label, body, _ok in BODY_CASES] + extra:
            with self.subTest(label):
                ruled = comment_rules.check_body(body)
                try:
                    with self.s.begin_nested():
                        self.comment(thread, body=body)
                    taken = True
                except IntegrityError:
                    taken = False
                if ruled:
                    self.assertTrue(taken, f"check_body accepts {label!r}, the database refuses it")
                elif taken:
                    stricter.append((label, ruled.reason))
        self.assertEqual(sorted(stricter), sorted([
            ("nbsp only", "comment_required"), ("ideographic space only", "comment_required"),
            ("zero width space only", "comment_required"), ("only control characters", "comment_required"),
            ("only format characters", "comment_required"), ("only line and paragraph separators", "comment_required"),
            ("4000 nbsp", "comment_required"),
        ]))

    def test_bodies_the_database_cannot_hold_at_all(self):
        """NUL and lone surrogates never reach a CHECK: the driver or the server refuses them (a 500 if the rules
        let them through - check_body refuses them as comment_invalid)."""
        thread = self.thread()
        for label, body in (("nul", "a\x00b"), ("lone surrogate", "a\ud800b")):
            with self.subTest(label):
                self.assertEqual(comment_rules.check_body(body).reason, "comment_invalid")
                with self.assertRaises((DataError, StatementError, UnicodeEncodeError, ValueError)):
                    with self.s.begin_nested():
                        self.comment(thread, body=body)

    # ---------------------------------------------------------------- read marks
    def test_one_read_mark_per_user_and_thread(self):
        thread = self.thread()
        first = self.comment(thread)
        mark = lambda **kw: self.insert("thread_reads", returning="user_id", **{  # noqa: E731
            "user_id": self.pupil, "thread_id": thread, "last_read_comment_id": first, **kw})
        self.accepted(mark)
        self.refused("pk_thread_reads", mark)
        self.accepted(mark, user_id=self.other)
        self.refused("fk_thread_reads_last_read_comment_id_comments", mark, user_id=self.student(3).id,
                     last_read_comment_id=first + 1000)

    # ---------------------------------------------------------------- foreign keys
    def test_everything_restricts(self):
        """Each restricted row is pointed at by exactly one comment row, so the refusal names that foreign key."""
        author, reader, deleter = self.student(3).id, self.student(4).id, self.student(5).id
        private = self.thread(student=self.pupil)
        shared_item = self.insert("feed_items", **ASSIGNMENT)
        shared = self.thread(shared_item)
        c1 = self.comment(shared, author_id=author)
        c2 = self.comment(shared, deleted_at="2026-11-09 11:00+00", deleted_by=deleter)
        self.comment(private)
        self.insert("thread_reads", returning="user_id", user_id=reader, thread_id=shared, last_read_comment_id=c1)
        self.s.commit()
        restricted = [
            ("fk_comment_threads_item_id_feed_items", f"DELETE FROM feed_items WHERE id = {self.item}"),
            ("fk_comment_threads_student_id_users", f"DELETE FROM users WHERE id = {self.pupil}"),
            ("fk_comments_author_id_users", f"DELETE FROM users WHERE id = {author}"),
            ("fk_comments_deleted_by_users", f"DELETE FROM users WHERE id = {deleter}"),
            ("fk_thread_reads_user_id_users", f"DELETE FROM users WHERE id = {reader}"),
            ("fk_thread_reads_last_read_comment_id_comments", f"DELETE FROM comments WHERE id = {c1}"),
            ("fk_comments_thread_id_comment_threads", f"DELETE FROM comment_threads WHERE id = {private}"),
        ]
        for constraint, sql in restricted:
            with self.subTest(constraint=constraint):
                self.refused(constraint, self.s.execute, text(sql))
        # a thread is pointed at by its comments and the read marks: without the comments, the marks restrict
        savepoint = self.s.begin_nested()
        self.s.execute(text("DELETE FROM thread_reads"))
        self.s.execute(text(f"DELETE FROM comments WHERE id = {c2}"))
        self.refused("fk_comments_thread_id_comment_threads", self.s.execute,
                     text(f"DELETE FROM comment_threads WHERE id = {shared}"))
        savepoint.rollback()

    def test_the_new_foreign_keys_say_their_on_delete(self):
        """Every foreign key of the comment tables restricts (none cascades): the list of the report of stage 2."""
        rows = self.s.execute(text("""
            SELECT c.conname, c.confdeltype
              FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
             WHERE n.nspname = current_schema() AND c.contype = 'f'
               AND c.conrelid::regclass::text IN ('comment_threads', 'comments', 'thread_reads')""")).all()
        self.assertEqual(dict(rows), {
            "fk_comment_threads_item_id_feed_items": "r",
            "fk_comment_threads_student_id_users": "r",
            "fk_comments_thread_id_comment_threads": "r",
            "fk_comments_author_id_users": "r",
            "fk_comments_deleted_by_users": "r",
            "fk_thread_reads_user_id_users": "r",
            "fk_thread_reads_thread_id_comment_threads": "r",
            "fk_thread_reads_last_read_comment_id_comments": "r",
        })  # r = RESTRICT (an explicit rule, unlike the default NO ACTION 'a')

    # ---------------------------------------------------------------- users.traces and user deletion
    def test_user_traces_know_comments_and_erase_read_marks(self):
        """Three new kinds of traces (a written comment, a deleted one, a private thread of the student); a read mark
        is no trace: deleting the user erases it."""
        now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        actor = audit.Actor(cli="cli:test")
        writer, deleter, owner, reader = (self.student(n) for n in (3, 4, 5, 6))
        private = self.thread(student=owner.id)                 # written from the console: the owner's only trace
        console = self.comment(private)
        shared = self.thread()
        own = self.comment(shared, author_id=writer.id)
        self.comment(shared, deleted_at="2026-11-09 11:00+00", deleted_by=deleter.id)
        self.insert("thread_reads", returning="user_id", user_id=reader.id, thread_id=shared, last_read_comment_id=own)
        self.insert("thread_reads", returning="user_id", user_id=reader.id, thread_id=private,
                    last_read_comment_id=console)
        self.s.commit()

        self.assertEqual(accounts.traces(self.s, writer), {"comments written": 1})
        self.assertEqual(accounts.traces(self.s, deleter), {"comments deleted": 1})
        self.assertEqual(accounts.traces(self.s, owner), {"private comment threads": 1})
        self.assertEqual(accounts.traces(self.s, reader), {})
        for user in (writer, deleter, owner):
            with self.subTest(user=user.login), self.assertRaises(accounts.UserError) as caught:
                accounts.delete_user(self.s, actor, user, now)
            self.assertEqual(caught.exception.code, "has_traces")
            self.s.rollback()

        reader_id = reader.id
        details = accounts.delete_user(self.s, actor, reader, now)
        self.s.commit()
        self.assertEqual(details["thread_reads_deleted"], 2)
        self.assertIsNone(self.s.get(User, reader_id))
        left = self.s.execute(text("SELECT count(*) FROM thread_reads WHERE user_id = :u"), {"u": reader_id})
        self.assertEqual(left.scalar_one(), 0)
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM comments")).scalar_one(), 3, "comments stay")
