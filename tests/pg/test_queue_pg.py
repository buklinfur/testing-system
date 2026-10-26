"""Queue semantics on a real PostgreSQL (tests are skipped unless JUDGE_TEST_DATABASE_URL is set)."""
import random
import threading
import unittest

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:  # imported only when the database is configured
    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    from app import loadinfo, metrics, queue as q
    from app.models import Judgement, JudgementReason, JudgementStatus, QueueEvent, TestResult, Verdict


@requires_pg
class QueueTests(PgCase):
    def test_schema_and_partial_unique_indexes(self):
        sub, j = self.submission(self.student(1))
        self.s.commit()
        with self.assertRaises(IntegrityError):  # one unfinished judgement per submission
            q.enqueue(self.s, sub)
            self.s.flush()
        self.s.rollback()

    def test_claim_marks_row_and_respects_empty_queue(self):
        _t, w = self.token_and_worker()
        self.assertIsNone(q.claim(self.s, w, self.settings))
        sub, j = self.submission(self.student(1))
        self.s.commit()
        got = q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertEqual((got.id, got.status, got.attempt, got.worker_id), (j.id, JudgementStatus.compiling, 1, w.id))
        self.assertIsNone(q.claim(self.s, w, self.settings))

    def test_capabilities_are_matched(self):
        _t, cpu = self.token_and_worker("cpu0", caps=("cpu",))
        _t, gpu = self.token_and_worker("gpu0", caps=("cpu", "gpu"))
        _s, j = self.submission(self.student(1), requires=["gpu"])
        self.s.commit()
        self.assertIsNone(q.claim(self.s, cpu, self.settings))
        self.assertEqual(q.claim(self.s, gpu, self.settings).id, j.id)

    def test_token_cannot_grant_more_than_it_allows(self):
        plain = q.create_worker_token(self.s, "weak", ["cpu"])
        token = q.authenticate(self.s, plain)
        with self.assertRaises(q.TokenError):
            q.register_worker(self.s, token, "w", ["cpu", "gpu"], {}, self.settings)
        self.assertIsNone(q.authenticate(self.s, "wrong"))

    def test_fair_order_matches_the_reference_implementation(self):
        rng = random.Random(7)
        users = [self.student(i) for i in range(4)]
        _t, w = self.token_and_worker()
        subs = []
        for _ in range(14):
            sub, j = self.submission(rng.choice(users), priority=rng.choice([0, 0, 0, 10]))
            subs.append((sub, j))
        self.s.commit()
        # claim a few so that in-flight / recent counters are non-trivial, then compare the next picks
        for _ in range(3):
            q.claim(self.s, w, self.settings)
            self.s.commit()
        rows = self.s.execute(text("""SELECT j.id, s.user_id, j.priority, j.created_at FROM judgements j
                                      JOIN submissions s ON s.id = j.submission_id WHERE j.status = 'queued'""")).all()
        inflight, recent = {}, {}
        for r in self.s.execute(text("""SELECT s.user_id FROM judgements j JOIN submissions s ON s.id = j.submission_id
                                        WHERE j.status IN ('compiling', 'running')""")):
            inflight[r[0]] = inflight.get(r[0], 0) + 1
            recent[r[0]] = recent.get(r[0], 0) + 1
        expected = [r["id"] for r in loadinfo.fair_order(
            [{"id": r[0], "user_id": r[1], "priority": r[2], "created_at": r[3]} for r in rows], inflight, recent)]
        self.assertEqual(q.queue_position(self.s, self.settings, judgement_id=expected[0]), 1)
        self.assertEqual(q.queue_position(self.s, self.settings, judgement_id=expected[1]), 2)
        self.assertEqual(q.queue_position(self.s, self.settings, judgement_id=expected[4]), 5)
        picked = []
        for _ in range(3):
            j = q.claim(self.s, w, self.settings)
            self.s.commit()
            picked.append(j.id)
        self.assertEqual(picked[0], expected[0], "SQL order == reference order for the first pick")

    def test_every_claim_follows_the_reference_order(self):
        """CLAIM_SQL / POSITION_SQL == loadinfo.fair_key at every step, with the counters taken from raw rows.

        created_at is shuffled against id (the tie-break order matters), and some students have history:
        judgements that started inside the fairness window (count as recent) and long before it (do not).
        """
        rng = random.Random(11)
        users = [self.student(i) for i in range(5)]
        # minutes ago; the window is 5 minutes. Uneven on purpose: a wrong window changes who goes first.
        history = {0: (60, 90, 120), 1: (1,), 2: (2, 3, 60)}
        for n, starts in history.items():
            for minutes_ago in starts:
                sub, _ = self.submission(users[n], enqueue=False)
                self.s.add(Judgement(submission_id=sub.id, problem_version_id=self.seed["version"].id,
                                     status=JudgementStatus.finished, verdict=Verdict.OK,
                                     started_at=text(f"now() - interval '{minutes_ago} minutes'"),
                                     finished_at=text("now()")))
        queued = []
        for _ in range(16):
            _sub, j = self.submission(rng.choice(users), priority=rng.choice([0, 0, 0, 10, -1]))
            queued.append(j)
        for j, seconds_ago in zip(queued, rng.sample(range(10, 1000), len(queued))):
            j.created_at = text(f"now() - interval '{seconds_ago} seconds'")
        ties = rng.sample(queued, 4)  # equal created_at: the id decides
        for j in ties:
            j.created_at = text("now() - interval '5000 seconds'")
        self.s.commit()
        _t, w = self.token_and_worker(token_caps=("cpu",))

        def reference():
            now = self.s.execute(text("SELECT now()")).scalar_one()
            window = self.settings.recent_window_s
            rows = self.s.execute(text("""SELECT j.id, s.user_id, j.priority, j.created_at, j.status, j.started_at
                                          FROM judgements j JOIN submissions s ON s.id = j.submission_id""")).all()
            inflight, recent = {}, {}
            for r in rows:
                if r.status in (JudgementStatus.compiling.value, JudgementStatus.running.value):
                    inflight[r.user_id] = inflight.get(r.user_id, 0) + 1
                if r.started_at is not None and (now - r.started_at).total_seconds() < window:
                    recent[r.user_id] = recent.get(r.user_id, 0) + 1
            waiting = [{"id": r.id, "user_id": r.user_id, "priority": r.priority, "created_at": r.created_at}
                       for r in rows if r.status == JudgementStatus.queued.value]
            return [r["id"] for r in loadinfo.fair_order(waiting, inflight, recent)]

        steps = 0
        while True:
            expected = reference()
            positions = sorted(self.s.execute(q.POSITION_SQL, {"window": self.settings.recent_window_s}).all(),
                               key=lambda r: r.pos)
            self.assertEqual([r.id for r in positions], expected, f"POSITION_SQL order, step {steps}")
            j = q.claim(self.s, w, self.settings)
            self.s.commit()
            if not expected:
                self.assertIsNone(j)
                break
            self.assertEqual(j.id, expected[0], f"CLAIM_SQL pick, step {steps}")
            steps += 1
        self.assertEqual(steps, len(queued))

    def test_one_student_does_not_block_another(self):
        a, b = self.student(1), self.student(2)
        for _ in range(5):
            self.submission(a)
        _sb, jb = self.submission(b)
        self.s.commit()
        _t, w1 = self.token_and_worker("w1")
        _t, w2 = self.token_and_worker("w2")
        first = q.claim(self.s, w1, self.settings)
        self.s.commit()
        second = q.claim(self.s, w2, self.settings)
        self.s.commit()
        self.assertEqual(second.id, jb.id, "the second free worker serves the other student")

    def test_rejudges_wait_behind_fresh_submissions(self):
        a = self.student(1)
        _s1, rej = self.submission(a, reason=JudgementReason.rejudge)
        _s2, fresh = self.submission(self.student(2))
        self.s.commit()
        _t, w = self.token_and_worker()
        self.assertEqual(q.claim(self.s, w, self.settings).id, fresh.id)

    def test_concurrent_claims_never_hand_out_a_job_twice(self):
        users = [self.student(i) for i in range(5)]
        for i in range(40):
            self.submission(users[i % 5])
        workers = [self.token_and_worker(f"w{i}")[1] for i in range(8)]
        self.s.commit()
        got, lock = [], threading.Lock()

        def run(worker):
            while True:
                with self.Session() as session:
                    w = session.get(type(worker), worker.id)
                    j = q.claim(session, w, self.settings)
                    session.commit()
                    if j is None:
                        return
                    with lock:
                        got.append(j.id)

        threads = [threading.Thread(target=run, args=(w,)) for w in workers]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(got), 40)
        self.assertEqual(len(set(got)), 40)

    def test_reaper_requeues_and_gives_up_on_poison_submissions(self):
        _t, w = self.token_and_worker()
        _s, j = self.submission(self.student(1))
        self.s.commit()
        for attempt in (1, 2):
            claimed = q.claim(self.s, w, self.settings)
            self.assertEqual(claimed.attempt, attempt)
            self.s.commit()
            self.s.execute(text("UPDATE workers SET last_heartbeat = now() - interval '5 minutes'"))
            self.s.commit()
            self.assertEqual(q.reap(self.s, self.settings), {"requeued": 1, "gave_up": 0})
            self.s.commit()
            event = self.s.execute(select(QueueEvent).where(QueueEvent.kind == "requeued")
                                   .order_by(QueueEvent.id.desc()).limit(1)).scalar_one()
            self.assertEqual((event.worker_id, event.details["attempt"]), (w.id, attempt), "the history names the worker")
            self.s.execute(text("UPDATE workers SET last_heartbeat = now()"))
            self.s.commit()
        q.claim(self.s, w, self.settings)
        self.s.commit()
        self.s.execute(text("UPDATE workers SET last_heartbeat = now() - interval '5 minutes'"))
        self.s.commit()
        self.assertEqual(q.reap(self.s, self.settings), {"requeued": 0, "gave_up": 1})
        self.s.commit()
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.verdict, j.failures), (JudgementStatus.internal_error, Verdict.IE, 3))

    def test_live_worker_keeps_its_job_but_a_runaway_judging_is_taken_away(self):
        _t, w = self.token_and_worker()
        _s, j = self.submission(self.student(1))
        self.s.commit()
        q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertEqual(q.reap(self.s, self.settings), {"requeued": 0, "gave_up": 0})
        self.s.execute(text("UPDATE judgements SET started_at = now() - interval '2 hours'"))
        self.s.commit()
        self.assertEqual(q.reap(self.s, self.settings)["requeued"], 1)

    def test_heartbeat_progress_and_cancel(self):
        _t, w = self.token_and_worker()
        _s, j = self.submission(self.student(1))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        reply = q.heartbeat(self.s, w, state="busy", judgement_id=j.id, attempt=j.attempt, phase="running",
                            tests_done=3, tests_total=10, stats={})
        self.s.commit()
        self.assertFalse(reply["cancel"])
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.tests_done, j.tests_total), (JudgementStatus.running, 3, 10))
        wrong = q.heartbeat(self.s, w, state="busy", judgement_id=j.id, attempt=99, phase="running",
                            tests_done=None, tests_total=None, stats={})
        self.assertTrue(wrong["cancel"])

    def test_register_requeues_what_the_previous_instance_held(self):
        token, w = self.token_and_worker()
        _s, j = self.submission(self.student(1))
        self.s.commit()
        q.claim(self.s, w, self.settings)
        self.s.commit()
        old_instance = w.instance_id
        w2 = q.register_worker(self.s, token, "w0", ["cpu"], {}, self.settings)
        self.s.commit()
        self.assertNotEqual(w2.instance_id, old_instance)
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.failures, j.worker_id), (JudgementStatus.queued, 1, None))

    def test_release_is_not_a_failure(self):
        _t, w = self.token_and_worker()
        _s, j = self.submission(self.student(1))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertFalse(q.release(self.s, w, j.id, attempt=j.attempt + 1, reason="x"))
        self.assertTrue(q.release(self.s, w, j.id, attempt=j.attempt, reason="stopping"))
        self.s.commit()
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.failures), (JudgementStatus.queued, 0))
        event = self.s.execute(select(QueueEvent).where(QueueEvent.kind == "released")).scalar_one()
        self.assertEqual((event.worker_id, event.judgement_id, event.details),
                         (w.id, j.id, {"reason": "stopping", "attempt": 1}), "the history names the worker")


@requires_pg
class ResultTests(PgCase):
    def claimed(self, reason=None, user=None):
        _t, w = self.token_and_worker()
        sub, j = self.submission(user or self.student(1), reason=reason or JudgementReason.initial)
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        return w, sub, j

    def test_result_is_recorded_once_and_idempotent(self):
        w, _sub, j = self.claimed()
        payload = self.ok_payload(j)
        self.assertEqual(q.finish(self.s, w, j.id, payload, self.settings), "recorded")
        self.s.commit()
        self.assertEqual(q.finish(self.s, w, j.id, payload, self.settings), "already_recorded")
        self.s.commit()
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.verdict, float(j.score)), (JudgementStatus.finished, Verdict.OK, 100.0))
        rows = self.s.execute(select(TestResult).where(TestResult.judgement_id == j.id)).scalars().all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r.visible_to_student for r in rows), [False, True])

    def test_fencing_rejects_stale_attempts_and_other_workers(self):
        w, _sub, j = self.claimed()
        payload = self.ok_payload(j)
        _t, other = self.token_and_worker("w9")
        with self.assertRaises(q.LeaseLost):
            q.finish(self.s, other, j.id, payload, self.settings)
        with self.assertRaises(q.LeaseLost):
            q.finish(self.s, w, j.id, dict(payload, attempt=j.attempt + 1), self.settings)

    def test_unknown_test_case_ids_are_rejected(self):
        w, _sub, j = self.claimed()
        bad = self.ok_payload(j, tests=[{"test_case_id": 999999, "verdict": "OK"}])
        with self.assertRaises(q.BadResult):
            q.finish(self.s, w, j.id, bad, self.settings)

    def test_internal_error_retries_then_gives_up(self):
        w, _sub, j = self.claimed()
        ie = {"attempt": j.attempt, "verdict": "IE", "error": "boom", "tests": []}
        self.assertEqual(q.finish(self.s, w, j.id, ie, self.settings), "requeued")
        self.s.commit()
        for _ in range(2):
            j = q.claim(self.s, w, self.settings)
            self.s.commit()
            out = q.finish(self.s, w, j.id, dict(ie, attempt=j.attempt), self.settings)
            self.s.commit()
        self.assertEqual(out, "recorded")
        j = self.s.get(Judgement, j.id, populate_existing=True)
        self.assertEqual((j.status, j.verdict), (JudgementStatus.internal_error, Verdict.IE))

    def test_rejudge_keeps_history_and_takes_over_only_on_success(self):
        w, sub, j1 = self.claimed()
        q.finish(self.s, w, j1.id, self.ok_payload(j1), self.settings)
        self.s.commit()
        outcome = q.request_rejudge(self.s, sub.id, requested_by=self.seed["teacher"].id)
        self.s.commit()
        self.assertEqual(outcome.kind, "created")
        j2 = outcome.judgement
        self.assertFalse(j2.is_current)
        self.assertEqual(q.request_rejudge(self.s, sub.id).kind, "retargeted", "no second queue entry")
        self.s.commit()
        cur = self.s.execute(select(Judgement.id).where(Judgement.submission_id == sub.id, Judgement.is_current)).scalar_one()
        self.assertEqual(cur, j1.id, "the old verdict stays visible while the rejudge waits")
        j2 = q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertEqual(q.request_rejudge(self.s, sub.id).kind, "already_running")
        wa = self.ok_payload(j2)
        wa.update(verdict="WA", score="0")
        q.finish(self.s, w, j2.id, wa, self.settings)
        self.s.commit()
        rows = self.s.execute(select(Judgement).where(Judgement.submission_id == sub.id).order_by(Judgement.id)).scalars().all()
        self.assertEqual([(r.id, r.is_current, r.verdict) for r in rows], [(j1.id, False, Verdict.OK), (j2.id, True, Verdict.WA)])
        self.assertEqual(len(self.s.execute(select(TestResult).where(TestResult.judgement_id == j1.id)).scalars().all()), 2,
                         "old test results are kept")

    def test_failed_rejudge_does_not_replace_the_good_verdict(self):
        w, sub, j1 = self.claimed()
        q.finish(self.s, w, j1.id, self.ok_payload(j1), self.settings)
        self.s.commit()
        j2 = q.request_rejudge(self.s, sub.id).judgement
        self.s.commit()
        for _ in range(3):
            j2 = q.claim(self.s, w, self.settings)
            q.finish(self.s, w, j2.id, {"attempt": j2.attempt, "verdict": "IE", "error": "x", "tests": []}, self.settings)
            self.s.commit()
        cur = self.s.execute(select(Judgement).where(Judgement.submission_id == sub.id, Judgement.is_current)).scalar_one()
        self.assertEqual((cur.id, cur.verdict), (j1.id, Verdict.OK))

    def test_rejudge_contest_problem(self):
        a = self.student(1)
        self.submission(a)
        self.submission(self.student(2))
        self.s.commit()
        counts = q.rejudge_contest_problem(self.s, self.seed["cp"].id)
        self.assertEqual(counts["retargeted"], 2, "both are still queued: nothing duplicated")


@requires_pg
class LoadTests(PgCase):
    def test_snapshot_position_and_metrics(self):
        _t, w = self.token_and_worker()
        a, b = self.student(1), self.student(2)
        for _ in range(3):
            self.submission(a)
        _sb, jb = self.submission(b)
        self.s.commit()
        q.heartbeat(self.s, w, state="idle", judgement_id=None, attempt=None, phase=None, tests_done=None, tests_total=None, stats={})
        self.s.commit()
        snap = q.load_snapshot(self.s, self.settings, user_id=b.id)
        self.assertEqual((snap.queue_length, snap.workers_online, snap.workers_busy), (4, 1, 0))
        self.assertEqual(snap.my_position, 4, "equal counters: age decides, student 2 came last")
        self.assertEqual(snap.my_wait_seconds, 12.5)
        self.assertEqual(q.load_snapshot(self.s, self.settings, user_id=a.id).my_wait_seconds, 0.0,
                         "a is first in line and an idle worker is online, so the wait is zero")
        text_out = metrics.render_prometheus(q.collect_metrics(self.s, self.settings))
        self.assertIn("judge_queue_length 4", text_out)
        rows = q.worker_table(self.s, self.settings)
        self.assertEqual(rows[0]["state"], "idle")

    def beat(self, w, state="idle"):
        q.heartbeat(self.s, w, state=state, judgement_id=None, attempt=None, phase=None, tests_done=None,
                    tests_total=None, stats={})
        self.s.commit()

    def counters(self):
        snap = q.load_snapshot(self.s, self.settings)
        return snap.workers_online, snap.workers_busy, snap.level

    def test_busy_follows_the_jobs_without_waiting_for_a_heartbeat(self):
        _t, w = self.token_and_worker()
        self.beat(w)
        _s, j = self.submission(self.student(1))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertEqual(self.counters(), (1, 1, "busy"))
        q.finish(self.s, w, j.id, self.ok_payload(j), self.settings)
        self.s.commit()
        self.assertEqual(w.state, "idle", "finish frees the worker at once")
        self.assertEqual(self.counters(), (1, 0, "free"), "no heartbeat needed for the indicator to recover")

    def test_release_frees_the_worker_at_once(self):
        _t, w = self.token_and_worker()
        self.beat(w)
        _s, j = self.submission(self.student(1))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        self.assertTrue(q.release(self.s, w, j.id, j.attempt, "worker_stopping"))
        self.s.commit()
        self.assertEqual(w.state, "idle")
        self.assertEqual(self.counters(), (1, 0, "busy"), "one job queued, one idle worker: busy, not overloaded")

    def test_a_stale_busy_flag_without_a_job_is_not_busy(self):
        _t, w = self.token_and_worker()
        self.beat(w, state="busy")  # e.g. the flag left over from a job that was taken away by the reaper
        self.assertEqual(self.counters(), (1, 0, "free"))

    def test_a_draining_worker_is_not_capacity(self):
        _t, w1 = self.token_and_worker("w1")
        _t, w2 = self.token_and_worker("w2")
        self.beat(w1)
        self.beat(w2, state="draining")
        student = self.student(1)
        for _ in range(2):
            self.submission(student)
        self.s.commit()
        self.assertEqual(self.counters(), (1, 0, "overloaded"), "two jobs, one worker that will take them")
        metrics_out = q.collect_metrics(self.s, self.settings)
        self.assertEqual(metrics_out["workers_by_state"], {"idle": 1, "draining": 1}, "metrics still show both")


if __name__ == "__main__":
    unittest.main()
