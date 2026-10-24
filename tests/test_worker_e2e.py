"""The whole worker against the in-memory fake backend, judging with the UNSAFE local sandbox.

Covers: claim -> manifest -> blobs -> disk cache -> judge -> result, quizzes, cache reuse, fencing,
release on stop, lease loss, superseding, fair claim order, and the real supervisor with real processes
(crash recovery, graceful SIGTERM). The real backend and isolate are NOT involved (see docs/QUEUE.md).
"""
import os
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

from tests.fakebackend import TOKEN, FakeBackend  # noqa: E402
from worker.client import ApiClient  # noqa: E402
from worker.config import WorkerConfig  # noqa: E402
from worker.process import WorkerProcess  # noqa: E402
from worker.topology import WorkerSlot  # noqa: E402

EX = ROOT / "problems-examples"
OK_SUM = (EX / "sum-of-two/solutions/ok.py").read_bytes()
SLOW_SUM = b"import time\ntime.sleep(0.4)\na, b = map(int, input().split())\nprint(a + b)\n"


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


if __name__ == "__main__":
    unittest.main()
