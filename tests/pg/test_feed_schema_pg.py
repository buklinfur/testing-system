"""The schema of the class feed (D-059, migration b32303b5df22) on a real PostgreSQL: every CHECK and every partial
unique index refuses at least one row (by its name) and accepts at least one, and the foreign keys restrict or
cascade as docs/DATA_MODEL.md says. Services and rules come in later stages; here only the database holds.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

ASSIGNMENT = dict(kind="assignment", state="draft", title="Блок-схема", body_md="", grading="points", max_points=10,
                  answer_kind="text", grades_release="manual")
CONTEST = dict(kind="contest", state="draft", title="Контест", body_md="")


@requires_pg
class FeedSchemaTests(PgCase):
    def setUp(self):
        super().setUp()
        self.contest_id = self.seed["cp"].contest_id
        self.cp_id = self.seed["cp"].id
        self.pupil = self.student(1).id
        self.group = self.s.execute(text("INSERT INTO groups (name) VALUES ('7Б') RETURNING id")).scalar_one()
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def insert(self, table, **values):
        cols = ", ".join(values)
        params = ", ".join(f":{k}" for k in values)
        return self.s.execute(text(f"INSERT INTO {table} ({cols}) VALUES ({params}) RETURNING id"), values).scalar_one()

    def item(self, **kw):
        base = dict(CONTEST, contest_id=self.contest_id) if kw.get("kind") == "contest" else dict(ASSIGNMENT)
        base.update(kw)
        return self.insert("feed_items", **{k: v for k, v in base.items() if v is not None or k in kw})

    def refused(self, constraint, fn, *args, **kw):
        """The statement fails on exactly this constraint, and the session goes on (a savepoint)."""
        with self.assertRaises(IntegrityError) as caught:
            with self.s.begin_nested():
                fn(*args, **kw)
        self.assertIn(constraint, str(caught.exception.orig))

    def accepted(self, fn, *args, **kw):
        with self.s.begin_nested():
            return fn(*args, **kw)

    def work(self, item=None, user=None):
        return self.insert("assignment_works", item_id=item or self.item(), user_id=user or self.pupil,
                           status="submitted", status_at="2026-11-05 10:00+00")

    def hand_in(self, work):
        return self.insert("assignment_submissions", work_id=work)

    # ---------------------------------------------------------------- feed_items
    def test_feed_item_checks(self):
        cases = [  # (constraint, a refused row, an accepted row)
            ("ck_feed_items_title_not_blank", dict(title="  "), dict(title="Задание")),
            ("ck_feed_items_contest_link", dict(kind="contest", contest_id=None), dict(kind="contest")),
            ("ck_feed_items_contest_link", dict(contest_id=self.contest_id), dict(contest_id=None)),
            ("ck_feed_items_material_after_4v4", dict(kind="material", grading=None, max_points=None, answer_kind=None,
                                                      grades_release=None), dict(kind="assignment")),
            ("ck_feed_items_assignment_settings", dict(grading=None, max_points=None), dict(grading="none",
                                                                                            max_points=None)),
            ("ck_feed_items_assignment_settings", dict(answer_kind=None), dict(answer_kind="mark")),
            ("ck_feed_items_assignment_settings", dict(grades_release=None), dict(grades_release="immediate")),
            ("ck_feed_items_assignment_settings", dict(kind="contest", grading="pass_fail", max_points=None,
                                                       answer_kind=None, grades_release=None),
             dict(kind="contest")),
            ("ck_feed_items_due_only_assignment", dict(kind="contest", due_at="2026-11-10 10:00+00"),
             dict(kind="contest", available_from="2026-11-10 10:00+00")),
            ("ck_feed_items_max_points", dict(grading="points", max_points=None), dict(grading="points", max_points=5)),
            ("ck_feed_items_max_points", dict(grading="pass_fail", max_points=5), dict(grading="pass_fail",
                                                                                       max_points=None)),
            ("ck_feed_items_max_points_positive", dict(max_points=0), dict(max_points="0.5")),
            ("ck_feed_items_deadline", dict(available_from="2026-11-10 10:00+00", due_at="2026-11-10 10:00+00"),
             dict(available_from="2026-11-10 10:00+00", due_at="2026-11-10 10:00:00.000001+00")),
            ("ck_feed_items_published_at", dict(state="published"),
             dict(state="published", published_at="2026-11-05 10:00+00")),
            ("ck_feed_items_published_at", dict(state="archived"),
             dict(state="archived", published_at="2026-11-05 10:00+00")),
        ]
        for constraint, bad, good in cases:
            with self.subTest(constraint=constraint, bad=bad):
                self.refused(constraint, self.item, **bad)
                self.accepted(self.item, **good)
        # the deadline CHECK only binds when both moments are set
        for only in (dict(available_from="2026-11-10 10:00+00"), dict(due_at="2026-11-10 10:00+00")):
            self.accepted(self.item, **only)

    # ---------------------------------------------------------------- audience
    def test_audience_one_subject_and_uniqueness(self):
        item = self.item()
        audience = lambda **kw: self.insert("feed_item_audience", item_id=item, **kw)  # noqa: E731
        self.refused("ck_feed_item_audience_one_subject", audience)
        self.refused("ck_feed_item_audience_one_subject", audience, group_id=self.group, user_id=self.pupil)
        self.accepted(audience, group_id=self.group)
        self.accepted(audience, user_id=self.pupil)
        self.refused("uq_feed_item_audience_group", audience, group_id=self.group)
        self.refused("uq_feed_item_audience_user", audience, user_id=self.pupil)
        other = self.item()  # the same group and student on another item
        self.accepted(self.insert, "feed_item_audience", item_id=other, group_id=self.group)
        self.accepted(self.insert, "feed_item_audience", item_id=other, user_id=self.pupil)

    # ---------------------------------------------------------------- works, hand-ins, files
    def test_one_work_per_item_and_student(self):
        item = self.item()
        self.accepted(self.work, item)
        self.refused("uq_assignment_works_item_user", self.work, item)
        self.accepted(self.work, item, self.student(2).id)
        self.accepted(self.work, self.item())

    def test_submission_file_checks(self):
        sub = self.hand_in(self.work())
        f = lambda **kw: self.insert("assignment_submission_files", **{  # noqa: E731
            "submission_id": sub, "kind": "text", "path": "answer.md", "blob_sha256": "a" * 64, "size": 1,
            "position": 0, **kw})
        self.refused("ck_assignment_submission_files_path_not_blank", f, kind="file", path=" ")
        self.refused("ck_assignment_submission_files_non_negative", f, size=-1)
        self.refused("ck_assignment_submission_files_non_negative", f, position=-1)
        self.accepted(f, size=0)
        # 4v.3: one text answer per hand-in (the UNIQUE(submission_id, path) of 4v.1 became partial indexes, D-079)
        self.refused("uq_assignment_submission_files_text", f, blob_sha256="b" * 64)
        self.accepted(f, kind="file", path="scheme.png", position=1)
        self.refused("fk_assignment_submission_files_blob_sha256_blobs", f, kind="file", path="x.txt",
                     blob_sha256="d" * 64)

    # ---------------------------------------------------------------- reviews
    def test_review_checks(self):
        work = self.work()
        review = lambda **kw: self.insert("assignment_reviews", work_id=work, **kw)  # noqa: E731
        graded = self.accepted(review, action="graded", score=8)
        self.accepted(review, action="graded", passed=True)
        self.accepted(review, action="graded")  # grading none: a status without a mark
        self.accepted(review, action="returned", comment="переделай")
        cases = [
            ("ck_assignment_reviews_release_of", dict(action="released")),
            ("ck_assignment_reviews_release_of", dict(action="graded", release_of=graded)),
            ("ck_assignment_reviews_mark_only_graded", dict(action="returned", score=1)),
            ("ck_assignment_reviews_mark_only_graded", dict(action="released", release_of=graded, passed=False)),
            ("ck_assignment_reviews_one_mark", dict(action="graded", score=1, passed=True)),
            ("ck_assignment_reviews_score_non_negative", dict(action="graded", score=-1)),
            ("ck_assignment_reviews_comment_length", dict(action="returned", comment="я" * 2001)),
        ]
        for constraint, bad in cases:
            with self.subTest(constraint=constraint, bad=bad):
                self.refused(constraint, review, **bad)
        self.accepted(review, action="graded", score=0, comment="я" * 2000)
        self.accepted(review, action="released", release_of=graded)
        self.refused("uq_assignment_reviews_release_of", review, action="released", release_of=graded)
        regraded = self.accepted(review, action="graded", score=9)
        self.accepted(review, action="released", release_of=regraded)  # another grade is released once too

    # ---------------------------------------------------------------- deadline_extensions: a second target
    def test_extension_one_target_and_uniqueness_per_target(self):
        item = self.item(due_at="2026-11-10 10:00+00")
        ext = lambda **kw: self.insert("deadline_extensions", due_at="2026-11-11 10:00+00", **kw)  # noqa: E731
        self.refused("ck_deadline_extensions_one_target", ext, user_id=self.pupil)
        self.refused("ck_deadline_extensions_one_target", ext, contest_problem_id=self.cp_id, feed_item_id=item,
                     user_id=self.pupil)
        self.refused("ck_deadline_extensions_one_subject", ext, feed_item_id=item)
        # a student and a group may have one extension per target, of each kind
        self.accepted(ext, contest_problem_id=self.cp_id, user_id=self.pupil)
        self.accepted(ext, feed_item_id=item, user_id=self.pupil)
        self.accepted(ext, feed_item_id=item, group_id=self.group)
        self.refused("uq_deadline_extensions_item_user", ext, feed_item_id=item, user_id=self.pupil)
        self.refused("uq_deadline_extensions_item_group", ext, feed_item_id=item, group_id=self.group)
        self.refused("uq_deadline_extensions_user", ext, contest_problem_id=self.cp_id, user_id=self.pupil)
        self.accepted(ext, feed_item_id=self.item(due_at="2026-11-10 10:00+00"), user_id=self.pupil)

    # ---------------------------------------------------------------- foreign keys
    def test_restrict_and_cascade(self):
        """Each restricted row is pointed at by exactly one feed row, so the refusal names that foreign key."""
        listener, late = self.student(2).id, self.student(3).id
        teacher = self.insert("users", login="teacher", password_hash="x", display_name="T", role="admin", is_active=True)
        contest_item = self.item(kind="contest")
        self.insert("feed_item_audience", item_id=contest_item, group_id=self.group)
        self.insert("feed_item_audience", item_id=contest_item, user_id=listener)
        assignment = self.item(due_at="2026-11-10 10:00+00")
        work = self.work(assignment)
        sub = self.hand_in(work)
        self.insert("assignment_submission_files", submission_id=sub, kind="text", path="answer.md",
                    blob_sha256="c" * 64, size=1, position=0)
        review = self.insert("assignment_reviews", work_id=work, action="graded", score=5, reviewer_id=teacher)
        extended = self.item(due_at="2026-11-10 10:00+00")
        self.insert("deadline_extensions", feed_item_id=extended, user_id=late, due_at="2026-11-11 10:00+00")
        self.s.commit()

        released = self.insert("assignment_reviews", work_id=work, action="released", release_of=review)
        self.s.commit()
        restricted = [  # what is archived, never deleted, cannot go while a feed row points at it
            ("fk_feed_items_contest_id_contests", f"DELETE FROM contests WHERE id = {self.contest_id}"),
            ("fk_feed_item_audience_group_id_groups", f"DELETE FROM groups WHERE id = {self.group}"),
            ("fk_feed_item_audience_user_id_users", f"DELETE FROM users WHERE id = {listener}"),
            ("fk_assignment_works_user_id_users", f"DELETE FROM users WHERE id = {self.pupil}"),
            ("fk_assignment_reviews_reviewer_id_users", f"DELETE FROM users WHERE id = {teacher}"),
            ("fk_assignment_submission_files_blob_sha256_blobs", "DELETE FROM blobs WHERE sha256 = '" + "c" * 64 + "'"),
            ("fk_assignment_works_item_id_feed_items", f"DELETE FROM feed_items WHERE id = {assignment}"),
            ("fk_assignment_reviews_release_of_assignment_reviews", f"DELETE FROM assignment_reviews WHERE id = {review}"),
            ("fk_deadline_extensions_feed_item_id_feed_items", f"DELETE FROM feed_items WHERE id = {extended}"),
        ]
        for constraint, sql in restricted:
            with self.subTest(constraint=constraint):
                self.refused(constraint, self.s.execute, text(sql))

        # a work is pointed at by its hand-ins and its reviews: without the reviews, the hand-in restricts
        savepoint = self.s.begin_nested()
        self.s.execute(text(f"DELETE FROM assignment_reviews WHERE id IN ({released}, {review})"))
        self.refused("fk_assignment_submissions_work_id_assignment_works", self.s.execute,
                     text(f"DELETE FROM assignment_works WHERE id = {work}"))
        savepoint.rollback()
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM assignment_reviews")).scalar_one(), 2)

        # the files of a hand-in go with it; the audience goes with its item
        self.s.execute(text(f"DELETE FROM assignment_reviews WHERE id IN ({released}, {review})"))
        self.s.execute(text(f"DELETE FROM assignment_submissions WHERE id = {sub}"))
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM assignment_submission_files")).scalar_one(), 0)
        self.s.execute(text(f"DELETE FROM feed_items WHERE id = {contest_item}"))
        self.assertEqual(self.s.execute(text("SELECT count(*) FROM feed_item_audience")).scalar_one(), 0)
        self.s.commit()

    def test_the_new_foreign_keys_say_their_on_delete(self):
        """Every foreign key of the feed tables has an explicit rule; only the audience, the files of a hand-in and
        (as in 4a) the extensions of a contest problem cascade."""
        rows = self.s.execute(text("""
            SELECT c.conrelid::regclass::text, c.conname, c.confdeltype
              FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
             WHERE n.nspname = current_schema() AND c.contype = 'f'
               AND c.conrelid::regclass::text IN ('feed_items', 'feed_item_audience', 'assignment_works',
                   'assignment_submissions', 'assignment_submission_files', 'assignment_reviews')
                OR (c.contype = 'f' AND c.conname = 'fk_deadline_extensions_feed_item_id_feed_items'
                    AND n.nspname = current_schema())""")).all()
        rules = {name: rule for _table, name, rule in rows}
        cascade = {"fk_feed_item_audience_item_id_feed_items", "fk_assignment_submission_files_submission"}
        self.assertEqual(len(rules), 15)
        for name, rule in rules.items():
            with self.subTest(name=name):
                self.assertEqual(rule, "c" if name in cascade else "r")  # c = CASCADE, r = RESTRICT
