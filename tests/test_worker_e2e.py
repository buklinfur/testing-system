"""The whole worker against the in-memory fake backend, judging with the UNSAFE local sandbox.

Covers: claim -> manifest -> blobs -> disk cache -> judge -> result, quizzes, cache reuse, fencing,
release on stop, lease loss, superseding, fair claim order, checks of problem versions (kinds, ready / failed,
timeout, release on stop), and the real supervisor with real processes (crash recovery, graceful SIGTERM). The real backend and isolate are NOT involved (see docs/QUEUE.md).
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ["JUDGE_ALLOW_UNSAFE_LOCAL"] = "1"

import worker.process as worker_process  # noqa: E402
from tests.fakebackend import TOKEN, FakeBackend  # noqa: E402
from worker.client import ApiClient  # noqa: E402
from worker.config import WorkerConfig  # noqa: E402
from worker.process import WorkerProcess  # noqa: E402
from worker.topology import WorkerSlot  # noqa: E402

EX = ROOT / "problems-examples"
OK_SUM = (EX / "sum-of-two/solutions/ok.py").read_bytes()
SLOW_SUM = b"import time\ntime.sleep(0.4)\na, b = map(int, input().split())\nprint(a + b)\n"
WA_SUM = b"print(0)\n"


def sol(name, source, expect, language="python"):
    return {"name": name, "language": language, "source": source, "expect": expect}


def wait_for(cond, timeout=30.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class WorkerHarness:
    def __init__(self, test: unittest.TestCase, name: str = "t", **overrides):
        self.backend = FakeBackend()
        self.url = self.backend.start()
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.close)
        self.cfg = WorkerConfig(api_url=self.url, token=TOKEN, machine=name, backend="local",
                                cache_dir=Path(self.tmp.name) / "cache", poll_interval_s=0.05,
                                heartbeat_interval_s=0.15, drain_timeout_s=30, **overrides)
        self.workers: list = []
        self.threads: list = []
        self.codes: dict = {}

    def start_worker(self, index: int = 0) -> WorkerProcess:
        worker = WorkerProcess(self.cfg, WorkerSlot(index, (), index * 2))

        def run():
            self.codes[index] = worker.run()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.workers.append(worker)
        self.threads.append(thread)
        return worker

    def stop_all(self):
        for w in self.workers:
            w.stop.set()
        for t in self.threads:
            t.join(15)

    def close(self):
        self.stop_all()
        self.backend.stop()
        self.tmp.cleanup()

    def job(self, jid):
        return self.backend.judgements[jid]


class WorkerE2E(unittest.TestCase):
    def test_correct_solution_and_cache_reuse(self):
        h = WorkerHarness(self)
        manifest = h.backend.add_problem(EX / "sum-of-two", 1)
        all_ids = {t["id"] for g in manifest["groups"] for t in g["tests"]}
        h.start_worker()
        j1 = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j1)["status"] == "finished", what="first job")
        result = h.backend.results[j1]
        self.assertEqual(result["verdict"], "OK")
        self.assertEqual(Decimal(result["score"]), Decimal(100))
        ids = [t["test_case_id"] for t in result["tests"]]
        self.assertEqual(set(ids), all_ids)
        self.assertEqual(ids, [t["id"] for g in manifest["groups"] for t in g["tests"]], "database order is kept")
        visible = {t["test_case_id"] for t in result["tests"] if t["visible_to_student"]}
        self.assertTrue(visible and visible < all_ids, "samples are open, the rest hidden")
        self.assertEqual({g["name"] for g in result["summary"]["groups"]}, {g["name"] for g in manifest["groups"]})
        downloads, manifests = h.backend.blob_downloads, h.backend.manifest_downloads
        j2 = h.backend.submit(user=1, version_id=1, source=OK_SUM + b"# again\n")
        wait_for(lambda: h.job(j2)["status"] == "finished", what="second job")
        self.assertEqual(h.backend.manifest_downloads, manifests, "manifest is not fetched twice")
        self.assertEqual(h.backend.blob_downloads - downloads, 1, "only the new source is downloaded")

    def test_extra_job_fields_are_ignored_and_checks_are_not_taken_without_the_capability(self):
        """Protocol v1 only grows: a judgement job now carries `kind` (and may carry fields a later backend adds);
        the worker reads the fields it knows by name. A worker that does not announce problem_check never gets a
        check, even when one is queued and nothing else is."""
        h = WorkerHarness(self, capabilities=("cpu",))
        h.backend.add_problem(EX / "sum-of-two", 1)
        check = h.backend.add_check(1, [{"name": "solutions/ok.py", "language": "python", "source": OK_SUM,
                                         "expect": "OK"}])
        real = h.backend._claim_judgement

        def with_future_fields(worker):
            job = real(worker)
            if job:
                job.update(future_field={"nested": [1, 2]}, another="x")
            return job
        h.backend._claim_judgement = with_future_fields
        h.start_worker()
        j1 = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j1)["status"] == "finished", what="the job")
        self.assertEqual(h.backend.results[j1]["verdict"], "OK")
        time.sleep(0.5)  # several idle polls
        self.assertEqual((h.backend.checks[check]["status"], h.backend.checks[check]["attempt"]), ("queued", 0))

    def test_second_process_reuses_the_disk_cache(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        w0 = h.start_worker(0)
        j1 = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j1)["status"] == "finished")
        w0.stop.set()
        h.threads[0].join(10)
        before = h.backend.blob_downloads, h.backend.manifest_downloads
        h.start_worker(1)  # a "restarted" process with an empty memory
        j2 = h.backend.submit(user=1, version_id=1, source=OK_SUM + b"#\n")
        wait_for(lambda: h.job(j2)["status"] == "finished")
        self.assertEqual(h.backend.manifest_downloads, before[1])
        self.assertEqual(h.backend.blob_downloads - before[0], 1)

    def test_custom_checker_problem_and_wrong_answer(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "pair-with-sum", 2)
        h.start_worker()
        ok = h.backend.submit(user=1, version_id=2, source=(EX / "pair-with-sum/solutions/ok.py").read_bytes())
        bad = h.backend.submit(user=1, version_id=2, source=(EX / "pair-with-sum/solutions/wa.py").read_bytes())
        wait_for(lambda: all(h.job(j)["status"] == "finished" for j in (ok, bad)))
        self.assertEqual(h.backend.results[ok]["verdict"], "OK")
        self.assertEqual(h.backend.results[bad]["verdict"], "WA")
        self.assertEqual(Decimal(h.backend.results[bad]["score"]), 0)
        jury = list((Path(h.tmp.name) / "cache" / "jury").glob("*.sum"))
        self.assertEqual(len(jury), 1, "the compiled checker is cached on disk once")

    def test_compile_error_is_a_verdict_not_a_failure(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=b"int main( {", language="cpp")
        wait_for(lambda: h.job(j)["status"] == "finished")
        self.assertEqual(h.backend.results[j]["verdict"], "CE")
        self.assertTrue(h.backend.results[j]["compile_log"])
        self.assertEqual(h.job(j)["failures"], 0)

    def test_quiz(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "quiz-python-basics", 3)
        h.start_worker()
        j = h.backend.submit(user=1, version_id=3, language=None,
                             answers={"q1": ["b"], "q2": ["a", "b"], "q3": ["b"]})
        wait_for(lambda: h.job(j)["status"] == "finished")
        r = h.backend.results[j]
        self.assertEqual(r["tests"], [])
        self.assertEqual(Decimal(r["score"]), Decimal(r["max_score"]))
        self.assertEqual(r["verdict"], "OK")

    def test_unknown_language_gives_up_after_max_attempts(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=b"x", language="brainfuck")
        wait_for(lambda: h.job(j)["status"] == "internal_error", what="poison submission to give up")
        self.assertEqual(h.job(j)["attempt"], 3)
        self.assertEqual(h.job(j)["verdict"], "IE")

    def test_broken_problem_data_is_internal_error_not_a_crash(self):
        h = WorkerHarness(self)
        m = h.backend.add_problem(EX / "sum-of-two", 1)
        missing = m["groups"][0]["tests"][0]["input_sha256"]
        del h.backend.blobs[missing]  # blob vanished from the backend's store
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j)["status"] == "internal_error")
        self.assertTrue(h.threads[0].is_alive(), "the worker survived")

    def test_stop_gives_the_running_job_back_without_counting_a_failure(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=SLOW_SUM)
        wait_for(lambda: h.job(j)["status"] in ("compiling", "running") and (h.job(j)["done"] or 0) >= 1, what="progress")
        w.stop.set()
        w.abort.set()  # what the drain timeout does
        h.threads[0].join(20)
        self.assertEqual(h.job(j)["status"], "queued")
        self.assertEqual(h.job(j)["failures"], 0)
        self.assertEqual(h.codes[0], 0)
        self.assertEqual(h.backend.workers["t-w0"]["state"], "stopped")

    def test_graceful_stop_finishes_the_current_job_first(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=SLOW_SUM)
        wait_for(lambda: h.job(j)["status"] in ("compiling", "running"))
        w.stop.set()  # SIGTERM: no new jobs, but the current one completes
        h.threads[0].join(60)
        self.assertEqual(h.job(j)["status"], "finished")
        self.assertEqual(h.backend.results[j]["verdict"], "OK")

    def test_lease_lost_abandons_the_job_and_result_is_not_delivered_twice(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=SLOW_SUM)
        wait_for(lambda: h.job(j)["status"] in ("compiling", "running") and (h.job(j)["done"] or 0) >= 1)
        h.backend.expire_worker("t-w0")  # the reaper decides the worker is dead
        wait_for(lambda: h.job(j)["failures"] == 1, what="the reaper to take the job")
        time.sleep(1.0)  # the heartbeat tells the worker; it must not deliver the stale attempt
        w.stop.set()
        w.abort.set()
        h.threads[0].join(20)
        self.assertTrue(all(r["attempt"] != 1 for r in h.backend.results.values()), "stale attempt delivered nothing")

    def test_stale_result_from_an_old_attempt_is_rejected(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        c1, c2 = ApiClient(h.url, TOKEN), ApiClient(h.url, TOKEN)
        c1.register("a", ["cpu"], {})
        c2.register("b", ["cpu"], {})
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        job = c1.claim()
        h.backend.expire_worker("a")
        job2 = c2.claim()
        self.assertEqual((job["judgement_id"], job2["attempt"]), (j, 2))
        from worker.client import ApiConflict
        with self.assertRaises(ApiConflict):
            c1.submit_result(j, {"attempt": 1, "verdict": "OK", "score": "100", "tests": []})
        self.assertEqual(c2.submit_result(j, {"attempt": 2, "verdict": "OK", "score": "100", "tests": []})["status"], "recorded")
        self.assertEqual(c2.submit_result(j, {"attempt": 2, "verdict": "OK", "score": "100", "tests": []})["status"], "already_recorded")

    def test_second_process_with_the_same_name_supersedes_the_first(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        h.start_worker()
        wait_for(lambda: "t-w0" in h.backend.workers)
        intruder = ApiClient(h.url, TOKEN)
        intruder.register("t-w0", ["cpu"], {})
        h.threads[0].join(15)
        self.assertFalse(h.threads[0].is_alive())
        self.assertEqual(h.codes[0], 75)

    def test_wrong_token_is_fatal(self):
        h = WorkerHarness(self)
        h.cfg.token = "nope"
        h.start_worker()
        h.threads[0].join(10)
        self.assertEqual(h.codes[0], 78)

    def test_fair_start_order_between_students(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        jobs_a = [h.backend.submit(user=1, version_id=1, source=OK_SUM + b"#%d\n" % i) for i in range(4)]
        job_b = h.backend.submit(user=2, version_id=1, source=OK_SUM + b"#b\n")
        h.start_worker()
        wait_for(lambda: all(h.job(j)["status"] == "finished" for j in jobs_a + [job_b]), what="all jobs")
        order = sorted(jobs_a + [job_b], key=lambda j: h.job(j)["started_at"])
        self.assertEqual(order[0], jobs_a[0])
        self.assertEqual(order[1], job_b, "student 2 is served second although he was last in line")


class CheckE2E(unittest.TestCase):
    """Checks of problem versions (4a.4) by the whole worker against the fake backend, local sandbox."""

    def check(self, h, cid):
        return h.backend.checks[cid]

    def done(self, h, cid, timeout=60.0):
        wait_for(lambda: self.check(h, cid)["status"] == "done", timeout=timeout, what=f"check {cid}")
        return self.check(h, cid)["report"]

    def test_a_good_version_is_ready_with_a_full_report(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "pair-with-sum", 1)
        cid = h.backend.add_check(1, [sol("solutions/ok.py", (EX / "pair-with-sum/solutions/ok.py").read_bytes(), "OK"),
                                      sol("solutions/wa.py", (EX / "pair-with-sum/solutions/wa.py").read_bytes(), "WA")])
        h.start_worker()
        report = self.done(h, cid)
        self.assertEqual((self.check(h, cid)["outcome"], report["outcome"], report["reason"]), ("ready", "ready", None))
        self.assertEqual(report["programs"], [{"role": "checker", "name": "check.cpp", "ok": True, "log": ""}])
        self.assertEqual([(r["name"], r["expected"], r["got"], r["ok"]) for r in report["solutions"]],
                         [("solutions/ok.py", "OK", "OK", True), ("solutions/wa.py", "WA", "WA", True)])
        self.assertEqual(report["worker"], "t-w0")
        self.assertGreaterEqual(report["duration_ms"], 0)
        self.assertEqual(self.check(h, cid)["failures"], 0)

    def test_a_submission_is_still_judged_after_a_check(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol("ok.py", OK_SUM, "OK")])
        h.start_worker()
        self.done(h, cid)
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j)["status"] == "finished", what="the submission after the check")
        self.assertEqual(h.backend.results[j]["verdict"], "OK")

    def test_a_wrong_expectation_fails_with_the_first_failed_test(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol("ok.py", OK_SUM, "OK"), sol("liar.py", WA_SUM, "OK")])
        h.start_worker()
        report = self.done(h, cid)
        self.assertEqual((report["outcome"], report["reason"]), ("failed", "solution_mismatch"))
        liar = report["solutions"][1]
        self.assertEqual((liar["expected"], liar["got"], liar["ok"]), ("OK", "WA", False))
        self.assertEqual(liar["first_fail"]["verdict"], "WA")
        self.assertTrue(liar["first_fail"]["test"])

    def test_a_checker_that_does_not_compile_fails_without_running_solutions(self):
        h = WorkerHarness(self)
        root = Path(h.tmp.name) / "broken"
        shutil.copytree(EX / "pair-with-sum", root)
        (root / "checker/check.cpp").write_text("int main( { not c++\n")
        h.backend.add_problem(root, 1)
        cid = h.backend.add_check(1, [sol("solutions/ok.py", (root / "solutions/ok.py").read_bytes(), "OK")])
        h.start_worker()
        report = self.done(h, cid)
        self.assertEqual((report["outcome"], report["reason"], report["solutions"]),
                         ("failed", "checker_compile_failed", []))
        self.assertFalse(report["programs"][0]["ok"])
        self.assertIn("check.cpp", report["programs"][0]["log"])

    def test_the_deadline_gives_a_partial_report_with_reason_timeout(self):
        h = WorkerHarness(self)
        h.backend.check_deadline_s = 2
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol("fast.py", OK_SUM, "OK")] + [sol(f"slow{i}.py", SLOW_SUM, "OK") for i in range(4)])
        h.start_worker()
        report = self.done(h, cid, timeout=60)
        self.assertEqual((report["outcome"], report["reason"]), ("failed", "timeout"))
        names = [r["name"] for r in report["solutions"]]
        self.assertEqual(names[0], "fast.py", "the finished rows are kept")
        self.assertLess(len(names), 5, "the check stopped early")
        self.assertIn("exceeded", report["error"])
        self.assertEqual(self.check(h, cid)["failures"], 0, "a timeout is a result, not a failure of the worker")

    def test_a_stop_gives_the_check_back_at_once_without_a_failure(self):
        """SIGTERM sets `stop`: a running check is released at once (a submission would be finished first)."""
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol(f"slow{i}.py", SLOW_SUM, "OK") for i in range(5)])
        w = h.start_worker()
        wait_for(lambda: self.check(h, cid)["status"] == "running" and (self.check(h, cid)["done"] or 0) >= 1,
                 what="the check to make progress")
        w.stop.set()
        h.threads[0].join(20)
        c = self.check(h, cid)
        self.assertEqual((c["status"], c["failures"], c["attempt"], c["outcome"]), ("queued", 0, 1, None))
        self.assertEqual(h.codes[0], 0)
        self.assertEqual(h.backend.workers["t-w0"]["state"], "stopped")

    def test_lease_lost_abandons_the_check(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol(f"slow{i}.py", SLOW_SUM, "OK") for i in range(5)])
        h.start_worker()
        wait_for(lambda: self.check(h, cid)["status"] == "running", what="the check to start")
        with h.backend.lock:  # what the reaper does: back to the queue, counted as a failure
            h.backend._fail_check(self.check(h, cid), "worker_lost")
            h.backend.max_concurrent_checks = 0  # nobody takes it again during the test
        time.sleep(1.5)
        c = self.check(h, cid)
        self.assertEqual((c["status"], c["failures"]), ("queued", 1))
        self.assertNotIn(cid, h.backend.check_results, "the abandoned check sent no result")

    def test_an_unknown_job_kind_is_given_back_not_run(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        real = h.backend._claim_judgement
        sent = []

        def future_kind(worker):
            job = real(worker)
            if job and not sent:
                sent.append(job["judgement_id"])
                job["kind"] = "gpu_training"
            return job
        h.backend._claim_judgement = future_kind
        pause = worker_process.UNKNOWN_KIND_PAUSE_S
        worker_process.UNKNOWN_KIND_PAUSE_S = 0.2
        self.addCleanup(setattr, worker_process, "UNKNOWN_KIND_PAUSE_S", pause)
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j)["status"] == "finished", what="the job, given back once and then claimed normally")
        self.assertEqual((h.job(j)["attempt"], h.job(j)["failures"]), (2, 0), "the release is not a failure")
        self.assertEqual(h.backend.results[j]["attempt"], 2, "attempt 1 (unknown kind) was never judged")
        self.assertTrue(h.threads[0].is_alive(), "the worker did not crash")
        self.assertEqual(w._stats["jobs_released"], 1)
        self.assertIn("gpu_training", w._stats["last_error"])

    def test_a_release_path_outside_the_api_is_not_followed(self):
        """The unknown job points `release` at another host: the worker logs, sends nothing there, keeps working."""
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        thief = FakeBackend()
        thief_url = thief.start()
        self.addCleanup(thief.stop)
        seen = []
        real_handle = thief.handle
        thief.handle = lambda method, path, headers, body: (seen.append((path, headers.get("Authorization"))),
                                                           real_handle(method, path, headers, body))[1]
        real = h.backend._claim_judgement
        sent = []

        def hostile(worker):
            job = real(worker)
            if job and not sent:
                sent.append(job["judgement_id"])
                job.update(kind="gpu_training", release=thief_url + "/steal")
            return job
        h.backend._claim_judgement = hostile
        pause = worker_process.UNKNOWN_KIND_PAUSE_S
        worker_process.UNKNOWN_KIND_PAUSE_S = 0.2
        self.addCleanup(setattr, worker_process, "UNKNOWN_KIND_PAUSE_S", pause)
        w = h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: w._stats["last_error"] is not None, what="the unknown job to be refused")
        self.assertEqual(seen, [], "no request (and no token) reached the other host")
        self.assertEqual(w._stats["jobs_released"], 0)
        self.assertEqual((h.job(j)["status"], h.job(j)["attempt"]), ("compiling", 1), "left to the server's timeout")
        self.assertTrue(h.threads[0].is_alive())

    def test_a_job_without_kind_is_a_judgement(self):
        """A backend older than 4a.4 sends no `kind`."""
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        real = h.backend._claim_judgement

        def old_backend(worker):
            job = real(worker)
            if job:
                job.pop("kind")
                job.pop("release")
            return job
        h.backend._claim_judgement = old_backend
        h.start_worker()
        j = h.backend.submit(user=1, version_id=1, source=OK_SUM)
        wait_for(lambda: h.job(j)["status"] == "finished")
        self.assertEqual(h.backend.results[j]["verdict"], "OK")


class SupervisorE2E(unittest.TestCase):
    def start_supervisor(self, h: WorkerHarness, workers: int = 2) -> subprocess.Popen:
        env = dict(os.environ, JUDGE_API_URL=h.url, JUDGE_WORKER_TOKEN=TOKEN, JUDGE_BACKEND="local",
                   JUDGE_ALLOW_UNSAFE_LOCAL="1", JUDGE_PIN_CPUS="0", JUDGE_WORKERS=str(workers),
                   JUDGE_MACHINE_NAME="sup", JUDGE_CACHE_DIR=str(Path(h.tmp.name) / "supcache"),
                   JUDGE_POLL_INTERVAL_S="0.05", JUDGE_HEARTBEAT_INTERVAL_S="0.2", JUDGE_DRAIN_TIMEOUT_S="20")
        proc = subprocess.Popen([sys.executable, "-m", "worker", "run"], cwd=ROOT, env=env)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc

    def test_processes_jobs_in_parallel_and_stops_on_sigterm(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        sup = self.start_supervisor(h, workers=2)
        jobs = [h.backend.submit(user=i % 3, version_id=1, source=OK_SUM + b"#%d\n" % i) for i in range(6)]
        wait_for(lambda: all(h.job(j)["status"] == "finished" for j in jobs), timeout=60, what="6 jobs")
        self.assertEqual({h.job(j)["verdict"] for j in jobs}, {"OK"})
        self.assertEqual({w["info"]["boxes"][0] for w in h.backend.workers.values()}, {0, 2}, "distinct isolate boxes")
        sup.send_signal(signal.SIGTERM)
        self.assertEqual(sup.wait(timeout=30), 0)
        self.assertEqual({w["state"] for w in h.backend.workers.values()}, {"stopped"})

    def test_crashed_worker_is_restarted_and_its_job_is_redone(self):
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        sup = self.start_supervisor(h, workers=1)
        j = h.backend.submit(user=1, version_id=1, source=SLOW_SUM)
        wait_for(lambda: h.job(j)["status"] in ("compiling", "running") and (h.job(j)["done"] or 0) >= 1, 30, "job to start")
        pid = h.backend.workers["sup-w0"]["info"]["pid"]
        os.kill(pid, signal.SIGKILL)
        wait_for(lambda: h.job(j)["status"] == "finished", timeout=90, what="job redone after the crash")
        self.assertEqual(h.job(j)["failures"], 1)
        self.assertEqual(h.job(j)["attempt"], 2)
        self.assertNotEqual(h.backend.workers["sup-w0"]["info"]["pid"], pid)
        sup.send_signal(signal.SIGTERM)
        sup.wait(timeout=30)

    def test_sigterm_during_a_check_releases_it(self):
        """A real SIGTERM to the supervisor (it passes it on to the worker processes): the check goes back to the
        queue at once, not as a failure, and the supervisor exits 0 well before the drain timeout."""
        h = WorkerHarness(self)
        h.backend.add_problem(EX / "sum-of-two", 1)
        cid = h.backend.add_check(1, [sol(f"slow{i}.py", SLOW_SUM, "OK") for i in range(10)])
        sup = self.start_supervisor(h, workers=1)
        wait_for(lambda: h.backend.checks[cid]["status"] == "running" and (h.backend.checks[cid]["done"] or 0) >= 1,
                 timeout=60, what="the check to make progress")
        t0 = time.monotonic()
        sup.send_signal(signal.SIGTERM)
        self.assertEqual(sup.wait(timeout=30), 0)
        self.assertLess(time.monotonic() - t0, 15, "not drained for the 20 s drain timeout")
        c = h.backend.checks[cid]
        self.assertEqual((c["status"], c["failures"], c["outcome"]), ("queued", 0, None))


if __name__ == "__main__":
    unittest.main()
