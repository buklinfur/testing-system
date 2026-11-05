"""Services of the class feed and manual assignments (classroom/items.py, works.py; D-059, docs/CLASSROOM.md §7) on a
real PostgreSQL: items, audience, feeds, extensions, hand-ins, grades, returns, releases, the order of events, the
locks under concurrency, the status cache against its history, identity, the traces in groups and users, and
dev_seed --item.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import contextlib
import inspect
import io
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
US = timedelta(microseconds=1)

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import db, dev_seed
    from app import groups as g
    from app.auth import audit, users as accounts
    from app.blobstore import BlobStore
    from app.classroom import grading, items, works
    from app.classroom.items import FeedError
    from app.contests import deadlines, service as contests
    from app.contests.spec import parse
    from app.models import (AssignmentReview, AssignmentSubmission, AssignmentSubmissionFile, AssignmentWork,
                            AuditLog, Blob, Contest, ContestState, FeedItem, Judgement, Role, User)
    from app.timeutil import zone

EVENT = {"submission": "submit", "graded": "grade", "returned": "return", "released": "release"}


@requires_pg
class ClassroomServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        self.actor = audit.Actor(user_id=self.seed["admin"].id)
        self.a, self.b, self.c = self.student(1), self.student(2), self.student(3)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], T0)
        self.s.commit()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blobs = BlobStore(tmp.name)

    # ---------------------------------------------------------------- helpers
    def assignment(self, groups=None, users=(), publish=True, **fields):
        base = dict(grading="points", max_points=10, due_at=DUE)
        base.update(fields)
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Блок-схема", body_md="Нарисуй",
                                 **base)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group] if groups is None else groups,
                           users=list(users))
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def submit(self, user, item, now=T0, text="ответ", session=None, **kw):
        return works.submit_work(session or self.s, user, item, now, blobs=self.blobs, text=text, **kw)

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(FeedError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.s.rollback()

    def assert_cache_matches_history(self, item):
        """The cached status of every work == next_status applied to its events in their order."""
        self.s.expire_all()
        for work in self.s.execute(select(AssignmentWork).where(AssignmentWork.item_id == item.id)).scalars():
            status = None
            for kind, row in works.events(self.s, work):
                t = grading.next_status(status, EVENT["submission" if kind == "submission" else row.action.value])
                self.assertTrue(t, f"work {work.id}: {kind} not allowed after {status}")
                status = t.status
            self.assertEqual(work.status.value, status, f"work {work.id}")

    def run_parallel(self, *jobs, hold=0.3):
        """Each job(session) runs in its own connection, all released at once by a barrier; a job that succeeds
        keeps its transaction open `hold` seconds before committing (without the lock another one would decide in
        that gap). Returns the results (or the FeedError codes), in the order of the jobs."""
        barrier, results, errors = threading.Barrier(len(jobs)), [None] * len(jobs), []

        def run(i, job):
            session = self.Session()
            try:
                barrier.wait()
                try:
                    results[i] = job(session)
                    session.flush()
                    time.sleep(hold)
                    session.commit()
                except FeedError as exc:
                    session.rollback()
                    results[i] = exc.code
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        self.s.expire_all()
        return results

    def in_session(self, session, item, *users):
        return (session.get(FeedItem, item.id), *[session.get(User, u.id) for u in users])

    # ---------------------------------------------------------------- items
    def test_create_refuses_what_the_schema_would_with_codes(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        cases = [
            ("title_invalid", dict(kind="assignment", title="  ", grading="none")),
            ("kind_not_available", dict(kind="material", title="Памятка")),
            ("contest_required", dict(kind="contest", title="К")),
            ("item_invalid", dict(kind="assignment", title="З", grading="none", contest=contest)),
            ("grading_invalid", dict(kind="assignment", title="З")),
            ("max_points_invalid", dict(kind="assignment", title="З", grading="points")),
            ("max_points_invalid", dict(kind="assignment", title="З", grading="pass_fail", max_points=5)),
            ("answer_kind_invalid", dict(kind="assignment", title="З", grading="none", answer_kind="files")),
            ("deadline_invalid", dict(kind="assignment", title="З", grading="none", available_from=DUE, due_at=DUE)),
            ("item_invalid", dict(kind="contest", title="К", contest=contest, due_at=DUE)),
            ("item_invalid", dict(kind="contest", title="К", contest=contest, grading="none")),
        ]
        for code, kw in cases:
            with self.subTest(code=code, kw=kw):
                self.refused(code, items.create_item, self.s, self.actor, T0, **kw)
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="  Схема  ", grading="none")
        self.assertEqual((item.title, item.answer_kind.value, item.grades_release.value, item.state.value),
                         ("Схема", "text", "manual", "draft"))
        self.assertEqual(self.s.execute(select(AuditLog.action).where(AuditLog.object_type == "feed_item")).scalar(),
                         "item.create")

    def test_publish_needs_an_addressee_and_audience_rules(self):
        item = self.assignment(groups=[], publish=False)
        self.refused("no_audience", items.publish, self.s, self.actor, item, T0)
        self.refused("not_a_student", items.set_audience, self.s, self.actor, item, T0, users=[self.seed["admin"]])
        archived = g.create(self.s, self.actor, "6А", T0)
        g.archive(self.s, self.actor, archived, T0)
        self.s.commit()
        self.refused("group_archived", items.set_audience, self.s, self.actor, item, T0, groups=[archived])
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group], users=[self.c])
        self.assertTrue(items.publish(self.s, self.actor, item, T0))
        self.s.commit()
        self.refused("no_audience", items.set_audience, self.s, self.actor, item, T0)
        # an archived group already in the audience may stay (it gives nothing, K6)
        g.archive(self.s, self.actor, self.group, T0)
        result = items.add_audience(self.s, self.actor, item, T0, users=[self.b])
        self.assertEqual(result, {"added": ["student student2"], "removed": []})
        self.assertEqual(items.remove_audience(self.s, self.actor, item, T0, users=[self.c]),
                         {"added": [], "removed": ["student student3"]})

    def test_delete_only_a_draft_without_works_and_extensions(self):
        published = self.assignment()
        self.refused("item_not_draft", items.delete_item, self.s, self.actor, published, T0)
        draft = self.assignment(publish=False)
        items.add_extension(self.s, self.actor, draft, DUE + timedelta(hours=1), T0, user=self.a)
        self.s.commit()
        self.refused("item_has_extensions", items.delete_item, self.s, self.actor, draft, T0)
        other = self.assignment(publish=False)
        self.s.add(AssignmentWork(item_id=other.id, user_id=self.a.id, status="submitted", status_at=T0))
        self.s.commit()
        self.refused("item_has_works", items.delete_item, self.s, self.actor, other, T0)
        clean = self.assignment(publish=False)
        items.delete_item(self.s, self.actor, clean, T0)
        self.s.commit()
        self.assertIsNone(self.s.get(FeedItem, clean.id))
        self.refused("item_not_published", items.archive, self.s, self.actor, draft, T0)
        self.assertTrue(items.archive(self.s, self.actor, published, T0))
        self.assertTrue(items.unarchive(self.s, self.actor, published, T0))

    def test_update_scale_locked_after_a_grade_late_never_recomputed(self):
        item = self.assignment(after_due_submit="late")
        sub, decision = self.submit(self.a, item, now=DUE + US)
        self.assertTrue(decision.late)
        self.s.commit()
        changed = items.update_item(self.s, self.actor, item, T0, due_at=DUE + timedelta(days=1), max_points=20)
        self.assertEqual(sorted(changed), ["due_at", "max_points"])
        self.s.commit()
        self.assertTrue(self.s.get(AssignmentSubmission, sub.id).late, "late is fixed at receipt")
        works.grade_work(self.s, self.actor, item, self.a, T0, score=5)
        self.s.commit()
        self.refused("grading_locked", items.update_item, self.s, self.actor, item, T0, max_points=30)
        self.refused("grading_locked", items.update_item, self.s, self.actor, item, T0, grading="none",
                     max_points=None)
        self.assertEqual(items.update_item(self.s, self.actor, item, T0, max_points=Decimal("20.00")), [])
        self.assertEqual(items.update_item(self.s, self.actor, item, T0, title="Новая"), ["title"])
        self.refused("item_invalid", items.update_item, self.s, self.actor, item, T0, kind="contest")

    # ---------------------------------------------------------------- contest items, feeds
    def contest(self, mode="open", groups=()):
        text = ("format_version: 1\nslug: lesson\ntitle: Урок\nstarts_at: 2026-11-01 09:00\n"
                f"access: {{mode: {mode}" + (f", groups: [{', '.join(groups)}]" if groups else "") + "}\n"
                "problems:\n  - {label: A, problem: sum}\n")
        result = contests.apply(self.s, self.actor, parse(text, zone("UTC")), T0)
        contests.set_state(self.s, self.actor, result.contest, ContestState.published, T0)
        self.s.commit()
        return result.contest

    def contest_item(self, contest, groups=None, users=()):
        item = items.create_item(self.s, self.actor, T0, kind="contest", title="Контест", contest=contest)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group] if groups is None else groups,
                           users=list(users))
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def test_contest_warning_is_computed_and_gives_no_access(self):
        open_contest = self.contest()
        self.assertIsNone(items.contest_warning(self.s, self.contest_item(open_contest)))
        other = g.create(self.s, self.actor, "8В", T0)
        self.s.commit()
        groups_contest = self.contest("groups", ["8В"])
        item = self.contest_item(groups_contest)
        self.assertIn("groups not attached to contest lesson: 7Б", items.contest_warning(self.s, item))
        self.assertNotIn(item, items.student_feed(self.s, self.a, T0), "the item gives no access")
        self.assertEqual(self.s.execute(select(func.count()).select_from(Judgement)).scalar_one(), 0)
        items.set_audience(self.s, self.actor, item, T0, groups=[other])
        self.assertIsNone(items.contest_warning(self.s, item))
        code_contest = self.contest("code")
        self.assertIn("invite code", items.contest_warning(self.s, self.contest_item(code_contest)))

    def test_feeds(self):
        later = self.assignment(available_from=T0 + timedelta(days=1))
        first = self.assignment()
        personal = self.assignment(groups=[], users=[self.a])
        both = self.assignment(users=[self.a])  # the group and personally: once in the feed
        draft = self.assignment(publish=False)
        open_item = self.contest_item(self.contest())
        feed = items.student_feed(self.s, self.a, T0 + timedelta(days=2))
        self.assertEqual([i.id for i in feed].count(both.id), 1)
        self.assertEqual({i.id for i in feed}, {later.id, first.id, personal.id, both.id, open_item.id})
        self.assertNotIn(later.id, [i.id for i in items.student_feed(self.s, self.a, T0)], "delayed publication")
        self.assertNotIn(personal.id, [i.id for i in items.student_feed(self.s, self.b, T0)])
        # the teacher's feed of the group: newest first by available_from, else published_at; drafts too
        self.assertEqual([i.id for i in items.group_feed(self.s, self.group)][:1], [later.id])
        self.assertIn(draft.id, [i.id for i in items.group_feed(self.s, self.group)])
        # removed from the group: the group's items are gone (the contest item too), the personal ones stay
        g.remove_members(self.s, self.actor, self.group, [self.a], T0)
        self.s.commit()
        self.assertEqual({i.id for i in items.student_feed(self.s, self.a, T0 + timedelta(days=2))},
                         {personal.id, both.id})

    # ---------------------------------------------------------------- hand-ins
    def test_hand_in_stores_answer_md_once_and_checks_the_limit_first(self):
        item = self.assignment(users=[self.c])
        sub, _ = self.submit(self.a, item, text="мой ответ")
        sub2, _ = self.submit(self.b, item, text="мой ответ")
        self.s.commit()
        files = self.s.execute(select(AssignmentSubmissionFile).order_by(AssignmentSubmissionFile.id)).scalars().all()
        self.assertEqual([(f.path, f.size) for f in files], [("answer.md", len("мой ответ".encode()))] * 2)
        self.assertEqual(files[0].blob_sha256, files[1].blob_sha256, "the same text is one blob")
        self.assertEqual(self.s.execute(select(func.count()).select_from(Blob)
                                        .where(Blob.sha256 == files[0].blob_sha256)).scalar_one(), 1)
        self.assertEqual(works.answer_text(self.s, self.blobs, sub), "мой ответ")
        stored = sorted(p.name for p in self.blobs.root.rglob("*") if p.is_file())
        self.refused("text_too_long", self.submit, self.c, item, text="я" * 6, text_limit=11)
        self.assertEqual(sorted(p.name for p in self.blobs.root.rglob("*") if p.is_file()), stored,
                         "nothing written before the limit")
        self.assertIsNone(works.work_of(self.s, item, self.c), "no work for a refused first hand-in")
        mark = self.assignment(answer_kind="mark")
        sub3, _ = self.submit(self.a, mark, text=None)
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentSubmissionFile)
                                        .where(AssignmentSubmissionFile.submission_id == sub3.id)).scalar_one(), 0)
        self.refused("text_not_expected", self.submit, self.a, mark, text="тетрадь")

    def test_manual_work_never_reaches_the_queue(self):
        item = self.assignment()
        self.submit(self.a, item)
        works.grade_work(self.s, self.actor, item, self.a, T0, score=1)
        self.s.commit()
        self.assertEqual(self.s.execute(select(func.count()).select_from(Judgement)).scalar_one(), 0)

    def test_deadline_extensions_and_late(self):
        item = self.assignment(after_due_submit="late")
        _, on_time = self.submit(self.a, item, now=DUE)
        self.assertFalse(on_time.late)
        items.add_extension(self.s, self.actor, item, DUE + timedelta(hours=2), T0, group=self.group)
        _, ext = self.submit(self.b, item, now=DUE + timedelta(hours=1))
        self.assertFalse(ext.late, "the group's extension")
        late_sub, late = self.submit(self.b, item, now=DUE + timedelta(hours=2) + US)
        self.assertTrue(late.late)
        self.s.commit()
        # an extension given afterwards changes nothing already received
        items.add_extension(self.s, self.actor, item, DUE + timedelta(days=3), T0, user=self.b)
        self.s.commit()
        self.s.expire_all()
        self.assertTrue(self.s.get(AssignmentSubmission, late_sub.id).late)
        strict = self.assignment(after_due_submit="no")
        self.refused("deadline_passed", self.submit, self.a, strict, now=DUE + US)
        self.assert_cache_matches_history(item)

    def test_item_extensions_use_the_core_and_their_own_audit(self):
        item = self.assignment()
        row, old = items.add_extension(self.s, self.actor, item, DUE + timedelta(hours=1), T0, user=self.a,
                                       reason="болел")
        self.assertIsNone(old)
        _, old = items.add_extension(self.s, self.actor, item, DUE + timedelta(hours=2), T0, user=self.a)
        self.assertEqual(old, DUE + timedelta(hours=1))
        self.s.commit()
        self.refused("extension_not_later", items.add_extension, self.s, self.actor, item, DUE, T0, user=self.a)
        self.refused("not_a_student", items.add_extension, self.s, self.actor, item, DUE + US, T0,
                     user=self.seed["admin"])
        no_due = self.assignment(due_at=None)
        self.refused("no_deadline", items.add_extension, self.s, self.actor, no_due, DUE, T0, user=self.a)
        contest_item = self.contest_item(self.contest())
        self.refused("not_an_assignment", items.add_extension, self.s, self.actor, contest_item, DUE, T0,
                     user=self.a)
        self.assertEqual([e["user"] for e in items.list_extensions(self.s, item)], ["student1"])
        self.refused("extension_not_found", items.remove_extension, self.s, self.actor, no_due, row.id, T0)
        items.remove_extension(self.s, self.actor, item, row.id, T0)
        # contest problems keep their own action names
        deadline_contest = self.contest()
        cp = deadline_contest.problems[0]
        cp.due_at = DUE
        self.s.commit()
        deadlines.add_extension(self.s, self.actor, deadline_contest, "A", DUE + US, T0, user=self.a)
        actions = list(self.s.execute(select(AuditLog.action).where(AuditLog.action.like("%.extension_%"))
                                      .order_by(AuditLog.id)).scalars())
        self.assertEqual(actions, ["item.extension_add", "item.extension_add", "item.extension_remove",
                                   "contest.extension_add"])

    # ---------------------------------------------------------------- grades, returns, releases
    def test_grade_checks(self):
        item = self.assignment()
        self.refused("score_out_of_range", works.grade_work, self.s, self.actor, item, self.a, T0, score=11)
        self.refused("grade_kind_mismatch", works.grade_work, self.s, self.actor, item, self.a, T0, score=1,
                     passed=True)
        self.refused("not_addressed", works.grade_work, self.s, self.actor, item, self.c, T0, score=1)
        self.refused("not_a_student", works.grade_work, self.s, self.actor, item, self.seed["admin"], T0, score=1)
        self.refused("comment_too_long", works.grade_work, self.s, self.actor, item, self.a, T0, score=1,
                     comment="я" * 2001)
        draft = self.assignment(publish=False)
        self.refused("not_published", works.grade_work, self.s, self.actor, draft, self.a, T0, score=1)
        # graded without a hand-in: the work is created graded
        result = works.grade_work(self.s, self.actor, item, self.a, T0, score=0, comment="не сдано")
        self.s.commit()
        self.assertEqual(result["status"], "graded")
        self.refused("already_graded", self.submit, self.a, item)
        self.assert_cache_matches_history(item)

    def test_return_and_hand_in_again(self):
        item = self.assignment()
        self.refused("nothing_to_return", works.return_work, self.s, self.actor, item, self.a, T0, "x")
        self.submit(self.a, item)
        self.s.commit()
        self.refused("comment_required", works.return_work, self.s, self.actor, item, self.a, T0, "  ")
        works.return_work(self.s, self.actor, item, self.a, T0 + US, "добавь вывод")
        self.s.commit()
        view = works.student_work(self.s, self.a, item, T0 + US)
        self.assertEqual((view["status"], view["returned_comment"]), ("returned", "добавь вывод"))
        self.refused("already_returned", works.return_work, self.s, self.actor, item, self.a, T0, "ещё")
        self.submit(self.a, item, now=T0 + 2 * US)
        self.s.commit()
        self.assertEqual(works.student_work(self.s, self.a, item, T0 + 2 * US)["status"], "submitted")
        self.assert_cache_matches_history(item)

    def test_grade_with_release_in_one_transaction_shares_at_and_is_seen(self):
        """Order of events (at, kind, id): a grade and its release at the same `at` keep their order."""
        item = self.assignment()
        self.submit(self.a, item)
        works.grade_work(self.s, self.actor, item, self.a, T0 + US, score=8, comment="хорошо", release=True)
        self.s.commit()
        rows = self.s.execute(select(AssignmentReview).order_by(AssignmentReview.id)).scalars().all()
        self.assertEqual([(r.action.value, r.at) for r in rows], [("graded", T0 + US), ("released", T0 + US)])
        view = works.student_work(self.s, self.a, item, T0 + US)
        self.assertEqual((view["status"], view["grade"]["score"]), ("graded", Decimal(8)))
        self.assert_cache_matches_history(item)

    def test_hand_in_and_review_at_the_same_moment(self):
        """A hand-in before a review at the same `at`: the grade is the last event."""
        item = self.assignment(grades_release="immediate")
        self.submit(self.a, item, now=T0)
        works.grade_work(self.s, self.actor, item, self.a, T0, score=6)
        self.s.commit()
        work = works.work_of(self.s, item, self.a)
        self.assertEqual([k for k, _r in works.events(self.s, work)], ["submission", "review"])
        self.assertEqual(works.student_work(self.s, self.a, item, T0)["status"], "graded")
        self.assert_cache_matches_history(item)

    def test_release_rules_and_bulk_release(self):
        item = self.assignment(users=[self.c])
        d = self.student(4)
        items.add_audience(self.s, self.actor, item, T0, users=[d])
        for user in (self.a, self.b, self.c):
            self.submit(user, item)
        works.grade_work(self.s, self.actor, item, self.a, T0, score=7)  # graded, not released
        works.grade_work(self.s, self.actor, item, self.c, T0, score=5, release=True)  # already released
        self.s.commit()
        self.assertIsNone(works.student_work(self.s, self.a, item, T0)["grade"], "manual: hidden until released")
        result = works.release_grades(self.s, self.actor, item, T0 + US, [self.a, self.b, self.c, d])
        self.s.commit()
        self.assertEqual({r["login"]: r["result"] for r in result},
                         {"student1": "released", "student2": "nothing_to_release", "student3": "already_released",
                          "student4": "nothing_to_release"})
        self.assertEqual(works.student_work(self.s, self.a, item, T0 + US)["grade"]["score"], Decimal(7))
        # a regrade after the release stays hidden until it is released
        works.grade_work(self.s, self.actor, item, self.a, T0 + 2 * US, score=9)
        self.s.commit()
        self.assertEqual(works.student_work(self.s, self.a, item, T0 + 2 * US)["grade"]["score"], Decimal(7))
        works.release_grades(self.s, self.actor, item, T0 + 3 * US)
        self.s.commit()
        self.assertEqual(works.student_work(self.s, self.a, item, T0 + 3 * US)["grade"]["score"], Decimal(9))
        audit_rows = self.s.execute(select(AuditLog).where(AuditLog.action == "work.release")
                                    .order_by(AuditLog.id)).scalars().all()
        self.assertEqual(audit_rows[0].details["released"], ["student1"])
        self.assert_cache_matches_history(item)

    def test_no_release_needed_for_immediate_and_for_grading_none(self):
        for fields in (dict(grades_release="immediate"), dict(grading="none", max_points=None)):
            with self.subTest(**fields):
                item = self.assignment(**fields)
                self.submit(self.a, item)
                kw = {} if fields.get("grading") == "none" else {"score": 3}
                works.grade_work(self.s, self.actor, item, self.a, T0, comment="принято", **kw)
                self.s.commit()
                view = works.student_work(self.s, self.a, item, T0)
                self.assertEqual((view["status"], view["grade"]["comment"]), ("graded", "принято"))
                self.refused("release_not_needed", works.release_grades, self.s, self.actor, item, T0)

    # ---------------------------------------------------------------- concurrency
    def test_two_first_hand_ins_at_once_make_one_work(self):
        item = self.assignment()

        def hand_in(session):
            it, user = self.in_session(session, item, self.a)
            return works.submit_work(session, user, it, T0, blobs=self.blobs, text="x")[0].id

        results = self.run_parallel(hand_in, hand_in)
        self.assertTrue(all(isinstance(r, int) for r in results), results)
        work_rows = self.s.execute(select(AssignmentWork).where(AssignmentWork.item_id == item.id)).scalars().all()
        self.assertEqual(len(work_rows), 1)
        self.assertEqual(work_rows[0].status.value, "submitted")
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentSubmission)).scalar_one(), 2)
        self.assert_cache_matches_history(item)

    def test_grade_without_hand_in_against_the_first_hand_in(self):
        item = self.assignment()

        def hand_in(session):
            it, user = self.in_session(session, item, self.a)
            return works.submit_work(session, user, it, T0, blobs=self.blobs, text="x")[0].id

        def grade(session):
            it, user = self.in_session(session, item, self.a)
            return works.grade_work(session, self.actor, it, user, T0, score=0)["status"]

        results = self.run_parallel(hand_in, grade)
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentWork)).scalar_one(), 1)
        # either the hand-in came first (then the grade), or the grade did and the hand-in is already_graded
        self.assertTrue(results[0] == "already_graded" or isinstance(results[0], int), results)
        self.assertEqual(results[1], "graded")
        self.assert_cache_matches_history(item)

    def test_two_grades_at_once(self):
        item = self.assignment()
        self.submit(self.a, item)
        self.s.commit()

        def grade(score):
            def job(session):
                it, user = self.in_session(session, item, self.a)
                return works.grade_work(session, self.actor, it, user, T0 + US, score=score)["status"]
            return job

        self.assertEqual(self.run_parallel(grade(4), grade(5)), ["graded", "graded"])
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentReview)).scalar_one(), 2)
        self.assert_cache_matches_history(item)

    def test_hand_in_against_a_grade(self):
        item = self.assignment()
        self.submit(self.a, item)
        self.s.commit()

        def hand_in(session):
            it, user = self.in_session(session, item, self.a)
            return works.submit_work(session, user, it, T0 + US, blobs=self.blobs, text="y")[0].id

        def grade(session):
            it, user = self.in_session(session, item, self.a)
            return works.grade_work(session, self.actor, it, user, T0 + US, score=6)["status"]

        results = self.run_parallel(hand_in, grade)
        self.assertEqual(results[1], "graded")
        self.assertTrue(results[0] == "already_graded" or isinstance(results[0], int), results)
        self.assertEqual(self.s.execute(select(AssignmentWork.status)).scalar_one().value, "graded")
        self.assert_cache_matches_history(item)

    def test_release_against_a_regrade(self):
        item = self.assignment()
        self.submit(self.a, item)
        works.grade_work(self.s, self.actor, item, self.a, T0, score=4)
        self.s.commit()

        def release(session):
            it = session.get(FeedItem, item.id)
            return works.release_grades(session, self.actor, it, T0 + US)[0]["result"]

        def regrade(session):
            it, user = self.in_session(session, item, self.a)
            return works.grade_work(session, self.actor, it, user, T0 + US, score=8)["status"]

        results = self.run_parallel(release, regrade)
        self.assertEqual(results, ["released", "graded"])
        reviews = self.s.execute(select(AssignmentReview).order_by(AssignmentReview.id)).scalars().all()
        released = [r for r in reviews if r.action.value == "released"]
        self.assertEqual(len(released), 1)
        graded_before = [r.id for r in reviews if r.action.value == "graded" and r.id < released[0].id]
        self.assertEqual(released[0].release_of, graded_before[-1], "the release published the grade it saw")
        self.assert_cache_matches_history(item)

    def test_two_bulk_releases_at_once_release_each_grade_once(self):
        item = self.assignment()
        for user in (self.a, self.b):
            self.submit(user, item)
            works.grade_work(self.s, self.actor, item, user, T0, score=2)
        self.s.commit()

        def release(session):
            return sorted(r["result"] for r in works.release_grades(session, self.actor,
                                                                    session.get(FeedItem, item.id), T0 + US))

        results = self.run_parallel(release, release)
        self.assertEqual(sorted(results), [["already_released"] * 2, ["released"] * 2])
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentReview)
                                        .where(AssignmentReview.action == "released")).scalar_one(), 2)

    # ---------------------------------------------------------------- what a student sees, identity
    def test_archived_item_keeps_the_work_visible_and_refuses_hand_ins(self):
        item = self.assignment()
        self.submit(self.a, item)
        works.grade_work(self.s, self.actor, item, self.a, T0, score=7, release=True)
        items.archive(self.s, self.actor, item, T0)
        self.s.commit()
        self.assertNotIn(item.id, [i.id for i in items.student_feed(self.s, self.a, T0)])
        self.refused("item_archived", self.submit, self.a, item)
        view = works.student_work(self.s, self.a, item, T0)
        self.assertEqual((view["status"], view["grade"]["score"]), ("graded", Decimal(7)))
        self.assertIsNone(works.student_work(self.s, self.b, item, T0), "no work, not in the feed: nothing")

    def test_identity_comes_from_the_user_object_only(self):
        for fn in (works.submit_work, works.student_work, works.grade_work, works.return_work, works.work_detail):
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(params & {"work_id", "user_id", "student_id", "submission_id"}, fn.__name__)
        item = self.assignment()
        self.submit(self.b, item, text="ответ B")
        works.grade_work(self.s, self.actor, item, self.b, T0, score=9, release=True)
        self.s.commit()
        work_b = works.work_of(self.s, item, self.b)
        # A sees only A's (empty) work: no status, no grade of B
        view = works.student_work(self.s, self.a, item, T0)
        self.assertEqual((view["status"], view["grade"], view["submissions"]), (None, None, 0))
        # A's hand-in makes A's own work and leaves B's untouched
        self.submit(self.a, item, text="ответ A")
        self.s.commit()
        self.assertNotEqual(works.work_of(self.s, item, self.a).id, work_b.id)
        self.s.refresh(work_b)
        self.assertEqual(work_b.status.value, "graded")
        self.assertEqual([e["text"] for e in works.work_detail(self.s, self.blobs, item, self.b, T0)["events"]
                          if e["kind"] == "submission"], ["ответ B"])

    # ---------------------------------------------------------------- reading for judgectl and the admin API (4v.1b)
    def test_items_query_and_item_detail(self):
        other = g.create(self.s, self.actor, "8В", T0)
        first = self.assignment(users=[self.c])
        second = self.assignment(groups=[other], publish=False, grading="pass_fail", max_points=None)
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        link = items.create_item(self.s, self.actor, T0, kind="contest", title="Олимпиада", contest=contest)
        self.s.commit()

        def ids(**kw):
            return [i.id for i in self.s.execute(items.items_query(**kw).order_by(FeedItem.id)).scalars()]

        self.assertEqual(ids(), [first.id, second.id, link.id])
        self.assertEqual(ids(group=self.group), [first.id])
        self.assertEqual(ids(group=other), [second.id])
        self.assertEqual(ids(kind="contest"), [link.id])
        self.assertEqual(ids(states=["draft"]), [second.id, link.id])
        items.add_extension(self.s, self.actor, first, DUE + timedelta(days=1), T0, user=self.a)
        self.s.commit()
        d = items.item_detail(self.s, first)
        self.assertEqual((d["id"], d["grading"].value, d["max_points"], d["answer_kind"].value, d["extensions"]),
                         (first.id, "points", Decimal(10), "text", 1))
        self.assertEqual(d["audience"], {"groups": [{"id": self.group.id, "name": "7Б", "archived": False}],
                                         "users": [{"id": self.c.id, "login": "student3",
                                                    "display_name": self.c.display_name}]})
        self.assertIsNone(d["contest"])
        self.assertEqual(items.item_detail(self.s, link)["contest"]["slug"], contest.slug)

    def test_score_and_max_points_take_plain_decimals_only(self):
        item = self.assignment()
        for bad in ("4.555", "1e1", "NaN", "Infinity", 4.555, float("nan"), True, "4,5"):
            with self.subTest(score=bad):
                self.refused("score_invalid", works.grade_work, self.s, self.actor, item, self.a, T0, score=bad)
        self.refused("score_out_of_range", works.grade_work, self.s, self.actor, item, self.a, T0, score="-1")
        works.grade_work(self.s, self.actor, item, self.a, T0, score=4.5)
        works.grade_work(self.s, self.actor, item, self.b, T0, score="7.25")
        self.s.commit()
        self.assertEqual([r.score for r in self.s.execute(select(AssignmentReview).order_by(AssignmentReview.id))
                          .scalars()], [Decimal("4.5"), Decimal("7.25")])
        for bad in ("1e1", "4.555", "NaN", "-5", "0"):
            with self.subTest(max_points=bad):
                self.refused("max_points_invalid", items.create_item, self.s, self.actor, T0, kind="assignment",
                             title="З", grading="points", max_points=bad)
        made = items.create_item(self.s, self.actor, T0, kind="assignment", title="З", grading="points",
                                 max_points="5.5")
        self.assertEqual(made.max_points, Decimal("5.5"))

    def test_work_card_addressee_without_work_and_strangers(self):
        item = self.assignment(due_at=T0 - US)  # past due: an addressee without hand-ins is «не сдано»
        other = self.assignment()
        # an addressee without work: status assigned, no events, missing computed
        card = works.work_card(self.s, self.blobs, item, self.a, T0)
        self.assertEqual((card["work_id"], card["status"], card["events"], card["missing"]),
                         (None, "assigned", [], True))
        self.assertEqual(card["student_sees"]["status"], None)
        self.assertEqual(self.s.execute(select(func.count()).select_from(AssignmentWork)).scalar(), 0)
        # not addressed and no work: None (the API answers 404 work_not_found)
        self.assertIsNone(works.work_card(self.s, self.blobs, item, self.c, T0))
        # a work on another item does not make a card on this one
        items.add_audience(self.s, self.actor, other, T0, users=[self.c])
        self.submit(self.c, other, text="ответ C")
        self.s.commit()
        self.assertIsNone(works.work_card(self.s, self.blobs, item, self.c, T0))
        # grading the addressee without work from the card works the usual way
        works.grade_work(self.s, self.actor, item, self.a, T0, score=0, comment="не сдано")
        self.s.commit()
        card = works.work_card(self.s, self.blobs, item, self.a, T0)
        self.assertEqual((card["status"], [e["action"] for e in card["events"]], card["missing"]),
                         ("graded", ["graded"], True))
        # a student removed from the group keeps the card of their work
        g.remove_members(self.s, self.actor, self.group, [self.a], T0)
        self.s.commit()
        self.assertEqual(works.work_card(self.s, self.blobs, item, self.a, T0)["status"], "graded")

    def test_review_queue_by_item_and_across_items(self):
        item = self.assignment(users=[self.c], due_at=DUE)
        other = self.assignment()
        self.submit(self.a, item, now=DUE + US)  # late
        self.submit(self.b, other)
        works.grade_work(self.s, self.actor, other, self.b, T0, score=5)
        self.s.commit()
        after = DUE + timedelta(hours=1)

        def page(**kw):
            return works.review_queue(self.s, after, **kw)

        rows = page(item=item)["items"]
        self.assertEqual([(r["login"], r["status"], r["late"], r["missing"]) for r in rows],
                         [("student3", "assigned", False, True), ("student2", "assigned", False, True),
                          ("student1", "submitted", True, False)])
        self.assertEqual([r["login"] for r in page(item=item, missing=True)["items"]], ["student3", "student2"])
        self.assertEqual([r["login"] for r in page(item=item, late=True)["items"]], ["student1"])
        self.assertEqual([r["login"] for r in page(item=item, group=self.group)["items"]], ["student2", "student1"])
        self.assertEqual([r["login"] for r in page(item=item, status="assigned")["items"]], ["student3", "student2"])
        # an extension takes the student out of «не сдано»
        items.add_extension(self.s, self.actor, item, after + timedelta(days=1), T0, user=self.b)
        self.s.commit()
        self.assertEqual([r["login"] for r in page(item=item, missing=True)["items"]], ["student3"])
        # cursor by user id
        first = page(item=item, limit=2)
        self.assertEqual(([r["user_id"] for r in first["items"]], first["next_cursor"]),
                         ([self.c.id, self.b.id], self.b.id))
        rest = page(item=item, limit=2, cursor=first["next_cursor"])
        self.assertEqual(([r["user_id"] for r in rest["items"]], rest["next_cursor"]), ([self.a.id], None))
        # across items: only works, newest first, cursor by work id
        rows = page()["items"]
        self.assertEqual([(r["item"], r["login"], r["status"]) for r in rows],
                         [(other.id, "student2", "graded"), (item.id, "student1", "submitted")])
        self.assertEqual(rows[0]["grade"], {"score": Decimal(5), "passed": None, "released": False})
        self.assertEqual([r["login"] for r in page(status="submitted")["items"]], ["student1"])
        self.assertEqual([r["login"] for r in page(late=True)["items"]], ["student1"])
        self.assertEqual([r["login"] for r in page(late=False)["items"]], ["student2"])  # its hand-in was on time
        self.assertEqual(page(status="assigned")["items"], [])
        first = page(limit=1)
        self.assertEqual(page(limit=1, cursor=first["next_cursor"])["items"][0]["login"], "student1")
        with self.assertRaises(FeedError) as caught:
            page(missing=True)
        self.assertEqual(caught.exception.code, "missing_needs_item")
        works.release_grades(self.s, self.actor, other, T0)
        self.s.commit()
        rows = {r["login"]: r for r in page(item=other)["items"]}
        self.assertEqual((rows["student2"]["grade"]["released"], rows["student1"]["grade"]), (True, None))

    # ---------------------------------------------------------------- groups and users
    def test_groups_rollback_archive_and_user_traces_know_the_feed(self):
        lone = g.create(self.s, self.actor, "9Г", T0)
        self.s.commit()
        self.assertIsNone(g.why_kept_after_rollback(self.s, lone))
        item = self.assignment(groups=[lone], users=[self.c])
        self.assertEqual(g.why_kept_after_rollback(self.s, lone), "it is addressed by a feed item")
        self.assertEqual([i.id for i in g.feed_items_of(self.s, lone)], [item.id])
        g.archive(self.s, self.actor, lone, T0)
        self.s.commit()
        details = self.s.execute(select(AuditLog.details).where(AuditLog.action == "group.archive")).scalar_one()
        self.assertEqual(details["feed_items"], [item.id])
        self.submit(self.c, item)
        works.grade_work(self.s, self.actor, item, self.c, T0, score=1)
        self.s.commit()
        self.assertEqual({k: v for k, v in accounts.traces(self.s, self.c).items()},
                         {"feed items addressed personally": 1, "assignment works": 1})
        admin_traces = accounts.traces(self.s, self.seed["admin"])
        for label in ("feed items created", "feed addressees added", "assignment reviews"):
            self.assertIn(label, admin_traces)
        with self.assertRaises(accounts.UserError):
            accounts.delete_user(self.s, self.actor, self.c, T0)

    # ---------------------------------------------------------------- dev_seed --item
    def test_dev_seed_hands_in_work_through_the_rules(self):
        def seed(*argv, dev="1"):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"JUDGE_DEV": dev, "JUDGE_BLOB_DIR": str(self.blobs.root)}), \
                    mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dev_seed.main(list(argv))
            return code, out.getvalue(), err.getvalue()

        student = self.s.execute(select(User).where(User.login == "dev-student-1")).scalar_one_or_none()
        self.assertIsNone(student)
        dev = User(login="dev-student-1", password_hash="!", display_name="Dev 1", role=Role.student)
        self.s.add(dev)
        self.s.flush()
        item = self.assignment(groups=[], users=[dev], due_at=None)
        answer = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8")
        answer.write("ответ из файла")
        answer.close()
        self.addCleanup(os.unlink, answer.name)
        code, _out, err = seed("--item", str(item.id), "--text-file", answer.name, dev="0")
        self.assertEqual(code, 2)
        self.assertIn("JUDGE_DEV=1", err)
        code, out, err = seed("--item", str(item.id), "--text-file", answer.name)
        self.assertEqual(code, 0, err)
        made = json.loads(out)
        self.assertEqual((made["status"], made["late"], made["login"]), ("submitted", False, "dev-student-1"))
        self.s.expire_all()
        sub = self.s.get(AssignmentSubmission, made["submission_id"])
        self.assertEqual(works.answer_text(self.s, self.blobs, sub), "ответ из файла")
        code, _out, err = seed("--item", str(item.id), "--user", "2", "--text-file", answer.name)
        self.assertEqual(code, 1)
        self.assertIn("refused: no_access", err)
