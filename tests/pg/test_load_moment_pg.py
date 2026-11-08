"""The load snapshot reads one moment of the database (queue.read_one_moment, D-067, docs/QUEUE.md) on a real
PostgreSQL:

* the mark of the moment is bound to the transaction: without read_one_moment, after a commit or a rollback, or when
  the transaction had already read, the snapshot functions refuse;
* every time in the snapshot is the database's now() of that transaction, never the clock of this process;
* a deterministic interleaving - a second connection commits a claim, a finish or new submissions between the
  queries of one snapshot - keeps the invariants (queue + in flight == total, busy <= in flight, a place within the
  queue), and the same scenario without REPEATABLE READ breaks them (the test has teeth);
* under a thread that keeps changing the queue, 200 snapshots keep the invariants;
* the outputs that show the snapshot keep their keys.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import ast
import contextlib
import io
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import event, func, select, text

    from app import queue as q
    from app import system
    from app.auth import audit
    from app.models import Judgement, Submission, Worker
    from app.settings import Settings

try:
    from fastapi.testclient import TestClient

    from app.internal_main import create_internal_app
    HAVE_API = True
except ImportError:  # pragma: no cover
    HAVE_API = False

# the keys of the outputs before session 4b.0 (captured from the code of 28682a9): they must not change
LOAD_KEYS = {"level", "my_position", "my_wait_seconds", "queue_length", "typical_seconds", "workers_busy",
             "workers_online"}
METRICS_KEYS = {"checks", "finished_by_verdict", "in_flight", "median_wait_s", "oldest_queued_age_s", "queue_length",
                "requeued_total", "typical_seconds", "workers_by_state", "workers_online"}
WORKER_KEYS = {"capabilities", "check_id", "heartbeat_age_s", "id", "info", "judgement_id", "name", "state", "stats"}
SYSTEM_KEYS = {"checks", "config_problems", "load", "metrics", "migration", "timezone", "workers", "workers_known"}
PROMETHEUS = {"judge_finished_total", "judge_in_flight", "judge_problem_checks", "judge_queue_length",
              "judge_queue_oldest_age_seconds", "judge_queue_wait_seconds", "judge_requeued_total",
              "judge_typical_judge_seconds", "judge_workers"}
JUDGECTL_STATUS = {"blobs", "checks", "config_problems", "database", "migration", "migration_head", "queue", "timezone",
                   "workers_known"}
SNAPSHOT_FUNCTIONS = ("_moment", "load_snapshot", "collect_metrics", "worker_table", "_workers_online",
                      "_recent_durations", "queue_position")
PROCESS_CLOCKS = {("_now",), ("datetime", "now"), ("datetime", "utcnow"), ("time", "time"), ("time", "monotonic")}


def without_repeatable_read(session):
    """The control: the same mark as read_one_moment, but the transaction stays READ COMMITTED."""
    session.connection()
    session.info[q.MOMENT] = (session.get_transaction(), session.execute(select(func.now())).scalar_one())


class SnapshotUsesNoProcessClockTests(unittest.TestCase):
    """Every time in the snapshot comes from the database: no clock of this process in its functions."""

    def test_no_process_clock_in_the_snapshot_functions(self):
        tree = ast.parse((ROOT / "backend" / "app" / "queue.py").read_text(encoding="utf-8"))
        found = {}
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in SNAPSHOT_FUNCTIONS:
                calls = set()
                for call in ast.walk(node):
                    if isinstance(call, ast.Call):
                        f = call.func
                        if isinstance(f, ast.Name):
                            calls.add((f.id,))
                        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                            calls.add((f.value.id, f.attr))
                found[node.name] = sorted(calls & PROCESS_CLOCKS)
        self.assertEqual(set(found), set(SNAPSHOT_FUNCTIONS), "every snapshot function was found")
        self.assertEqual({k: v for k, v in found.items() if v}, {})


@requires_pg
class LoadMomentTests(PgCase):
    # ---------------------------------------------------------------- helpers
    def worker(self, name="w0"):
        _token, worker = self.token_and_worker(name)
        return worker

    def other(self, fn):
        """fn(session) in a connection of its own, committed: what another request does meanwhile."""
        with self.Session() as o:
            out = fn(o)
            o.commit()
        return out

    def submit_in(self, o, user_id):
        sub = Submission(user_id=user_id, contest_problem_id=self.seed["cp"].id, language_key="python",
                         source_sha256="c" * 64)
        o.add(sub)
        o.flush()
        return q.enqueue(o, sub)

    @contextlib.contextmanager
    def after_statement(self, n, action):
        """Run action() once, right after the n-th statement this session executes from here on."""
        conn = self.s.connection()
        count = [0]

        def listener(*_args):
            count[0] += 1
            if count[0] == n:
                action()

        event.listen(conn, "after_cursor_execute", listener)
        try:
            yield
        finally:
            event.remove(conn, "after_cursor_execute", listener)

    def total_and_done(self):
        total = self.s.execute(select(func.count()).select_from(Judgement)).scalar_one()
        done = self.s.execute(select(func.count()).select_from(Judgement).where(Judgement.status.in_(q.DONE))).scalar_one()
        return total, done

    # ---------------------------------------------------------------- the mark of the moment
    def test_the_snapshot_functions_refuse_without_the_mark(self):
        self.worker()
        self.s.commit()
        for fn in (q.load_snapshot, q.collect_metrics, q.worker_table):
            with self.subTest(fn=fn.__name__), self.assertRaises(q.QueueError):
                fn(self.s, self.settings)
            self.s.rollback()

    def test_read_one_moment_must_start_the_transaction(self):
        self.s.execute(text("SELECT 1"))
        with self.assertRaises(q.QueueError):
            q.read_one_moment(self.s)
        self.s.rollback()
        q.read_one_moment(self.s)  # a fresh transaction: fine
        self.assertEqual(self.s.execute(text("SHOW transaction_isolation")).scalar_one(), "repeatable read")

    def test_the_mark_ends_with_its_transaction(self):
        self.worker()
        self.s.commit()
        q.read_one_moment(self.s)
        q.load_snapshot(self.s, self.settings)
        self.s.commit()
        with self.assertRaises(q.QueueError):  # snapshot -> commit -> a second snapshot without read_one_moment
            q.load_snapshot(self.s, self.settings)
        self.s.rollback()
        q.read_one_moment(self.s)
        q.collect_metrics(self.s, self.settings)
        self.s.rollback()
        with self.assertRaises(q.QueueError):  # the same after a rollback
            q.worker_table(self.s, self.settings)

    # ---------------------------------------------------------------- the time is the database's
    def test_every_time_comes_from_the_database_moment(self):
        w = self.worker()
        _sub, j = self.submission(self.student(1))
        self.s.commit()
        timeout = self.settings.heartbeat_timeout_s
        # the worker's heartbeat 2 s inside the timeout, by the database clock
        self.s.execute(text("UPDATE workers SET last_heartbeat = now() - make_interval(secs => :s)"),
                       {"s": timeout - 2})
        self.s.execute(text("UPDATE judgements SET created_at = now() - interval '100 seconds' WHERE id = :id"),
                       {"id": j.id})
        self.s.commit()
        far = datetime(2100, 1, 1, tzinfo=timezone.utc)  # this process's clock is wrong: nothing may follow it
        with mock.patch.object(q, "_now", lambda: far):
            moment = q.read_one_moment(self.s)
            load = q.load_snapshot(self.s, self.settings)
            metrics = q.collect_metrics(self.s, self.settings)
            row = q.worker_table(self.s, self.settings)[0]
            created = self.s.execute(select(Judgement.created_at).where(Judgement.id == j.id)).scalar_one()
            beat = self.s.execute(select(Worker.last_heartbeat).where(Worker.id == w.id)).scalar_one()
        self.s.commit()
        self.assertEqual((load.workers_online, row["state"]), (1, "idle"))
        self.assertEqual(metrics["oldest_queued_age_s"], (moment - created).total_seconds())
        self.assertEqual(row["heartbeat_age_s"], (moment - beat).total_seconds())
        self.assertAlmostEqual(metrics["oldest_queued_age_s"], 100.0, delta=1.0)
        # 2 s past the timeout by the database clock: offline, whatever this process thinks
        self.s.execute(text("UPDATE workers SET last_heartbeat = now() - make_interval(secs => :s)"),
                       {"s": timeout + 2})
        self.s.commit()
        with mock.patch.object(q, "_now", lambda: datetime(2000, 1, 1, tzinfo=timezone.utc)):
            q.read_one_moment(self.s)
            self.assertEqual(q.load_snapshot(self.s, self.settings).workers_online, 0)
            self.assertEqual(q.worker_table(self.s, self.settings)[0]["state"], "offline")
        self.s.commit()

    # ---------------------------------------------------------------- interleavings (with teeth)
    def metrics_with_a_claim_between(self, mark):
        """3 queued; another connection claims one right after the first query of collect_metrics (the queued
        count). Returns queue_length + in_flight and the total."""
        w = self.worker()
        student = self.student(1)
        for _ in range(3):
            self.submission(student)
        self.s.commit()
        mark(self.s)
        claim = lambda: self.other(lambda o: q.claim(o, o.get(Worker, w.id), self.settings))  # noqa: E731
        with self.after_statement(1, claim):
            m = q.collect_metrics(self.s, self.settings)
        total, done = self.total_and_done()
        self.s.commit()
        return m["queue_length"] + m["in_flight"] + done, total

    def test_queue_plus_in_flight_is_the_total(self):
        counted, total = self.metrics_with_a_claim_between(q.read_one_moment)
        self.assertEqual((counted, total), (3, 3))

    def test_teeth_queue_plus_in_flight_without_repeatable_read(self):
        counted, total = self.metrics_with_a_claim_between(without_repeatable_read)
        self.assertEqual((counted, total), (4, 3), "READ COMMITTED counts the claimed job twice")

    def place_with_submissions_between(self, mark):
        """x has a job in flight and one queued; right after the queued count, two other students submit (no job
        in flight: they go before x). Returns (queue_length, x's place)."""
        w = self.worker()
        x, others = self.student(1), [self.student(2), self.student(3)]
        self.submission(x)
        self.s.commit()
        q.claim(self.s, w, self.settings)  # x now has a job in flight
        self.submission(x)
        self.s.commit()
        ids = [u.id for u in others]
        mark(self.s)
        submit = lambda: self.other(lambda o: [self.submit_in(o, uid) for uid in ids])  # noqa: E731
        with self.after_statement(1, submit):
            snap = q.load_snapshot(self.s, self.settings, user_id=x.id)
        self.s.commit()
        return snap.queue_length, snap.my_position

    def test_my_place_is_within_the_queue(self):
        self.assertEqual(self.place_with_submissions_between(q.read_one_moment), (1, 1))

    def test_teeth_my_place_without_repeatable_read(self):
        length, place = self.place_with_submissions_between(without_repeatable_read)
        self.assertEqual((length, place), (1, 3), "READ COMMITTED: third in a queue of one")

    def busy_with_a_finish_between(self, mark):
        """w holds a job; the snapshot reads busy (load_snapshot), then another connection finishes the job, then
        the snapshot reads in_flight (collect_metrics), as /internal/status and /system do."""
        w = self.worker()
        self.submission(self.student(1))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        jid, wid = j.id, w.id
        mark(self.s)
        load = q.load_snapshot(self.s, self.settings)

        def finish(o):
            job = o.get(Judgement, jid)
            q.finish(o, o.get(Worker, wid), jid, self.ok_payload(job), self.settings)
        self.other(finish)
        metrics = q.collect_metrics(self.s, self.settings)
        self.s.commit()
        return load.workers_busy, metrics["in_flight"]

    def test_busy_is_at_most_in_flight(self):
        self.assertEqual(self.busy_with_a_finish_between(q.read_one_moment), (1, 1))

    def test_teeth_busy_without_repeatable_read(self):
        busy, in_flight = self.busy_with_a_finish_between(without_repeatable_read)
        self.assertEqual((busy, in_flight), (1, 0), "READ COMMITTED: one busy worker, no job in flight")

    # ---------------------------------------------------------------- load
    def test_200_snapshots_while_the_queue_changes(self):
        workers = [self.worker(f"w{i}") for i in range(2)]
        students = [self.student(i) for i in range(1, 5)]
        self.s.commit()
        worker_ids, student_ids = [w.id for w in workers], [u.id for u in students]
        stop, errors, steps = threading.Event(), [], [0]

        def churn():
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    with self.Session() as o:
                        kind = i % 3
                        if kind == 0:
                            self.submit_in(o, student_ids[i % len(student_ids)])
                        elif kind == 1:
                            q.claim(o, o.get(Worker, worker_ids[i % 2]), self.settings)
                        else:
                            held = o.execute(select(Judgement).where(Judgement.status.in_(q.IN_FLIGHT))
                                             .limit(1)).scalar_one_or_none()
                            if held is not None:
                                q.finish(o, o.get(Worker, held.worker_id), held.id, self.ok_payload(held),
                                         self.settings)
                        o.commit()
                    steps[0] += 1
            except Exception as exc:  # noqa: BLE001 - reported by the main thread
                errors.append(exc)

        thread = threading.Thread(target=churn, daemon=True)
        thread.start()
        try:
            for n in range(200):
                q.read_one_moment(self.s)
                snap = q.load_snapshot(self.s, self.settings, user_id=student_ids[n % len(student_ids)])
                m = q.collect_metrics(self.s, self.settings)
                table = q.worker_table(self.s, self.settings)
                total, done = self.total_and_done()
                self.s.commit()
                with self.subTest(snapshot=n):
                    self.assertEqual(m["queue_length"] + m["in_flight"] + done, total)
                    self.assertEqual(snap.queue_length, m["queue_length"])
                    self.assertLessEqual(snap.workers_busy, m["in_flight"])
                    self.assertLessEqual(sum(r["judgement_id"] is not None for r in table), m["in_flight"])
                    if snap.my_position is not None:
                        self.assertLessEqual(snap.my_position, snap.queue_length)
        finally:
            stop.set()
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertGreater(steps[0], 20, "the queue really changed meanwhile")

    # ---------------------------------------------------------------- the outputs keep their keys
    def test_outputs_keep_their_keys(self):
        w = self.worker()
        student = self.student(1)
        for _ in range(2):
            self.submission(student)
        self.s.commit()
        j = q.claim(self.s, w, self.settings)  # one finished (judge_finished_total), one queued
        q.finish(self.s, w, j.id, self.ok_payload(j), self.settings)
        self.s.commit()
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(blob_dir=tmp, timezone="UTC")
            with self.Session() as db:  # closed at once: an open transaction would block the next test's TRUNCATE
                snap = system.snapshot(db, settings)  # GET /api/admin/system and judgectl status
                db.rollback()
            self.assertEqual(set(snap), SYSTEM_KEYS)
            self.assertEqual(set(snap["load"]), LOAD_KEYS)
            self.assertEqual(set(snap["metrics"]), METRICS_KEYS)
            self.assertEqual(set(snap["workers"][0]), WORKER_KEYS)
            self.assertEqual(set(snap["migration"]), {"current", "head", "ok"})
            if HAVE_API:
                client = TestClient(create_internal_app(settings, session_factory=self.Session))
                status = client.get("/internal/status").json()
                self.assertEqual(set(status), {"load", "metrics", "workers"})
                self.assertEqual((set(status["load"]), set(status["metrics"]), set(status["workers"][0])),
                                 (LOAD_KEYS, METRICS_KEYS, WORKER_KEYS))
                names = set(re.findall(r"^(judge_[a-z_]+)", client.get("/internal/metrics").text, re.M))
                self.assertEqual(names, PROMETHEUS)
            from app.judgectl.cli import main
            from app.judgectl.core import Context
            out = io.StringIO()
            ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:test"),
                          out=out, err=io.StringIO(), interactive=lambda: False)
            main(["status", "--json"], ctx)
            data = json.loads(out.getvalue())
            self.assertEqual(set(data), JUDGECTL_STATUS)
            self.assertEqual(set(data["queue"]), {"level", "queue_length", "workers_busy", "workers_online"})
            out = io.StringIO()
            ctx.out = out
            self.assertEqual(main(["worker", "list", "--json"], ctx), 0)
            self.assertEqual(set(json.loads(out.getvalue())[0]), WORKER_KEYS)
