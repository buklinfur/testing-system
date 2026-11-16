"""The schema of students' draft files (4b.2, D-097, D-098, migration c5a9e3f1d27b) on a real PostgreSQL: every CHECK
and the partial unique index refuse a row by name and accept one at the edge; a deleted file frees its name but keeps
its row; the foreign keys say their ON DELETE in the catalog; a draft goes with its item, never with its blob; a draft
is no trace of the user and is erased with it. The migration itself: tests/pg/test_migrations_pg.py.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
from datetime import datetime, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.auth import audit, users as accounts
    from app.models import ATTACHMENT_NAME_MAX_CHARS, User

ASSIGNMENT = dict(kind="assignment", state="published", title="Фото тетради", body_md="", grading="none",
                  answer_kind="files", grades_release="manual", published_at="2026-11-15 10:00+00")
SHA_A, SHA_B = "e" * 64, "f" * 64


@requires_pg
class DraftSchemaTests(PgCase):
    def setUp(self):
        super().setUp()
        self.pupil = self.student(1).id
        self.other = self.student(2).id
        for sha in (SHA_A, SHA_B):
            self.s.execute(text("INSERT INTO blobs (sha256, size_bytes) VALUES (:s, 3)"), {"s": sha})
        self.item = self.insert("feed_items", **ASSIGNMENT)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def insert(self, table, **values):
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        return self.s.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params}) RETURNING id"),
                              values).scalar_one()

    def draft(self, **values):
        values = {"item_id": self.item, "user_id": self.pupil, "name": "IMG_0001.HEIC", "blob_sha256": SHA_A,
                  "size": 3, **values}
        return self.insert("work_draft_files", **values)

    def refused(self, constraint, **values):
        with self.assertRaises(IntegrityError) as caught:
            with self.s.begin_nested():
                self.draft(**values)
        self.assertIn(constraint, str(caught.exception.orig))

    def accepted(self, **values):
        with self.s.begin_nested():
            return self.draft(**values)

    def count(self, where="TRUE", **params):
        return self.s.execute(text(f"SELECT count(*) FROM work_draft_files WHERE {where}"), params).scalar_one()

    # ---------------------------------------------------------------- checks
    def test_name(self):
        for bad in ("", " ", "\t\r\n", "я" * (ATTACHMENT_NAME_MAX_CHARS + 1)):
            with self.subTest(bad=bad[:5]):
                self.refused("ck_work_draft_files_name_valid", name=bad)
        self.accepted(name="я" * ATTACHMENT_NAME_MAX_CHARS)
        self.accepted(name="тетрадь стр. 1.jpg")

    def test_size(self):
        self.refused("ck_work_draft_files_size_positive", size=0)
        self.refused("ck_work_draft_files_size_positive", size=-1)
        self.accepted(size=1)
        self.accepted(name="big.heic", size=30 * 1024 * 1024 * 1024)  # bigint: beyond int4

    def test_deleted_after_created(self):
        self.refused("ck_work_draft_files_deleted_after_created", created_at="2026-11-15 10:00+00",
                     deleted_at="2026-11-15 09:59:59+00")
        self.accepted(created_at="2026-11-15 10:00+00", deleted_at="2026-11-15 10:00+00")

    def test_live_names_are_unique_per_pair_ignoring_case(self):
        first = self.accepted(name="Фото.HEIC")
        self.refused("uq_work_draft_files_name", name="фото.heic", blob_sha256=SHA_B)
        self.accepted(name="фото.heic", user_id=self.other)  # another student
        self.accepted(name="фото.heic", item_id=self.insert("feed_items", **ASSIGNMENT))  # another item
        self.s.execute(text("UPDATE work_draft_files SET deleted_at = now() WHERE id = :i"), {"i": first})
        again = self.accepted(name="фото.heic", blob_sha256=SHA_B)  # the deleted one frees the name...
        self.assertNotEqual(again, first)
        self.assertEqual(self.count("deleted_at IS NOT NULL"), 1, "...and keeps its row for the quota")
        self.accepted(name="ФОТО.heic", deleted_at="2099-01-01 00:00+00")  # deleted rows never meet

    # ---------------------------------------------------------------- foreign keys
    def test_the_foreign_keys_say_their_on_delete(self):
        """c = CASCADE, r = RESTRICT (an explicit rule, unlike NO ACTION 'a')."""
        rows = self.s.execute(text("""
            SELECT c.conname, c.confdeltype
              FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
             WHERE n.nspname = current_schema() AND c.contype = 'f'
               AND c.conrelid = 'work_draft_files'::regclass""")).all()
        self.assertEqual(dict(rows), {
            "fk_work_draft_files_item_id_feed_items": "c",  # an item is deleted only as a draft
            "fk_work_draft_files_user_id_users": "c",  # drafts are no trace: erased with the user
            "fk_work_draft_files_blob_sha256_blobs": "r",  # blobs go only through the collector of module 6
        })

    def test_drafts_go_with_their_item_and_keep_blobs(self):
        self.draft()
        self.draft(name="b.jpg", blob_sha256=SHA_B, deleted_at="2099-01-01 00:00+00")
        self.s.commit()
        with self.assertRaises(IntegrityError) as caught:
            with self.s.begin_nested():
                self.s.execute(text(f"DELETE FROM blobs WHERE sha256 = '{SHA_B}'"))
        self.assertIn("fk_work_draft_files_blob_sha256_blobs", str(caught.exception.orig))
        self.s.execute(text("DELETE FROM feed_items WHERE id = :i"), {"i": self.item})
        self.assertEqual(self.count(), 0)
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM blobs WHERE sha256 IN (:a, :b)"),
                                        {"a": SHA_A, "b": SHA_B}).scalar_one(), 2, "blobs stay")

    # ---------------------------------------------------------------- the user
    def test_a_draft_is_no_trace_and_is_erased_with_the_user(self):
        now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        self.draft()
        self.draft(name="b.jpg", deleted_at="2099-01-01 00:00+00")
        self.draft(name="mine.jpg", user_id=self.other)
        self.s.commit()
        pupil = self.s.get(User, self.pupil)
        self.assertEqual(accounts.traces(self.s, pupil), {})
        details = accounts.delete_user(self.s, audit.Actor(cli="cli:test"), pupil, now)
        self.s.commit()
        self.assertEqual(details["draft_files_deleted"], 2)
        self.assertEqual(self.count("user_id = :u", u=self.pupil), 0)
        self.assertEqual(self.count("user_id = :u", u=self.other), 1, "another student's draft stays")
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM blobs WHERE sha256 IN (:a, :b)"),
                                        {"a": SHA_A, "b": SHA_B}).scalar_one(), 2, "blobs stay")
