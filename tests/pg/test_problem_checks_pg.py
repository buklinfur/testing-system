"""Checks of problem versions in the queue (session 4a.4, D-049..D-054) on a real PostgreSQL.

Skipped unless JUDGE_TEST_DATABASE_URL is set. The fair order of submissions (CLAIM_SQL == loadinfo.fair_key) is
tested in test_queue_pg.py and is not touched by checks.
"""
import threading
import time

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from dataclasses import replace

    from sqlalchemy import select, text
    from sqlalchemy.exc import IntegrityError

    from app import problem_checks as checks, queue as q
    from app.models import (CheckOutcome, CheckReason, CheckStatus, Judgement, JudgementStatus, Problem,
                            ProblemType, ProblemVersion, Program, ProgramRole, QueueEvent, Verdict, VersionCheck,
                            VersionStatus, Worker)

CAPS = ("cpu", "problem_check")
TOKEN_CAPS = ("cpu", "gpu", "problem_check")


@requires_pg
class ProblemCheckQueueTests(PgCase):
    # ---------------------------------------------------------------- helpers
    def worker(self, name):
        return self.token_and_worker(name, caps=CAPS, token_caps=TOKEN_CAPS)[1]

    def version(self, slug="extra", status=VersionStatus.pending_check):
        problem = Problem(slug=slug, owner_id=self.seed["admin"].id)
        self.s.add(problem)
        self.s.flush()
        v = ProblemVersion(problem_id=problem.id, version=1, status=status, type=ProblemType.batch,
                           titles={"ru": slug}, statements={"ru": ""}, config={})
        self.s.add(v)
        self.s.flush()
        return v

    def check(self, version=None, reason=CheckReason.import_):
        c = checks.enqueue_check(self.s, version or self.version(), reason)
        self.s.commit()
        return c

    def report(self, outcome="ready", **extra):
        return {"outcome": outcome, "reason": None if outcome == "ready" else "solution_mismatch", "programs": [],
                "solutions": [{"name": "ok.py", "expected": "OK", "got": "OK", "ok": True}], **extra}

    def claim(self, w):
        job = q.claim_next(self.s, w, self.settings)
        self.s.commit()
        return job

    def age(self, sql, **params):
        self.s.execute(text(sql), params)
        self.s.commit()
        self.s.expire_all()

    # ---------------------------------------------------------------- what is checked
    def test_needs_check_only_with_a_checker_an_interactor_or_solutions(self):
        v = self.version()
        self.assertFalse(checks.needs_check(self.s, v.id))
        self.s.add(Program(version_id=v.id, role=ProgramRole.validator, name="validator.cpp", language_key="cpp",
                           source_sha256="c" * 64))
        self.s.flush()
        self.assertFalse(checks.needs_check(self.s, v.id), "a validator alone is not run (D-051)")
        for role in (ProgramRole.checker, ProgramRole.interactor, ProgramRole.solution):
            with self.subTest(role=role):
                other = self.version(f"with-{role.value}")
                self.s.add(Program(version_id=other.id, role=role, name=f"{role.value}.cpp", language_key="cpp",
                                   source_sha256="c" * 64, expected_verdict=Verdict.OK if role.value == "solution" else None))
                self.s.flush()
                self.assertTrue(checks.needs_check(self.s, other.id))

    def test_one_unfinished_check_per_version(self):
        v = self.version()
        self.check(v)
        with self.assertRaises(IntegrityError):
            checks.enqueue_check(self.s, v, CheckReason.recheck)
        self.s.rollback()

    # ---------------------------------------------------------------- (1) priority: two phases
    def test_a_waiting_submission_goes_first(self):
        c = self.check()
        _sub, j = self.submission(self.student(1))
        self.s.commit()
        w = self.worker("w0")
        self.assertEqual(self.claim(w), self.s.get(Judgement, j.id), "phase 1: the submission")
        got = self.claim(w)
        self.assertIsInstance(got, VersionCheck, "phase 2: nothing else for this worker, now the check")
        self.assertEqual((got.id, got.status, got.attempt, got.worker_id), (c.id, CheckStatus.running, 1, w.id))
        self.assertIsNone(self.claim(w))

    def test_a_submission_this_worker_cannot_take_does_not_block_the_check(self):
        c = self.check()
        self.submission(self.student(1), requires=["gpu"])
        self.s.commit()
        self.assertEqual(self.claim(self.worker("cpu0")).id, c.id)

    def test_a_worker_without_the_capability_never_gets_a_check(self):
        self.check()
        _t, old = self.token_and_worker("old0", caps=("cpu",), token_caps=TOKEN_CAPS)
        self.assertIsNone(self.claim(old))
        self.assertIsNotNone(self.claim(self.worker("new0")))

    def test_quota(self):
        first, second = self.check(self.version("a")), self.check(self.version("b"))
        w0, w1 = self.worker("w0"), self.worker("w1")
        self.assertEqual(self.claim(w0).id, first.id)
        self.assertIsNone(self.claim(w1), "quota 1: one check runs, the second waits")
        self.assertEqual(checks.finish(self.s, w0, first.id, {"attempt": 1, "outcome": "ready",
                                                              "report": self.report()}, self.settings), "recorded")
        self.s.commit()
        self.assertEqual(self.claim(w1).id, second.id, "the first ended: the next one may start")
        self.settings_two = replace(self.settings, max_concurrent_checks=2)
        third = self.check(self.version("c"))
        self.assertIsNone(self.claim(w0))
        self.assertEqual(q.claim_next(self.s, w0, self.settings_two).id, third.id, "quota 2")

    def test_quota_holds_for_simultaneous_claims_on_two_connections(self):
        """Two workers claim at the same moment from two connections; with quota 1 exactly one gets a check.

        The first claimer sleeps inside its transaction, after phase 2 and before commit: the other one must wait
        for the advisory lock and then count the committed running check. Without the lock both would read 0.
        """
        workers = [self.worker("w0").id, self.worker("w1").id]
        for round_no in range(5):
            with self.subTest(round=round_no):
                self.s.execute(text("UPDATE version_checks SET status = 'done', outcome = 'ready'"))
                self.s.commit()
                for n in range(2):
                    self.check(self.version(f"r{round_no}-{n}"))
                barrier, got = threading.Barrier(2), {}

                def claim(worker_id):
                    with self.Session() as session:
                        worker = session.get(Worker, worker_id)
                        barrier.wait()
                        job = q.claim_next(session, worker, self.settings)
                        time.sleep(0.3)  # hold the lock: the other claim is blocked right now
                        session.commit()
                        got[worker_id] = job.id if job is not None else None

                threads = [threading.Thread(target=claim, args=(w,)) for w in workers]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(sorted(v is None for v in got.values()), [False, True], got)
                self.s.expire_all()
                self.assertEqual(checks.running_count(self.s), 1)

    # ---------------------------------------------------------------- (2) submissions do not wait for a check
    def test_a_submission_during_a_check_goes_to_a_free_worker_at_once(self):
        c = self.check()
        checker, free = self.worker("w0"), self.worker("w1")
        self.assertEqual(self.claim(checker).id, c.id)
        student = self.student(1)
        _sub, j = self.submission(student)
        self.s.commit()
        snap = q.load_snapshot(self.s, self.settings, user_id=student.id)
        self.assertEqual((snap.queue_length, snap.my_position), (1, 1), "the check is not in the students' queue")
        self.assertEqual(snap.workers_online, 1, "the worker on the check is not capacity (D-050)")
        got = self.claim(free)
        self.assertEqual((got.id, got.status, got.worker_id), (j.id, JudgementStatus.compiling, free.id))
        self.s.refresh(c)
        self.assertEqual((c.status, c.worker_id), (CheckStatus.running, checker.id), "the check goes on")

    def test_load_metrics_and_worker_table_show_checks_to_admins_only(self):
        c = self.check()
        self.check(self.version("second"))
        w = self.worker("w0")
        self.worker("w1")
        self.claim(w)
        snap = q.load_snapshot(self.s, self.settings)
        self.assertEqual((snap.queue_length, snap.workers_online, snap.workers_busy), (0, 1, 0))
        metrics = q.collect_metrics(self.s, self.settings)
        self.assertEqual((metrics["queue_length"], metrics["in_flight"]), (0, 0))
        self.assertEqual(metrics["checks"], {"queued": 1, "running": 1})
        table = {r["name"]: r for r in q.worker_table(self.s, self.settings)}
        self.assertEqual((table["w0"]["check_id"], table["w0"]["judgement_id"]), (c.id, None))
        self.assertIsNone(table["w1"]["check_id"])

    # ---------------------------------------------------------------- results, fencing, release
    def test_the_outcome_becomes_the_version_status_and_the_report_is_copied(self):
        for outcome in ("ready", "failed"):
            with self.subTest(outcome=outcome):
                v = self.version(f"v-{outcome}")
                c = self.check(v)
                w = self.worker(f"w-{outcome}")
                self.claim(w)
                payload = {"attempt": 1, "outcome": outcome, "report": self.report(outcome)}
                self.assertEqual(checks.finish(self.s, w, c.id, payload, self.settings), "recorded")
                self.s.commit()
                self.assertEqual(checks.finish(self.s, w, c.id, payload, self.settings), "already_recorded")
                self.s.refresh(v)
                self.s.refresh(c)
                self.assertEqual((v.status.value, v.check_report), (outcome, self.report(outcome)))
                self.assertEqual((c.status, c.outcome.value, c.report), (CheckStatus.done, outcome, self.report(outcome)))
                self.assertIsNotNone(c.finished_at)
                self.assertEqual(w.state, "idle")

    def test_fencing(self):
        c = self.check()
        w, other = self.worker("w0"), self.worker("w1")
        self.claim(w)
        payload = {"attempt": 1, "outcome": "ready", "report": self.report()}
        for who, attempt in ((other, 1), (w, 2)):
            with self.subTest(worker=who.name, attempt=attempt), self.assertRaises(checks.CheckLeaseLost):
                checks.finish(self.s, who, c.id, dict(payload, attempt=attempt), self.settings)
        self.s.rollback()
        reply = q.heartbeat(self.s, w, state="busy", judgement_id=None, attempt=2, phase=None, tests_done=1,
                            tests_total=3, stats={}, check_id=c.id)
        self.assertEqual(reply, {"cancel": True})
        reply = q.heartbeat(self.s, w, state="busy", judgement_id=None, attempt=1, phase=None, tests_done=1,
                            tests_total=3, stats={}, check_id=c.id)
        self.s.commit()
        self.assertEqual(reply, {"cancel": False})
        self.s.refresh(c)
        self.assertEqual((c.progress_done, c.progress_total), (1, 3))
        for bad in ({"attempt": 1}, {"attempt": 1, "outcome": "maybe", "report": {}},
                    {"attempt": 1, "outcome": "ready", "report": self.report("failed")},
                    {"attempt": 1, "outcome": "ready", "report": "text"},
                    {"attempt": 1, "outcome": "failed", "report": self.report("failed", reason=None)},
                    {"attempt": 1, "outcome": "failed", "report": self.report("failed", reason="bad luck")},
                    {"attempt": 1, "outcome": "ready", "report": self.report(by="server")},
                    {"attempt": 1, "outcome": "ready", "report": self.report(solutions="ok")},
                    {"attempt": 1, "outcome": "ready", "report": self.report(log="x" * checks.MAX_REPORT_BYTES)}):
            with self.subTest(bad=bad), self.assertRaises(checks.BadCheckResult):
                checks.finish(self.s, w, c.id, bad, self.settings)
            self.s.rollback()

    def test_release_and_deregister_do_not_count_a_failure(self):
        c = self.check()
        w = self.worker("w0")
        self.claim(w)
        self.assertFalse(checks.release_by_worker(self.s, w, c.id, 2, "stopping"), "a stale attempt is ignored")
        self.assertTrue(checks.release_by_worker(self.s, w, c.id, 1, "stopping"))
        self.s.commit()
        self.s.refresh(c)
        self.assertEqual((c.status, c.worker_id, c.failures, c.attempt), (CheckStatus.queued, None, 0, 1))
        self.assertEqual(self.claim(w).attempt, 2)
        q.deregister_worker(self.s, w)
        self.s.commit()
        self.s.refresh(c)
        self.assertEqual((c.status, c.failures), (CheckStatus.queued, 0))

    def test_a_restarted_worker_loses_its_check_as_a_failure(self):
        c = self.check()
        token, w = self.token_and_worker("w0", caps=CAPS, token_caps=TOKEN_CAPS)
        self.claim(w)
        q.register_worker(self.s, token, "w0", list(CAPS), {}, self.settings)
        self.s.commit()
        self.s.refresh(c)
        self.assertEqual((c.status, c.failures), (CheckStatus.queued, 1))

    def test_an_internal_error_of_the_worker_is_retried_then_the_check_fails(self):
        v = self.version()
        c = self.check(v)
        w = self.worker("w0")
        for attempt in range(1, self.settings.max_attempts + 1):
            self.assertEqual(self.claim(w).attempt, attempt)
            out = checks.finish(self.s, w, c.id, {"attempt": attempt, "error": "blob missing"}, self.settings)
            self.s.commit()
            self.assertEqual(out, "requeued" if attempt < self.settings.max_attempts else "recorded")
        self.s.refresh(c)
        self.s.refresh(v)
        self.assertEqual((c.status, c.outcome, c.failures), (CheckStatus.done, CheckOutcome.failed, 3))
        self.assertEqual((c.report["reason"], c.report["by"]), ("internal_error", "server"))
        self.assertIn("blob missing", c.report["error"])
        self.assertEqual(v.status, VersionStatus.failed)

    # ---------------------------------------------------------------- (4) the reaper
    def test_reaper_requeues_a_lost_check_and_gives_up_after_max_attempts(self):
        v = self.version()
        c = self.check(v)
        w = self.worker("w0")
        for attempt in range(1, self.settings.max_attempts + 1):
            self.assertEqual(self.claim(w).id, c.id)
            self.assertEqual(q.reap(self.s, self.settings), {"requeued": 0, "gave_up": 0}, "alive: kept")
            self.age("UPDATE workers SET last_heartbeat = now() - interval '1 hour' WHERE id = :w", w=w.id)
            out = q.reap(self.s, self.settings)
            self.s.commit()
            self.s.refresh(c)
            if attempt < self.settings.max_attempts:
                self.assertEqual(out, {"requeued": 0, "gave_up": 0, "checks_requeued": 1})
                self.assertEqual((c.status, c.worker_id, c.failures), (CheckStatus.queued, None, attempt))
                self.age("UPDATE workers SET last_heartbeat = now(), state = 'idle' WHERE id = :w", w=w.id)
            else:
                self.assertEqual(out, {"requeued": 0, "gave_up": 0, "checks_failed": 1})
        self.s.refresh(v)
        self.assertEqual((c.status, c.outcome, c.failures), (CheckStatus.done, CheckOutcome.failed, 3))
        self.assertEqual(c.report["reason"], "worker_lost")
        self.assertEqual(v.status, VersionStatus.failed)
        with self.assertRaises(checks.CheckLeaseLost):  # the worker wakes up too late
            checks.finish(self.s, w, c.id, {"attempt": 3, "outcome": "ready", "report": self.report()}, self.settings)

    def test_reaper_takes_a_check_whose_own_heartbeat_stopped(self):
        """The worker is alive (it heartbeats) but no longer about this check: the check is lost all the same."""
        c = self.check()
        w = self.worker("w0")
        self.claim(w)
        self.age("UPDATE version_checks SET heartbeat_at = now() - interval '1 hour' WHERE id = :c", c=c.id)
        self.assertEqual(q.reap(self.s, self.settings)["checks_requeued"], 1)

    def test_reaper_ends_an_overlong_check_failed_without_a_retry(self):
        v = self.version()
        c = self.check(v)
        w = self.worker("w0")
        self.claim(w)
        limit = self.settings.check_timeout_s
        self.age("UPDATE version_checks SET started_at = now() - make_interval(secs => :s) WHERE id = :c",
                 s=limit + checks.TIMEOUT_GRACE_S - 5, c=c.id)
        self.assertEqual(q.reap(self.s, self.settings), {"requeued": 0, "gave_up": 0}, "the worker's own limit first")
        self.age("UPDATE version_checks SET started_at = now() - make_interval(secs => :s) WHERE id = :c",
                 s=limit + checks.TIMEOUT_GRACE_S + 5, c=c.id)
        self.assertEqual(q.reap(self.s, self.settings), {"requeued": 0, "gave_up": 0, "checks_timed_out": 1})
        self.s.commit()
        self.s.refresh(c)
        self.s.refresh(v)
        self.assertEqual((c.status, c.outcome, c.failures), (CheckStatus.done, CheckOutcome.failed, 0), "no retry")
        self.assertEqual(c.report["reason"], "timeout")
        self.assertIn("600 s", c.report["error"])
        self.assertEqual(v.status, VersionStatus.failed)
        self.assertIsNone(self.claim(w), "nothing goes back to the queue")
        events = [e.kind for e in self.s.execute(select(QueueEvent).order_by(QueueEvent.id)).scalars()]
        self.assertEqual(events[-1], "check_done")

    # ---------------------------------------------------------------- recheck
    def test_recheck(self):
        v = self.seed["version"]  # ready, pinned in the seed contest
        out = checks.request_recheck(self.s, v, requested_by=self.seed["admin"].id)
        self.s.commit()
        self.assertEqual((out.kind, out.check.reason, out.check.requested_by),
                         ("created", CheckReason.recheck, self.seed["admin"].id))
        self.assertEqual(checks.request_recheck(self.s, v).kind, "already_queued")
        w = self.worker("w0")
        self.claim(w)
        self.assertEqual(checks.request_recheck(self.s, v).kind, "already_running")
        self.s.refresh(v)
        self.assertEqual(v.status, VersionStatus.ready, "the status changes only at the end (D-054)")
        checks.finish(self.s, w, out.check.id, {"attempt": 1, "outcome": "failed", "report": self.report("failed")},
                      self.settings)
        self.s.commit()
        self.s.refresh(v)
        self.assertEqual(v.status, VersionStatus.failed)
        again = checks.request_recheck(self.s, v)
        self.assertEqual(again.kind, "created")
        self.assertEqual([c.id for c in checks.history(self.s, v.id)], [again.check.id, out.check.id])
        self.s.rollback()
        self.s.get(Problem, v.problem_id).archived = True
        self.s.flush()
        with self.assertRaises(checks.CheckError) as caught:
            checks.request_recheck(self.s, v)
        self.assertEqual(caught.exception.code, "problem_archived")
