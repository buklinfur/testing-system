"""The schema of attachments and links (D-078, D-079, migration 32cd8723a2bc) on a real PostgreSQL: every new CHECK
and partial unique index refuses a row by its name and accepts one; a student's file named answer.md does not meet
the text answer; A.py and a.py in one hand-in are refused by the database; every new foreign key says its ON DELETE
in the catalog; items may now be answered with files; every foreign key to users is a trace or erased with the user.
The migration itself: tests/pg/test_migrations_pg.py.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
from datetime import datetime, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.auth import audit, users as accounts
    from app.models import (ATTACHMENT_NAME_MAX_CHARS, LINK_TITLE_MAX_CHARS, LINK_URL_MAX_CHARS, Base, User)

ASSIGNMENT = dict(kind="assignment", state="published", title="Проект", body_md="", grading="none",
                  answer_kind="files", grades_release="manual", published_at="2026-11-11 10:00+00")
SHA_A, SHA_B = "e" * 64, "f" * 64
URL_OK = "https://scratch.mit.edu/projects/1"


@requires_pg
class AttachmentSchemaTests(PgCase):
    def setUp(self):
        super().setUp()
        self.pupil = self.student(1).id
        for sha in (SHA_A, SHA_B):
            self.s.execute(text("INSERT INTO blobs (sha256, size_bytes) VALUES (:s, 3)"), {"s": sha})
        self.item = self.insert("feed_items", **ASSIGNMENT)
        self.work = self.insert("assignment_works", item_id=self.item, user_id=self.pupil, status="submitted",
                                status_at="2026-11-11 11:00+00")
        self.sub = self.hand_in()
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

    def hand_in(self):
        return self.insert("assignment_submissions", work_id=self.work)

    def file(self, sub=None, path="main.py", kind="file", size=3, sha=SHA_A, position=0):
        return self.insert("assignment_submission_files", submission_id=sub or self.sub, kind=kind, path=path,
                           blob_sha256=sha, size=size, position=position)

    def link(self, sub=None, url=URL_OK, title=None, position=0):
        return self.insert("assignment_submission_links", submission_id=sub or self.sub, url=url, title=title,
                           position=position)

    def attachment(self, item=None, **values):
        values.setdefault("kind", "file")
        values.setdefault("position", 0)
        if values["kind"] == "file":
            values = {"name": "схема.pdf", "blob_sha256": SHA_A, "size": 3, **values}
        else:
            values = {"url": URL_OK, **values}
        return self.insert("attachments", item_id=item or self.item, **values)

    def check(self, constraint, fn, bad: dict, good: dict):
        with self.subTest(constraint=constraint, bad=bad):
            self.refused(constraint, fn, **bad)
            self.accepted(fn, **good)

    # ---------------------------------------------------------------- feed_items
    def test_items_may_be_answered_with_files_now(self):
        """The CHECK answer_kind_before_4v3 is gone: every label of answer_kind is accepted."""
        for kind in ("mark", "text", "files", "text_or_files"):
            with self.subTest(kind=kind):
                self.accepted(self.insert, "feed_items", **dict(ASSIGNMENT, answer_kind=kind))
        names = self.s.execute(text("SELECT conname FROM pg_constraint WHERE conrelid = 'feed_items'::regclass")
                               ).scalars().all()
        self.assertNotIn("ck_feed_items_answer_kind_before_4v3", names)

    # ---------------------------------------------------------------- assignment_submission_files
    def test_kind_has_no_default(self):
        """Every row says its kind: the default 'text' of the migration is dropped right after it filled the old
        rows."""
        default = self.s.execute(text(
            "SELECT column_default, is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'assignment_submission_files' AND column_name = 'kind'")).one()
        self.assertEqual(tuple(default), (None, "NO"))
        self.refused("kind", self.insert, "assignment_submission_files", submission_id=self.sub, path="a.py",
                     blob_sha256=SHA_A, size=3)

    def test_submission_file_checks(self):
        self.check("ck_assignment_submission_files_text_is_answer_md", self.file,
                   dict(kind="text", path="answer.txt"), dict(kind="text", path="answer.md"))
        self.check("ck_assignment_submission_files_file_not_empty", self.file,
                   dict(path="empty.txt", size=0), dict(path="one.txt", size=1))
        self.check("ck_assignment_submission_files_path_not_blank", self.file, dict(path="  "), dict(path="b.py"))
        self.check("ck_assignment_submission_files_non_negative", self.file,
                   dict(path="c.py", position=-1), dict(path="c.py", position=1))

    def test_one_text_answer_per_hand_in(self):
        self.accepted(self.file, kind="text", path="answer.md")
        self.refused("uq_assignment_submission_files_text", self.file, kind="text", path="answer.md", sha=SHA_B)
        self.accepted(self.file, self.hand_in(), kind="text", path="answer.md")  # the next hand-in has its own

    def test_a_file_named_answer_md_does_not_meet_the_text_answer(self):
        self.accepted(self.file, kind="text", path="answer.md")
        self.accepted(self.file, kind="file", path="answer.md", sha=SHA_B)
        self.refused("uq_assignment_submission_files_name", self.file, kind="file", path="ANSWER.md")
        rows = self.s.execute(text("SELECT kind::text, path FROM assignment_submission_files WHERE submission_id = :s "
                                   "ORDER BY id"), {"s": self.sub}).all()
        self.assertEqual([tuple(r) for r in rows], [("text", "answer.md"), ("file", "answer.md")])

    def test_file_names_are_unique_in_a_hand_in_ignoring_case(self):
        self.accepted(self.file, path="A.py")
        self.refused("uq_assignment_submission_files_name", self.file, path="a.py")
        self.refused("uq_assignment_submission_files_name", self.file, path="A.PY")
        self.accepted(self.file, path="Отчёт.pdf")
        self.refused("uq_assignment_submission_files_name", self.file, path="отчёт.PDF")  # lower() folds Cyrillic
        self.accepted(self.file, self.hand_in(), path="a.py")  # another hand-in of the same work

    # ---------------------------------------------------------------- assignment_submission_links
    def test_submission_link_checks(self):
        long_url = "https://e.ru/" + "x" * (LINK_URL_MAX_CHARS - len("https://e.ru/"))
        self.check("ck_assignment_submission_links_url_valid", self.link,
                   dict(url=long_url + "x"), dict(url=long_url))
        for bad in ("javascript:alert(1)", "ftp://e.ru/", "e.ru", "", "data:text/html,x", " https://e.ru"):
            self.check("ck_assignment_submission_links_url_valid", self.link, dict(url=bad), dict(url=URL_OK))
        self.check("ck_assignment_submission_links_url_valid", self.link,
                   dict(url="file:///etc/passwd"), dict(url="HTTPS://E.RU/"))
        self.check("ck_assignment_submission_links_title_length", self.link,
                   dict(title="я" * (LINK_TITLE_MAX_CHARS + 1)), dict(title="я" * LINK_TITLE_MAX_CHARS))
        self.check("ck_assignment_submission_links_position_non_negative", self.link,
                   dict(position=-1), dict(position=0))

    def test_links_and_files_go_with_their_hand_in(self):
        """Hand-ins are never deleted by the services; if one is (by hand), its files and links go with it."""
        sub = self.hand_in()
        self.file(sub, kind="text", path="answer.md")
        self.file(sub, path="main.py")
        self.link(sub)
        self.s.execute(text("DELETE FROM assignment_submissions WHERE id = :s"), {"s": sub})
        for table in ("assignment_submission_files", "assignment_submission_links"):
            n = self.s.execute(text(f"SELECT count(*) FROM {table} WHERE submission_id = :s"), {"s": sub})
            self.assertEqual(n.scalar_one(), 0, table)

    # ---------------------------------------------------------------- attachments
    def test_attachment_kind_shape(self):
        shape = "ck_attachments_kind_shape"
        bad_rows = [
            dict(kind="file", blob_sha256=None),
            dict(kind="file", name=None),
            dict(kind="file", size=None),
            dict(kind="file", url=URL_OK),
            dict(kind="file", title="заголовок"),
            dict(kind="link", name="a.pdf"),
            dict(kind="link", blob_sha256=SHA_A),
            dict(kind="link", size=3),
            dict(kind="link", url=None),
        ]
        for bad in bad_rows:
            with self.subTest(bad=bad):
                self.refused(shape, self.attachment, **bad)
        self.accepted(self.attachment, kind="file")
        self.accepted(self.attachment, kind="link")
        self.accepted(self.attachment, kind="link", title="Проект в Scratch")

    def test_attachment_checks(self):
        long_url = "https://e.ru/" + "x" * (LINK_URL_MAX_CHARS - len("https://e.ru/"))
        name_max = "я" * (ATTACHMENT_NAME_MAX_CHARS - 4) + ".pdf"
        cases = [
            ("ck_attachments_name_valid", dict(name=" \t\r\n "), dict(name="a.pdf")),
            ("ck_attachments_name_valid", dict(name="я" + name_max), dict(name=name_max)),
            ("ck_attachments_size_positive", dict(name="b.pdf", size=0), dict(name="b.pdf", size=1)),
            ("ck_attachments_url_valid", dict(kind="link", url="javascript:alert(1)"), dict(kind="link")),
            ("ck_attachments_url_valid", dict(kind="link", url=long_url + "x"), dict(kind="link", url=long_url)),
            ("ck_attachments_title_length", dict(kind="link", title="я" * (LINK_TITLE_MAX_CHARS + 1)),
             dict(kind="link", title="я" * LINK_TITLE_MAX_CHARS)),
            ("ck_attachments_position_non_negative", dict(name="c.pdf", position=-1), dict(name="c.pdf", position=2)),
        ]
        for constraint, bad, good in cases:
            self.check(constraint, self.attachment, bad, good)

    def test_attachment_names_are_unique_on_an_item_ignoring_case(self):
        self.accepted(self.attachment, name="Схема.PDF")
        self.refused("uq_attachments_item_name", self.attachment, name="схема.pdf", blob_sha256=SHA_B)
        self.accepted(self.attachment, kind="link")  # links have no names
        self.accepted(self.attachment, kind="link")  # the same link twice is not refused by the database
        self.accepted(self.attachment, self.insert("feed_items", **ASSIGNMENT), name="схема.pdf")  # another item

    def test_attachments_go_with_their_item_and_keep_blobs_and_authors(self):
        teacher = self.insert("users", login="teacher", password_hash="x", display_name="T", role="admin",
                              is_active=True)
        draft = self.insert("feed_items", **dict(ASSIGNMENT, state="draft", published_at=None))
        self.attachment(draft, created_by=teacher)
        self.attachment(draft, kind="link")
        self.attachment(blob_sha256=SHA_B, name="other.pdf")
        self.s.commit()
        self.refused("fk_attachments_blob_sha256_blobs", self.s.execute,
                     text(f"DELETE FROM blobs WHERE sha256 = '{SHA_B}'"))
        self.refused("fk_attachments_created_by_users", self.s.execute, text(f"DELETE FROM users WHERE id = {teacher}"))
        self.s.execute(text("DELETE FROM feed_items WHERE id = :i"), {"i": draft})  # a draft is deleted with them
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM attachments WHERE item_id = :i"), {"i": draft}
                                        ).scalar_one(), 0)
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM blobs WHERE sha256 IN (:a, :b)"),
                                        {"a": SHA_A, "b": SHA_B}).scalar_one(), 2, "blobs stay")

    # ---------------------------------------------------------------- foreign keys
    def test_the_new_foreign_keys_say_their_on_delete(self):
        """The list of the report of stage 2: c = CASCADE, r = RESTRICT (an explicit rule, unlike NO ACTION 'a')."""
        rows = self.s.execute(text("""
            SELECT c.conname, c.confdeltype
              FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
             WHERE n.nspname = current_schema() AND c.contype = 'f'
               AND c.conrelid::regclass::text IN ('assignment_submission_links', 'attachments')""")).all()
        self.assertEqual(dict(rows), {
            "fk_assignment_submission_links_submission": "c",  # with its hand-in, as the files of the hand-in
            "fk_attachments_item_id_feed_items": "c",          # with its item (only a draft is deleted)
            "fk_attachments_blob_sha256_blobs": "r",
            "fk_attachments_created_by_users": "r",
            "fk_attachments_material_version_id_material_versions": "r",  # the second owner, 4v.4
        })
        files = self.s.execute(text("SELECT confdeltype FROM pg_constraint WHERE conname = "
                                    "'fk_assignment_submission_files_submission' "
                                    "AND conrelid = 'assignment_submission_files'::regclass")).scalar_one()
        self.assertEqual(files, "c", "unchanged since 4v.1")

    def test_every_foreign_key_to_users_is_a_trace_or_erased(self):
        """users.traces must see every row that keeps a user from being deleted; the rest is deleted with it."""
        covered = {(c.table.name, c.name) for _, c in accounts.TRACES}
        erased = {(c.table.name, c.name) for c in accounts.ERASED}
        self.assertEqual(covered & erased, set())
        found = {(t.name, fk.parent.name) for t in Base.metadata.sorted_tables for fk in t.foreign_keys
                 if fk.column.table.name == "users"}
        self.assertEqual(found - covered - erased, set(), "add the column to users.TRACES or users.ERASED")
        self.assertEqual((covered | erased) - found, set())

    def test_user_traces_know_attachments(self):
        now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
        teacher = User(login="teacher", password_hash="x", display_name="T", role="admin")
        self.s.add(teacher)
        self.s.flush()
        self.attachment(created_by=teacher.id)
        self.attachment(kind="link", created_by=teacher.id)
        self.s.commit()
        self.assertEqual(accounts.traces(self.s, teacher), {"attachments added": 2})
        with self.assertRaises(accounts.UserError) as caught:
            accounts.delete_user(self.s, audit.Actor(cli="cli:test"), teacher, now)
        self.assertEqual(caught.exception.code, "has_traces")
