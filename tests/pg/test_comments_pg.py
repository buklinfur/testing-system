"""Services of comment threads (classroom/comments.py; D-072, D-074, D-075, docs/CLASSROOM.md §9) on a real
PostgreSQL: threads made only by writing, two first comments in two connections, the rate under concurrency, identity
(student A never reads, writes, deletes or marks in B's thread or outside their feed), unread counters and marks that
only grow, «awaiting reply», archived items and groups, removed students, the audit, comments_enabled of items.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import inspect
import threading
import time
from datetime import datetime, timedelta, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
S = timedelta(seconds=1)

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import groups as g
    from app.auth import audit
    from app.classroom import comments, items, works
    from app.classroom.items import FeedError
    from app.models import AuditLog, Comment, CommentThread, Contest, Role, ThreadRead, User


@requires_pg
class CommentServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        self.admin = self.seed["admin"]
        self.actor = audit.Actor(user_id=self.admin.id)
        self.console = audit.Actor(cli="cli:teacher")
        self.a, self.b, self.c = self.student(1), self.student(2), self.student(3)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.a, self.b], T0)
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def assignment(self, groups=None, users=(), publish=True, comments_enabled=True, **fields):
        base = dict(grading="points", max_points=10, due_at=DUE, comments_enabled=comments_enabled)
        base.update(fields)
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Схема", body_md="Нарисуй", **base)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group] if groups is None else groups,
                           users=list(users))
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def contest_item(self):
        contest = self.s.get(Contest, self.seed["cp"].contest_id)
        item = items.create_item(self.s, self.actor, T0, kind="contest", title="Контест", contest=contest)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(FeedError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.s.rollback()

    def threads(self):
        return self.s.execute(select(func.count()).select_from(CommentThread)).scalar_one()

    def ids(self, view):
        return [c["id"] for c in view["comments"]]

    def post(self, fn, *args, **kw):
        comment = fn(self.s, *args, **kw)
        self.s.commit()
        return comment

    def run_parallel(self, *jobs, hold=0.2):
        """Each job(session) in its own connection, released at once by a barrier; a job that succeeds keeps its
        transaction open `hold` seconds before committing. The results or the FeedError codes, in job order."""
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

        workers = [threading.Thread(target=run, args=(i, job)) for i, job in enumerate(jobs)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(30)
        self.assertEqual(errors, [])
        self.s.expire_all()
        return results

    # ---------------------------------------------------------------- threads come only from writing
    def test_reading_marking_and_counting_never_create_a_thread(self):
        item = self.assignment()
        readers = [
            lambda: comments.shared_thread(self.s, self.a, item, T0),
            lambda: comments.private_thread(self.s, self.a, item, self.a, T0),
            lambda: comments.private_thread(self.s, self.admin, item, self.b, T0),
            lambda: comments.shared_thread(self.s, comments.CONSOLE, item, T0),
            lambda: comments.threads_query(self.s, self.admin, T0),
            lambda: comments.threads_query(self.s, comments.CONSOLE, T0, awaiting=True),
            lambda: comments.unread_by_item(self.s, self.a, [item], T0),
        ]
        for read in readers:
            read()
        self.assertEqual(comments.mark_read(self.s, self.a, item, T0), 0)
        self.assertEqual(comments.mark_read(self.s, self.a, item, T0, student=self.a), 0)
        self.assertEqual(comments.mark_read(self.s, self.admin, item, T0, student=self.b), 0)
        self.assertEqual(comments.unread_count(self.s, self.a, item, T0), 0)
        self.s.commit()
        self.assertEqual(self.threads(), 0)
        self.assertEqual(self.s.execute(select(func.count()).select_from(ThreadRead)).scalar_one(), 0)
        view = comments.shared_thread(self.s, self.a, item, T0)
        self.assertEqual((view["thread_id"], view["comments"], view["can_post"], view["unread"]), (None, [], True, 0))

    def test_a_refused_comment_creates_no_thread(self):
        item = self.assignment(comments_enabled=False)
        self.refused("comments_disabled", comments.post_shared, self.s, self.a, item, T0, "вопрос")
        self.refused("comment_required", comments.post_private, self.s, self.a, item, T0, "   ")
        self.refused("comment_required", comments.teacher_post, self.s, self.actor, item, T0, "​",
                     student=self.a)
        self.assertEqual(self.threads(), 0)

    def test_two_first_comments_in_two_connections_make_one_thread(self):
        item = self.assignment()
        shared = self.run_parallel(
            lambda s: comments.post_shared(s, s.get(User, self.a.id), s.get(type(item), item.id), T0, "раз").id,
            lambda s: comments.post_shared(s, s.get(User, self.b.id), s.get(type(item), item.id), T0, "два").id,
            lambda s: comments.teacher_post(s, self.actor, s.get(type(item), item.id), T0, "три").id)
        private = self.run_parallel(
            lambda s: comments.post_private(s, s.get(User, self.a.id), s.get(type(item), item.id), T0, "вопрос").id,
            lambda s: comments.teacher_post(s, self.actor, s.get(type(item), item.id), T0, "ответ",
                                            student=s.get(User, self.a.id)).id)
        self.assertTrue(all(isinstance(r, int) for r in shared + private), shared + private)
        rows = self.s.execute(select(CommentThread.student_id, func.count(Comment.id))
                              .join(Comment, Comment.thread_id == CommentThread.id)
                              .group_by(CommentThread.id, CommentThread.student_id)).all()
        self.assertEqual(sorted(rows, key=lambda r: r[0] or 0), [(None, 3), (self.a.id, 2)])

    # ---------------------------------------------------------------- the rate
    def test_limit_plus_one_comments_at_once_accept_exactly_limit(self):
        item, limit = self.assignment(), 3

        def job(s):
            return comments.post_shared(s, s.get(User, self.a.id), s.get(type(item), item.id), T0, "флуд",
                                        limit=limit).id

        results = self.run_parallel(*[job] * (limit + 1), hold=0.1)
        self.assertEqual(sorted(r for r in results if isinstance(r, str)), ["rate_limited"])
        self.assertEqual(len([r for r in results if isinstance(r, int)]), limit)
        self.assertEqual(self.s.execute(select(func.count()).select_from(Comment)).scalar_one(), limit)

    def test_the_rate_counts_deleted_comments_in_the_clock_of_the_request(self):
        item = self.assignment()
        first = self.post(comments.post_private, self.a, item, T0, "раз", limit=2)
        self.assertEqual(first.created_at, T0)  # not the database clock: the window and the row agree
        self.post(comments.delete_own, self.a, first.id, T0 + S)
        self.post(comments.post_shared, self.a, item, T0 + S, "два", limit=2)
        self.refused("rate_limited", comments.post_shared, self.s, self.a, item, T0 + 2 * S, "три", limit=2)
        self.refused("rate_limited", comments.post_private, self.s, self.a, item, T0 + 59 * S, "три", limit=2)
        self.post(comments.post_private, self.a, item, T0 + 60 * S, "три", limit=2)  # the first left the window
        self.post(comments.post_shared, self.b, item, T0 + 2 * S, "другой ученик", limit=2)
        for _ in range(5):
            self.post(comments.teacher_post, self.actor, item, T0 + 2 * S, "учитель без лимита")
        self.post(comments.post_shared, self.a, item, T0 + 3 * S, "без лимита", limit=0)

    # ---------------------------------------------------------------- identity
    def test_student_functions_take_no_student_id(self):
        for fn in (comments.post_shared, comments.post_private, comments.delete_own):
            names = list(inspect.signature(fn).parameters)
            self.assertNotIn("student", names, fn.__name__)
            self.assertFalse(any(n.endswith("user_id") or n == "student_id" for n in names), fn.__name__)

    def test_student_a_cannot_reach_the_private_thread_of_b(self):
        item = self.assignment()
        question = self.post(comments.post_private, self.b, item, T0, "вопрос B")
        self.refused("thread_not_found", comments.private_thread, self.s, self.a, item, self.b, T0)
        self.refused("thread_not_found", comments.mark_read, self.s, self.a, item, T0, student=self.b)
        self.refused("thread_not_found", comments.mark_read, self.s, self.a, item, T0, student=self.b,
                     up_to=question.id)
        self.refused("thread_not_found", comments.unread_count, self.s, self.a, item, T0, student=self.b)
        self.refused("comment_not_found", comments.delete_own, self.s, self.a, question.id, T0)
        self.assertEqual(comments.unread_by_item(self.s, self.a, [item], T0), {item.id: 0})
        self.post(comments.post_private, self.a, item, T0, "вопрос A")  # A's own thread, never B's
        self.assertEqual(comments.private_thread(self.s, self.a, item, self.a, T0)["comments"][0]["body"],
                         "вопрос A")
        self.assertEqual(self.ids(comments.private_thread(self.s, self.admin, item, self.b, T0)), [question.id])
        self.assertIsNone(self.s.get(Comment, question.id).deleted_at)
        self.assertEqual(self.s.execute(select(func.count()).select_from(ThreadRead)).scalar_one(), 0)

    def test_student_outside_the_audience_cannot_reach_the_thread_of_the_item(self):
        item = self.assignment()
        said = self.post(comments.post_shared, self.a, item, T0, "в группе")
        c = self.c  # not in the group
        self.refused("thread_not_found", comments.shared_thread, self.s, c, item, T0)
        self.refused("no_access", comments.post_shared, self.s, c, item, T0, "чужой")
        self.refused("no_access", comments.post_private, self.s, c, item, T0, "чужой")
        self.refused("comment_not_found", comments.delete_own, self.s, c, said.id, T0)
        self.refused("thread_not_found", comments.mark_read, self.s, c, item, T0)
        self.refused("thread_not_found", comments.mark_read, self.s, c, item, T0, up_to=said.id)
        self.refused("thread_not_found", comments.private_thread, self.s, c, item, c, T0)
        self.assertEqual(comments.unread_by_item(self.s, c, [item], T0), {item.id: 0})
        # B sees A's comment but cannot delete it; a missing one answers the same as an unseen one
        self.refused("not_your_comment", comments.delete_own, self.s, self.b, said.id, T0)
        self.refused("comment_not_found", comments.delete_own, self.s, self.b, said.id + 1000, T0)
        self.assertEqual(self.threads(), 1)

    def test_a_student_object_is_not_a_teacher_and_a_teacher_has_no_student_path(self):
        item = self.assignment()
        self.refused("not_a_student", comments.post_shared, self.s, self.admin, item, T0, "админ как ученик")
        self.refused("not_a_student", comments.delete_own, self.s, self.admin, 1, T0)
        with self.assertRaises(ValueError):
            comments.teacher_post(self.s, audit.Actor(user_id=self.a.id), item, T0, "ученик как учитель")
        self.s.rollback()
        self.refused("not_a_student", comments.teacher_post, self.s, self.actor, item, T0, "админу", student=self.admin)

    # ---------------------------------------------------------------- what is seen
    def test_students_see_display_names_teachers_see_everything(self):
        item = self.assignment()
        mine = self.post(comments.post_shared, self.a, item, T0, "вопрос")
        console = self.post(comments.teacher_post, self.console, item, T0, "ответ из консоли")
        web = self.post(comments.teacher_post, self.actor, item, T0, "ответ")
        gone = self.post(comments.post_shared, self.b, item, T0, "лишнее")
        self.post(comments.delete_comment, self.actor, gone.id, T0 + S, "  не по теме  ")
        student = comments.shared_thread(self.s, self.a, item, T0)
        self.assertEqual(self.ids(student), [mine.id, console.id, web.id])
        self.assertEqual([c["author"] for c in student["comments"]],
                         [{"display_name": "S1", "teacher": False}, {"display_name": "Преподаватель", "teacher": True},
                          {"display_name": "A", "teacher": True}])
        self.assertEqual([c["mine"] for c in student["comments"]], [True, False, False])
        self.assertNotIn("login", repr(student))
        teacher = comments.shared_thread(self.s, self.admin, item, T0)
        self.assertEqual(self.ids(teacher), [mine.id, console.id, web.id, gone.id])
        last = teacher["comments"][-1]
        self.assertEqual((last["deleted"], last["delete_reason"], last["deleted_by"]["login"], last["body"]),
                         (True, "не по теме", "admin", "лишнее"))
        self.assertEqual(teacher["comments"][1]["author"]["console"], True)
        self.assertIsNone(comments.shared_thread(self.s, comments.CONSOLE, item, T0)["unread"])

    def test_pages_of_a_thread(self):
        item = self.assignment()
        posted = [self.post(comments.teacher_post, self.actor, item, T0, f"№{i}").id for i in range(5)]
        page = comments.shared_thread(self.s, self.a, item, T0, limit=2)
        self.assertEqual((self.ids(page), page["next_after"]), (posted[:2], posted[1]))
        page = comments.shared_thread(self.s, self.a, item, T0, after=posted[3], limit=2)
        self.assertEqual((self.ids(page), page["next_after"]), (posted[4:], None))

    # ---------------------------------------------------------------- unread and marks
    def test_unread_skips_deleted_and_own_and_the_mark_only_grows(self):
        item = self.assignment()
        own = self.post(comments.post_shared, self.a, item, T0, "моё")
        t1 = self.post(comments.teacher_post, self.actor, item, T0, "1")
        t2 = self.post(comments.teacher_post, self.console, item, T0, "2")
        t3 = self.post(comments.teacher_post, self.actor, item, T0, "3")
        self.post(comments.delete_comment, self.actor, t3.id, T0)
        self.assertEqual(comments.unread_count(self.s, self.a, item, T0), 2)  # not own, not deleted
        self.assertEqual(comments.unread_by_item(self.s, self.a, [item], T0), {item.id: 2})
        self.assertEqual(comments.unread_count(self.s, self.admin, item, T0), 2)  # A's and the console's
        self.assertEqual(comments.mark_read(self.s, self.a, item, T0, up_to=t2.id), 0)
        self.assertEqual(comments.mark_read(self.s, self.a, item, T0, up_to=t1.id), 0)  # cannot go back
        self.s.commit()
        mark = select(ThreadRead.last_read_comment_id).where(ThreadRead.user_id == self.a.id)
        self.assertEqual(self.s.execute(mark).scalar_one(), t2.id)
        self.refused("comment_not_found", comments.mark_read, self.s, self.a, item, T0, up_to=t3.id)  # deleted
        self.refused("comment_not_found", comments.mark_read, self.s, self.a, item, T0, up_to=own.id + 1000)
        other = self.assignment()
        elsewhere = self.post(comments.teacher_post, self.actor, other, T0, "в другой ветке")
        self.refused("comment_not_found", comments.mark_read, self.s, self.a, item, T0, up_to=elsewhere.id)
        self.assertEqual(comments.mark_read(self.s, self.admin, item, T0), 0)  # a teacher marks the deleted too
        self.s.commit()
        self.assertEqual(self.s.execute(select(ThreadRead.last_read_comment_id)
                                        .where(ThreadRead.user_id == self.admin.id)).scalar_one(), t3.id)
        self.assertEqual(comments.unread_count(self.s, self.b, item, T0), 3)  # each reader has their own mark
        t4 = self.post(comments.teacher_post, self.actor, item, T0, "4")
        self.assertEqual(comments.unread_count(self.s, self.a, item, T0), 1)
        self.assertEqual(comments.mark_read(self.s, self.a, item, T0), 0)
        self.assertEqual(self.s.execute(mark).scalar_one(), t4.id)

    def test_the_console_has_no_read_marks(self):
        item = self.assignment()
        self.post(comments.post_shared, self.a, item, T0, "вопрос")
        for fn in (lambda: comments.mark_read(self.s, comments.CONSOLE, item, T0),
                   lambda: comments.unread_count(self.s, comments.CONSOLE, item, T0),
                   lambda: comments.threads_query(self.s, comments.CONSOLE, T0, unread=True)):
            with self.assertRaises(ValueError):
                fn()
        row = comments.threads_query(self.s, comments.CONSOLE, T0)["items"][0]
        self.assertIsNone(row["unread"])

    def test_unread_by_item_counts_the_shared_and_the_own_private_thread(self):
        item, other = self.assignment(), self.assignment()
        self.post(comments.teacher_post, self.actor, item, T0, "всем")
        self.post(comments.teacher_post, self.actor, item, T0, "лично A", student=self.a)
        self.post(comments.teacher_post, self.actor, item, T0, "лично B", student=self.b)
        self.post(comments.teacher_post, self.actor, other, T0, "в другом")
        self.assertEqual(comments.unread_by_item(self.s, self.a, [item, other], T0), {item.id: 2, other.id: 1})
        self.assertEqual(comments.unread_by_item(self.s, self.c, [item, other], T0), {item.id: 0, other.id: 0})

    # ---------------------------------------------------------------- the inbox
    def test_awaiting_reply_follows_the_last_comment_not_deleted(self):
        item = self.assignment()
        self.post(comments.post_private, self.a, item, T0, "вопрос")

        def awaiting():
            return [(r["student"]["login"] if r["student"] else None)
                    for r in comments.threads_query(self.s, self.admin, T0, awaiting=True)["items"]]

        self.assertEqual(awaiting(), ["student1"])
        reply = self.post(comments.teacher_post, self.actor, item, T0, "ответ", student=self.a)
        self.assertEqual(awaiting(), [])
        self.post(comments.delete_comment, self.actor, reply.id, T0)
        self.assertEqual(awaiting(), ["student1"])  # the answer is gone: the question waits again
        self.post(comments.post_shared, self.b, item, T0, "в общей")
        self.assertEqual(awaiting(), [None, "student1"])
        question = self.post(comments.post_shared, self.b, item, T0, "ещё")
        self.post(comments.delete_own, self.b, question.id, T0)
        self.assertEqual(awaiting(), [None, "student1"])
        self.post(comments.teacher_post, self.console, item, T0, "из консоли")
        self.assertEqual(awaiting(), ["student1"])

    def test_inbox_filters_and_pages(self):
        first, second = self.assignment(), self.assignment(groups=[], users=[self.c])
        self.post(comments.post_private, self.a, first, T0, "A")
        self.post(comments.post_private, self.c, second, T0, "C")
        self.post(comments.teacher_post, self.actor, first, T0, "всем")
        self.post(comments.post_shared, self.c, second, T0, "C всем")
        rows = comments.threads_query(self.s, self.admin, T0)["items"]
        self.assertEqual([(r["item"]["id"], r["kind"]) for r in rows],
                         [(second.id, "shared"), (first.id, "shared"), (second.id, "private"), (first.id, "private")])
        group = comments.threads_query(self.s, self.admin, T0, group=self.group)["items"]
        self.assertEqual([(r["item"]["id"], r["kind"]) for r in group], [(first.id, "shared"), (first.id, "private")])
        one = comments.threads_query(self.s, self.admin, T0, item=second)["items"]
        self.assertEqual({r["item"]["id"] for r in one}, {second.id})
        unread = comments.threads_query(self.s, self.admin, T0, unread=True)["items"]
        self.assertEqual(len(unread), 3)  # the shared thread of `first` has only the admin's own comment
        comments.mark_read(self.s, self.admin, second, T0, student=self.c)
        self.assertEqual(len(comments.threads_query(self.s, self.admin, T0, unread=True)["items"]), 2)
        page = comments.threads_query(self.s, self.admin, T0, limit=3)
        rest = comments.threads_query(self.s, self.admin, T0, limit=3, cursor=page["next_cursor"])
        self.assertEqual([r["thread_id"] for r in page["items"] + rest["items"]], [r["thread_id"] for r in rows])
        self.assertIsNone(rest["next_cursor"])

    # ---------------------------------------------------------------- archives, removed students
    def test_archived_item(self):
        item = self.assignment()
        shared = self.post(comments.post_shared, self.a, item, T0, "общая")
        self.post(comments.post_private, self.a, item, T0, "личная")
        items.archive(self.s, self.actor, item, T0)
        self.s.commit()
        self.refused("thread_not_found", comments.shared_thread, self.s, self.a, item, T0)
        self.refused("item_archived", comments.post_shared, self.s, self.a, item, T0, "ещё")
        self.refused("item_archived", comments.post_private, self.s, self.a, item, T0, "ещё")
        self.refused("comment_not_found", comments.delete_own, self.s, self.a, shared.id, T0)
        view = comments.private_thread(self.s, self.a, item, self.a, T0)  # their own thread stays
        self.assertEqual((len(view["comments"]), view["can_post"], view["cannot_post"]), (1, False, "item_archived"))
        self.post(comments.teacher_post, self.actor, item, T0, "учитель пишет в архиве", student=self.a)
        self.post(comments.teacher_post, self.actor, item, T0, "и в общую")

    def test_archived_group_and_removed_student(self):
        item = self.assignment()
        self.post(comments.post_private, self.a, item, T0, "A до")
        self.post(comments.post_private, self.b, item, T0, "B до")
        g.remove_members(self.s, self.actor, self.group, [self.a], T0)
        self.s.commit()
        for student in (self.a,):
            self.refused("thread_not_found", comments.shared_thread, self.s, student, item, T0)
            self.refused("no_access", comments.post_shared, self.s, student, item, T0, "после")
            self.refused("no_access", comments.post_private, self.s, student, item, T0, "после")
            self.assertEqual(len(comments.private_thread(self.s, student, item, student, T0)["comments"]), 1)
        self.post(comments.post_shared, self.b, item, T0, "B в общей")
        g.archive(self.s, self.actor, self.group, T0)
        self.s.commit()
        self.refused("thread_not_found", comments.shared_thread, self.s, self.b, item, T0)
        self.refused("no_access", comments.post_shared, self.s, self.b, item, T0, "после архива")
        self.refused("no_access", comments.post_private, self.s, self.b, item, T0, "после архива")
        self.assertEqual(len(comments.private_thread(self.s, self.b, item, self.b, T0)["comments"]), 1)
        # the teacher still answers a student with a thread, and reads it
        self.post(comments.teacher_post, self.actor, item, T0, "ответ", student=self.a)
        self.assertEqual(len(comments.private_thread(self.s, self.admin, item, self.a, T0)["comments"]), 2)
        # a student without thread or work, not addressed: nothing for the teacher either
        self.refused("thread_not_found", comments.private_thread, self.s, self.admin, item, self.c, T0)
        self.refused("not_addressed", comments.teacher_post, self.s, self.actor, item, T0, "?", student=self.c)

    def test_the_thread_of_a_contest_item_is_the_teachers(self):
        item = self.contest_item()
        self.post(comments.teacher_post, self.actor, item, T0, "контест начинается")
        self.assertEqual(len(comments.shared_thread(self.s, self.a, item, T0)["comments"]), 1)
        self.refused("comments_teacher_only", comments.post_shared, self.s, self.a, item, T0, "как решать A?")
        self.refused("not_an_assignment", comments.post_private, self.s, self.a, item, T0, "лично")
        self.refused("thread_not_found", comments.private_thread, self.s, self.admin, item, self.a, T0)

    def test_a_draft_takes_no_comments(self):
        item = self.assignment(publish=False)
        self.refused("not_published", comments.teacher_post, self.s, self.actor, item, T0, "черновик")
        self.refused("not_published", comments.post_shared, self.s, self.a, item, T0, "черновик")

    # ---------------------------------------------------------------- audit
    def test_audit_has_the_teachers_without_the_text(self):
        item = self.assignment()
        student_said = self.post(comments.post_shared, self.a, item, T0, "секрет ученика")
        teacher_said = self.post(comments.teacher_post, self.actor, item, T0, "секрет учителя", student=self.a)
        self.post(comments.teacher_post, self.console, item, T0, "из консоли")
        self.post(comments.delete_comment, self.actor, student_said.id, T0, "спам")
        own = self.post(comments.post_shared, self.b, item, T0, "передумал")
        self.post(comments.delete_own, self.b, own.id, T0 + S)
        rows = self.s.execute(select(AuditLog).where(AuditLog.action.like("comment.%")).order_by(AuditLog.id)).scalars()
        rows = list(rows)
        self.assertEqual([(r.action, r.actor_user_id, r.actor_cli) for r in rows],
                         [("comment.post", self.admin.id, None), ("comment.post", None, "cli:teacher"),
                          ("comment.delete", self.admin.id, None)])
        self.assertEqual(rows[0].details, {"item": item.id, "thread": "private", "student": "student1",
                                           "length": len("секрет учителя")})
        self.assertEqual(rows[0].object_id, str(teacher_said.id))
        self.assertEqual(rows[2].details, {"item": item.id, "thread": "shared", "student": None, "author": "student1",
                                           "reason": "спам"})
        self.assertNotIn("секрет", repr([r.details for r in rows]))
        self.s.refresh(own)
        self.assertEqual((own.deleted_at, own.deleted_by, own.delete_reason), (T0 + S, self.b.id, None))
        self.refused("already_deleted", comments.delete_comment, self.s, self.actor, own.id, T0)
        self.refused("comment_not_found", comments.delete_comment, self.s, self.actor, own.id + 1000, T0)
        self.refused("reason_too_long", comments.delete_comment, self.s, self.actor, teacher_said.id, T0, "x" * 201)
        self.post(comments.delete_comment, self.console, teacher_said.id, T0)
        self.s.refresh(teacher_said)
        self.assertEqual((teacher_said.deleted_by, teacher_said.delete_reason), (None, None))

    # ---------------------------------------------------------------- comments_enabled of an item
    def test_comments_enabled_is_an_audited_field_of_assignments(self):
        item = self.assignment(comments_enabled=False)
        self.assertEqual(items.update_item(self.s, self.actor, item, T0, comments_enabled=True), ["comments_enabled"])
        self.s.commit()
        row = self.s.execute(select(AuditLog).where(AuditLog.action == "item.update")).scalars().all()[-1]
        self.assertEqual(row.details["changes"], {"comments_enabled": [False, True]})
        self.assertIs(items.item_detail(self.s, item)["comments_enabled"], True)
        self.assertIs(items.item_summary(self.s, item)["comments_enabled"], True)
        self.refused("item_invalid", items.update_item, self.s, self.actor, item, T0, comments_enabled="yes")
        contest = self.contest_item()
        self.assertIs(items.item_summary(self.s, contest)["comments_enabled"], False)
        self.refused("item_invalid", items.update_item, self.s, self.actor, contest, T0, comments_enabled=True)
        self.assertEqual(items.update_item(self.s, self.actor, contest, T0, comments_enabled=False), [])

    def test_works_and_comments_share_nothing_but_the_pair(self):
        item = self.assignment()
        self.assertEqual(self.s.execute(select(func.count()).select_from(works.AssignmentWork)).scalar_one(), 0)
        self.post(comments.post_private, self.a, item, T0, "до сдачи")
        self.assertEqual(self.s.execute(select(func.count()).select_from(works.AssignmentWork)).scalar_one(), 0)
        self.assertEqual(self.s.get(User, self.a.id).role, Role.student)
