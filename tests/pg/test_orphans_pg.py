"""The safety net «idle, but holding a job» (queue.reclaim_orphans, D-069) on a real PostgreSQL: a heartbeat that
does not name a job its worker holds since longer than the grace (JUDGE_HEARTBEAT_TIMEOUT_S, by the database clock)
returns it to the queue as a failed attempt - for submissions and for checks of problem versions - while a job just
handed out, a job the heartbeat names and a job whose result is being recorded right now are left alone; the old
holder is refused afterwards; a judgement the server gave up on refuses a late result (judgements.server_gave_up_at marks it, D-090)
and keeps who judged it in every view.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import io
import json

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

CAPS = ("cpu", "problem_check")

if URL and HAVE_SA:
    from sqlalchemy import select, text

    from app import metrics
    from app import problem_checks as checks
    from app import queue as q
    from app import scores
    from app.auth import audit
    from app.models import (CheckReason, CheckStatus, Judgement, JudgementStatus, Problem, ProblemType,
                            ProblemVersion, QueueEvent, VersionCheck, VersionStatus, Worker)
    from app.settings import Settings
    from app.submissions import view


@requires_pg
class OrphanTests(PgCase):
    # ---------------------------------------------------------------- helpers
    def worker(self, name="w0"):
        return self.token_and_worker(name, caps=CAPS, token_caps=CAPS)[1]

    def job(self, w, user=1):
        """A submission claimed by w now: (submission id, judgement)."""
        sub, _j = self.submission(self.student(user))
        self.s.commit()
        j = q.claim(self.s, w, self.settings)
        self.s.commit()
        return sub.id, j

    def age(self, table, row_id, seconds):
        """Hand-out moment `seconds` ago by the database clock."""
        self.s.execute(text(f"UPDATE {table} SET started_at = now() - make_interval(secs => :s) WHERE id = :id"),
                       {"s": seconds, "id": row_id})
        self.s.commit()
        self.s.expire_all()

    def old(self):
        return self.settings.heartbeat_timeout_s + 5  # past the grace

    def beat(self, w, judgement_id=None, check_id=None, attempt=None, state="idle"):
        reply = q.heartbeat(self.s, self.s.get(Worker, w.id), state=state, judgement_id=judgement_id, attempt=attempt,
                            phase=None, tests_done=None, tests_total=None, stats={}, settings=self.settings,
                            check_id=check_id)
        self.s.commit()
        self.s.expire_all()
        return reply

    def judgement(self, jid):
        self.s.expire_all()
        return self.s.get(Judgement, jid)

    def events(self, kind):
        return list(self.s.execute(select(QueueEvent).where(QueueEvent.kind == kind).order_by(QueueEvent.id)).scalars())

    def version(self, slug="extra"):
        problem = Problem(slug=slug, owner_id=self.seed["admin"].id)
        self.s.add(problem)
        self.s.flush()
        v = ProblemVersion(problem_id=problem.id, version=1, status=VersionStatus.pending_check, type=ProblemType.batch,
                           titles={"ru": slug}, statements={"ru": ""}, config={})
        self.s.add(v)
        self.s.flush()
        return v

    def check(self, w):
        """A check claimed by w now (no submission is waiting)."""
        checks.enqueue_check(self.s, self.version(), CheckReason.import_)
        self.s.commit()
        got = q.claim_next(self.s, w, self.settings)
        self.s.commit()
        self.assertIsInstance(got, VersionCheck)
        return got

    def check_row(self, cid):
        self.s.expire_all()
        return self.s.get(VersionCheck, cid)

    def report(self):
        return {"outcome": "ready", "reason": None, "programs": [],
                "solutions": [{"name": "ok.py", "expected": "OK", "got": "OK", "ok": True}]}

    # ---------------------------------------------------------------- submissions
    def test_idle_heartbeat_returns_a_job_held_past_the_grace(self):
        w = self.worker()
        _sid, j = self.job(w)
        self.age("judgements", j.id, self.old())
        with self.assertLogs("judge.queue", "WARNING") as logged:
            reply = self.beat(w)
        self.assertIn(f"holds judgement {j.id} attempt 1 but its heartbeat does not name it", logged.output[0])
        self.assertEqual(reply["cancel"], False)
        self.assertEqual(reply["reclaimed"], [{"kind": "judgement", "id": j.id, "attempt": 1, "result": "requeued"}])
        got = self.judgement(j.id)
        self.assertEqual((got.status, got.worker_id, got.failures), (JudgementStatus.queued, None, 1))
        orphan = self.events("orphan_reclaimed")
        self.assertEqual([(e.judgement_id, e.worker_id, e.details["attempt"]) for e in orphan], [(j.id, w.id, 1)])
        self.assertEqual([e.details["reason"] for e in self.events("requeued")], ["worker_idle_holding"])
        self.s.commit()
        q.read_one_moment(self.s)
        m = q.collect_metrics(self.s, self.settings)
        self.s.commit()
        self.assertEqual(m["orphan_reclaims"], 1)
        self.assertIn("judge_orphan_reclaims 1", metrics.render_prometheus(m))

    def test_a_job_just_claimed_is_not_touched(self):
        w = self.worker()
        _sid, j = self.job(w)  # claimed now: an idle heartbeat sent just before the claim answered
        self.age("judgements", j.id, self.settings.heartbeat_timeout_s - 5)  # still inside the grace
        self.assertEqual(self.beat(w)["reclaimed"], [])
        got = self.judgement(j.id)
        self.assertEqual((got.status, got.worker_id, got.failures), (JudgementStatus.compiling, w.id, 0))

    def test_a_busy_heartbeat_about_another_job_returns_the_held_one(self):
        w = self.worker()
        _s1, named = self.job(w, user=1)
        _s2, held = self.job(w, user=2)  # a second one the worker lost track of
        self.age("judgements", named.id, self.old())
        self.age("judgements", held.id, self.old())
        reply = self.beat(w, judgement_id=named.id, attempt=named.attempt, state="busy")
        self.assertEqual([r["id"] for r in reply["reclaimed"]], [held.id])
        self.assertEqual(self.judgement(named.id).status, JudgementStatus.compiling, "the named one stays")
        self.assertEqual(self.judgement(held.id).status, JudgementStatus.queued)
        # the same id with another attempt is not «named»
        _s3, other = self.job(w, user=3)
        self.age("judgements", other.id, self.old())
        reply = self.beat(w, judgement_id=other.id, attempt=other.attempt + 1, state="busy")
        self.assertIn(other.id, [r["id"] for r in reply["reclaimed"]])

    def test_a_result_being_recorded_is_not_reclaimed(self):
        """finish holds the row (FOR UPDATE) while it records the result; an idle heartbeat meanwhile skips it
        (SKIP LOCKED) instead of waiting or reclaiming, and the result is accepted."""
        w = self.worker()
        _sid, j = self.job(w)
        self.age("judgements", j.id, self.old())
        with self.Session() as recording:
            row = recording.execute(select(Judgement).where(Judgement.id == j.id).with_for_update()).scalar_one()
            self.assertEqual(self.beat(w)["reclaimed"], [])  # returns at once: the row is locked
            self.assertEqual(q.finish(recording, recording.get(Worker, w.id), j.id, self.ok_payload(row),
                                      self.settings), "recorded")
            recording.commit()
        self.assertEqual(self.judgement(j.id).status, JudgementStatus.finished)
        self.assertEqual(self.beat(w)["reclaimed"], [], "finished: not in flight any more")

    def test_the_old_holder_is_refused_and_the_new_verdict_stays(self):
        a, b = self.worker("a"), self.worker("b")
        _sid, j = self.job(a)
        self.age("judgements", j.id, self.old())
        self.beat(a)  # reclaimed from a
        j2 = q.claim(self.s, self.s.get(Worker, b.id), self.settings)
        self.s.commit()
        self.assertEqual((j2.id, j2.attempt), (j.id, 2))
        self.assertEqual(q.finish(self.s, self.s.get(Worker, b.id), j.id, self.ok_payload(j2), self.settings), "recorded")
        self.s.commit()
        stale = dict(self.ok_payload(j2), attempt=1, verdict="WA", score="0")
        with self.assertRaises(q.LeaseLost):
            q.finish(self.s, self.s.get(Worker, a.id), j.id, stale, self.settings)
        self.s.rollback()
        self.assertEqual(self.beat(a, judgement_id=j.id, attempt=1, state="busy")["cancel"], True)
        got = self.judgement(j.id)
        self.assertEqual((got.status, got.verdict.value, got.worker_id), (JudgementStatus.finished, "OK", b.id))

    def test_repeated_orphaning_ends_in_ie_and_a_late_result_is_refused(self):
        w = self.worker()
        _sid, j = self.job(w)
        sid = _sid
        results = []
        for attempt in range(1, self.settings.max_attempts + 1):
            if attempt > 1:
                got = q.claim(self.s, self.s.get(Worker, w.id), self.settings)
                self.s.commit()
                self.assertEqual(got.id, j.id)
            self.age("judgements", j.id, self.old())
            results.append(self.beat(w)["reclaimed"][0]["result"])
        self.assertEqual(results, ["requeued", "requeued", "gave_up"])
        got = self.judgement(j.id)
        self.assertEqual((got.status, got.verdict.value, got.failures, got.worker_id, got.attempt),
                         (JudgementStatus.internal_error, "IE", 3, w.id, 3))
        self.assertTrue(q.server_gave_up(got), got.error)  # the mark: judgements.server_gave_up_at (D-090)
        self.assertIsNotNone(got.server_gave_up_at)
        self.assertTrue(got.error.startswith("3 failed attempts; last reason: "), got.error)
        self.assertEqual((got.score, got.max_score), (0, scores.version_max_score(self.s, got.problem_version_id)))
        # the last holder, same worker and attempt, delivers late: 409, not already_recorded
        with self.assertRaises(q.LeaseLost):
            q.finish(self.s, self.s.get(Worker, w.id), j.id, dict(self.ok_payload(got), attempt=3), self.settings)
        self.s.rollback()
        self.assertEqual(self.judgement(j.id).verdict.value, "IE")
        # who judged it is still shown everywhere: the submission's judgements, judgectl queue judgements
        self.assertEqual(view.judgements_of(self.s, sid)[0]["worker"], "w0")
        self.assertEqual(self.judgectl_queue_judgements()[0]["worker"], "w0")

    def test_a_worker_reported_ie_is_not_mistaken_for_the_servers_mark(self):
        settings = self.settings
        w = self.worker()
        _sid, j = self.job(w)
        j.failures = settings.max_attempts - 1  # this IE is the last attempt: recorded as given up by the worker
        self.s.commit()
        text = "the server gave up after 3 (worker text)"  # what the old text mark looked like (D-069)
        payload = {"attempt": j.attempt, "verdict": "IE", "score": "0", "error": text}
        self.assertEqual(q.finish(self.s, self.s.get(Worker, w.id), j.id, payload, settings), "recorded")
        self.s.commit()
        recorded = self.judgement(j.id)
        self.assertEqual(recorded.error, text)  # kept as the worker wrote it: the mark is a column now
        self.assertFalse(q.server_gave_up(recorded))
        self.assertIsNone(recorded.server_gave_up_at)
        self.assertEqual(recorded.max_score, scores.version_max_score(self.s, recorded.problem_version_id))
        self.assertEqual(q.finish(self.s, self.s.get(Worker, w.id), j.id, payload, settings), "already_recorded",
                         "the same result again is a network retry, not a late result")

    def judgectl_queue_judgements(self):
        from app.judgectl.cli import main
        from app.judgectl.core import Context
        out = io.StringIO()
        ctx = Context(session_factory=self.Session, settings=Settings(), actor=audit.Actor(cli="cli:test"),
                      out=out, err=io.StringIO(), interactive=lambda: False)
        self.assertEqual(main(["queue", "judgements", "--json"], ctx), 0)
        return json.loads(out.getvalue())

    # ---------------------------------------------------------------- checks of problem versions
    def test_checks_idle_heartbeat_returns_a_held_check(self):
        w = self.worker()
        c = self.check(w)
        self.age("version_checks", c.id, self.old())
        reply = self.beat(w)
        self.assertEqual(reply["reclaimed"], [{"kind": "check", "id": c.id, "attempt": 1, "result": "requeued"}])
        got = self.check_row(c.id)
        self.assertEqual((got.status, got.worker_id, got.failures), (CheckStatus.queued, None, 1))
        self.assertEqual([e.details["check_id"] for e in self.events("orphan_reclaimed")], [c.id])

    def test_checks_just_claimed_or_named_are_not_touched(self):
        w = self.worker()
        c = self.check(w)
        self.assertEqual(self.beat(w)["reclaimed"], [], "inside the grace")
        self.age("version_checks", c.id, self.old())
        self.assertEqual(self.beat(w, check_id=c.id, attempt=c.attempt, state="busy")["reclaimed"], [], "named")
        self.assertEqual(self.check_row(c.id).status, CheckStatus.running)

    def test_checks_a_heartbeat_about_a_judgement_returns_the_held_check(self):
        w = self.worker()
        c = self.check(w)
        _sid, j = self.job(w)
        self.age("version_checks", c.id, self.old())
        self.age("judgements", j.id, self.old())
        reply = self.beat(w, judgement_id=j.id, attempt=j.attempt, state="busy")
        self.assertEqual([(r["kind"], r["id"]) for r in reply["reclaimed"]], [("check", c.id)])

    def test_checks_a_result_being_recorded_is_not_reclaimed(self):
        w = self.worker()
        c = self.check(w)
        self.age("version_checks", c.id, self.old())
        with self.Session() as recording:
            recording.execute(select(VersionCheck).where(VersionCheck.id == c.id).with_for_update()).scalar_one()
            self.assertEqual(self.beat(w)["reclaimed"], [])
            self.assertEqual(checks.finish(recording, recording.get(Worker, w.id), c.id,
                                           {"attempt": c.attempt, "outcome": "ready", "report": self.report()},
                                           self.settings), "recorded")
            recording.commit()
        self.assertEqual(self.check_row(c.id).status, CheckStatus.done)

    def test_checks_the_old_holder_is_refused(self):
        a, b = self.worker("a"), self.worker("b")
        c = self.check(a)
        self.age("version_checks", c.id, self.old())
        self.beat(a)
        c2 = q.claim_next(self.s, self.s.get(Worker, b.id), self.settings)
        self.s.commit()
        self.assertEqual((c2.id, c2.attempt), (c.id, 2))
        with self.assertRaises(checks.CheckLeaseLost):
            checks.finish(self.s, self.s.get(Worker, a.id), c.id,
                          {"attempt": 1, "outcome": "failed", "report": dict(self.report(), outcome="failed",
                                                                            reason="solution_mismatch")},
                          self.settings)
        self.s.rollback()
        self.assertTrue(self.beat(a, check_id=c.id, attempt=1, state="busy")["cancel"])
        self.assertEqual(checks.finish(self.s, self.s.get(Worker, b.id), c.id,
                                       {"attempt": 2, "outcome": "ready", "report": self.report()}, self.settings),
                         "recorded")

    def test_checks_repeated_orphaning_fails_and_keeps_who_checked(self):
        w = self.worker()
        c = self.check(w)
        results = []
        for attempt in range(1, self.settings.max_attempts + 1):
            if attempt > 1:
                got = q.claim_next(self.s, self.s.get(Worker, w.id), self.settings)
                self.s.commit()
                self.assertEqual(got.id, c.id)
            self.age("version_checks", c.id, self.old())
            results.append(self.beat(w)["reclaimed"][0]["result"])
        self.assertEqual(results, ["requeued", "requeued", "gave_up"])
        got = self.check_row(c.id)
        self.assertEqual((got.status, got.report["by"], got.worker_id), (CheckStatus.done, "server", w.id))
        self.assertEqual(checks.check_row(got)["worker_id"], w.id, "the version card still says who checked")
        with self.assertRaises(checks.CheckLeaseLost):
            checks.finish(self.s, self.s.get(Worker, w.id), c.id,
                          {"attempt": 3, "outcome": "ready", "report": self.report()}, self.settings)
        self.s.rollback()
