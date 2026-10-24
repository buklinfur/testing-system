"""Module 3 unit tests that need neither PostgreSQL nor isolate nor FastAPI."""
import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
os.environ["JUDGE_ALLOW_UNSAFE_LOCAL"] = "1"

from app import loadinfo, metrics  # noqa: E402
from app.blobstore import BlobStore  # noqa: E402
from protocol.manifest import ManifestError, assemble_manifest, manifest_digest, validate_manifest  # noqa: E402
from runner.config import RunnerConfig  # noqa: E402
from runner.jurycache import MemoryJuryCache, jury_key  # noqa: E402
from runner.judge import Judge, JudgeCancelled  # noqa: E402
from runner.package import Problem  # noqa: E402
from worker import topology  # noqa: E402
from worker.cache import DiskCache, DiskJuryCache, IntegrityError  # noqa: E402
from worker.config import WorkerConfig  # noqa: E402
from worker.supervisor import Supervisor, restart_delay  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TopologyTests(unittest.TestCase):
    def make_sysfs(self, tmp, siblings):
        for cpu, text in siblings.items():
            d = Path(tmp) / f"cpu{cpu}" / "topology"
            d.mkdir(parents=True)
            (d / "thread_siblings_list").write_text(text + "\n")

    def test_cpu_lists(self):
        self.assertEqual(topology.parse_cpu_list("0-2,8,10-11"), [0, 1, 2, 8, 10, 11])
        self.assertEqual(topology.format_cpu_list([0, 1, 2, 8, 10, 11]), "0-2,8,10-11")
        with self.assertRaises(ValueError):
            topology.parse_cpu_list("3-1")
        with self.assertRaises(ValueError):
            topology.parse_cpu_list("a")

    def test_ryzen_like_layout_both_enumerations(self):
        for layout in ("adjacent", "split"):  # siblings (0,1),(2,3).. or (0,8),(1,9)..
            with tempfile.TemporaryDirectory() as tmp:
                sib = {}
                for cpu in range(16):
                    core = cpu // 2 if layout == "adjacent" else cpu % 8
                    mates = (2 * core, 2 * core + 1) if layout == "adjacent" else (core, core + 8)
                    sib[cpu] = f"{mates[0]},{mates[1]}" if layout == "split" else f"{mates[0]}-{mates[1]}"
                self.make_sysfs(tmp, sib)
                cores = topology.read_cores(Path(tmp), allowed=range(16))
                self.assertEqual(len(cores), 8)
                slots = topology.plan_workers(cores, workers=5, reserve_cores=1, box_base=0)
                self.assertEqual(len(slots), 5)
                self.assertTrue(all(len(s.cpus) == 2 for s in slots))
                used = [c for s in slots for c in s.cpus]
                self.assertEqual(len(used), len(set(used)), "cores must not overlap")
                self.assertNotIn(cores[0][0], used, "the first core stays free")
                self.assertEqual([s.box_base for s in slots], [0, 2, 4, 6, 8])

    def test_fewer_free_cores_than_workers_reduces_workers(self):
        cores = [(0, 1), (2, 3), (4, 5)]
        self.assertEqual(len(topology.plan_workers(cores, workers=6, reserve_cores=1)), 2)
        with self.assertRaises(ValueError):
            topology.plan_workers([(0,)], workers=2, reserve_cores=1)

    def test_missing_topology_falls_back_to_single_cpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(topology.read_cores(Path(tmp), allowed=[0, 1, 2]), [(0,), (1,), (2,)])

    def test_explicit_plan_and_overlap(self):
        self.assertEqual(topology.parse_explicit_plan("1-2;3-4;5"), [(1, 2), (3, 4), (5,)])
        with self.assertRaises(ValueError):
            topology.parse_explicit_plan("1-2;2-3")

    def test_box_base_offset(self):
        slots = topology.plan_workers([], workers=3, box_base=10, pin=False)
        self.assertEqual([s.boxes for s in slots], [(10, 11), (12, 13), (14, 15)])

    def test_affinity_is_applied_and_inherited(self):
        import subprocess
        cpu = sorted(os.sched_getaffinity(0))[0]
        old = os.sched_getaffinity(0)
        try:
            topology.apply_affinity((cpu,))
            out = subprocess.run([sys.executable, "-c", "import os;print(sorted(os.sched_getaffinity(0)))"],
                                 capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, str([cpu]))
        finally:
            os.sched_setaffinity(0, old)


class FairnessTests(unittest.TestCase):
    def row(self, id, user, priority=0, created=0):
        return {"id": id, "user_id": user, "priority": priority, "created_at": created}

    def test_one_student_cannot_starve_others(self):
        queued = [self.row(i, user=1, created=i) for i in range(1, 8)] + [self.row(100, user=2, created=50)]
        order = [r["id"] for r in loadinfo.fair_order(queued, inflight={}, recent={})]
        # all of them have equal in-flight/recent counts here, so age decides: student 2 waits behind seven
        self.assertEqual(order[-1], 100)
        # but as soon as student 1 occupies workers, student 2 jumps the line
        order = [r["id"] for r in loadinfo.fair_order(queued, inflight={1: 2}, recent={1: 2})]
        self.assertEqual(order[0], 100)

    def test_simulated_claims_alternate_between_students(self):
        queued = [self.row(i, user=1, created=i) for i in range(1, 7)] + [self.row(10 + i, user=2, created=10 + i) for i in range(3)]
        inflight, recent, picked = {}, {}, []
        for _ in range(6):  # six claims, nothing finishes in between
            best = loadinfo.fair_order(queued, inflight, recent)[0]
            queued.remove(best)
            picked.append(best["user_id"])
            inflight[best["user_id"]] = inflight.get(best["user_id"], 0) + 1
            recent[best["user_id"]] = recent.get(best["user_id"], 0) + 1
        self.assertEqual(picked[:4], [1, 2, 1, 2])

    def test_rejudges_never_jump_fresh_submissions(self):
        queued = [self.row(1, 1, priority=10, created=0), self.row(2, 2, priority=0, created=99)]
        self.assertEqual(loadinfo.fair_order(queued, {}, {})[0]["id"], 2)

    def test_work_conserving_with_a_single_student(self):
        queued = [self.row(1, 1, created=1), self.row(2, 1, created=2)]
        self.assertEqual(loadinfo.fair_order(queued, {1: 5}, {1: 5})[0]["id"], 1)


class EstimateTests(unittest.TestCase):
    def test_estimates(self):
        e = loadinfo.estimate_wait_seconds
        self.assertIsNone(e(1, 0, 0, 5))
        self.assertEqual(e(1, 5, 2, 4.0), 0.0)  # a free worker takes it now
        self.assertEqual(e(4, 5, 2, 4.0), 2.0)  # 3 ahead, 3 free workers -> 3 ahead is not < 3 -> first wave
        self.assertEqual(e(9, 5, 5, 4.0), 2.0 + 4.0)  # 8 ahead, none free, waves of 5 -> second wave
        self.assertEqual(loadinfo.typical_judge_seconds([]), loadinfo.DEFAULT_JUDGE_SECONDS)
        self.assertEqual(loadinfo.typical_judge_seconds([1, 9, 3]), 3)

    def test_snapshot_levels(self):
        S = loadinfo.LoadSnapshot
        self.assertEqual(S(0, 0, 0, 5).level, "offline")
        self.assertEqual(S(0, 5, 1, 5).level, "free")
        self.assertEqual(S(3, 5, 5, 5).level, "busy")
        self.assertEqual(S(20, 5, 5, 5).level, "overloaded")
        self.assertEqual(S(2, 5, 5, 5, my_position=2, my_wait_seconds=4.2).to_dict()["my_wait_seconds"], 5)


class ManifestTests(unittest.TestCase):
    def build(self, **over):
        t = {"id": 1, "position": 0, "name": "a", "input_sha256": sha(b"1"), "answer_sha256": sha(b"2")}
        g = {"id": 1, "position": 0, "name": "main", "points": 100, "tests": [t]}
        return assemble_manifest({"id": 7, "slug": "p", "type": "batch", "titles": {"ru": "П"}, "config": over.get("config", {})},
                                 [g], [{"role": "checker", "name": "c.cpp", "language": "cpp", "source_sha256": sha(b"c")},
                                       {"role": "validator", "name": "v.cpp", "language": "cpp", "source_sha256": sha(b"v")}])

    def test_digest_is_stable_and_sensitive(self):
        a, b = self.build(), self.build()
        self.assertEqual(manifest_digest(a), manifest_digest(b))
        self.assertNotEqual(manifest_digest(a), manifest_digest(self.build(config={"limits": {"time_ms": 2000}})))
        self.assertEqual(len(a["programs"]), 1, "only checker and interactor go to workers")
        self.assertEqual(a["groups"][0]["points"], "100.00")

    def test_validation(self):
        m = self.build()
        validate_manifest(m)
        m["groups"][0]["tests"][0]["input_sha256"] = "zz"
        with self.assertRaises(ManifestError):
            validate_manifest(m)
        m = self.build()
        m["groups"][0]["tests"].append(dict(m["groups"][0]["tests"][0], id=2, position=1))
        with self.assertRaises(ManifestError):
            validate_manifest(m)


class BlobStoreTests(unittest.TestCase):
    def test_put_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = BlobStore(tmp)
            h = store.put(b"hello")
            self.assertEqual(h, sha(b"hello"))
            self.assertEqual(store.put(b"hello"), h)
            self.assertEqual(store.get(h), b"hello")
            self.assertEqual(b"".join(store.iter_chunks(h)), b"hello")
            self.assertIsNone(store.get(sha(b"nope")))
            with self.assertRaises(ValueError):
                store.path("../../etc/passwd")


class MetricsTests(unittest.TestCase):
    def test_render(self):
        text = metrics.render_prometheus({
            "queue_length": 3, "oldest_queued_age_s": 12.34, "in_flight": 2, "workers_by_state": {"busy": 2, "idle": 1},
            "typical_seconds": 2.5, "median_wait_s": 1.0, "finished_by_verdict": {"OK": 5, 'W"A': 1}, "requeued_total": 4})
        self.assertIn("judge_queue_length 3", text)
        self.assertIn('judge_workers{state="busy"} 2', text)
        self.assertIn('judge_finished_total{verdict="W\\"A"} 1', text)
        self.assertTrue(text.endswith("\n"))


class DiskCacheTests(unittest.TestCase):
    def test_download_verify_and_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp))
            data = b"x" * 1000
            calls = []

            def fetch(write):
                calls.append(1)
                write(data[:400])
                write(data[400:])

            p = cache.get_blob(sha(data), fetch)
            self.assertEqual(p.read_bytes(), data)
            cache.get_blob(sha(data), fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(cache.counters.snapshot()["blob_hits"], 1)

    def test_corrupt_download_is_rejected_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp))
            with self.assertRaises(IntegrityError):
                cache.get_blob(sha(b"good"), lambda write: write(b"evil"))
            self.assertFalse(cache.blob_path(sha(b"good")).exists())
            self.assertEqual([p for p in (Path(tmp) / "blobs").rglob("*") if p.is_file()], [])

    def test_concurrent_processes_download_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, calls = b"y" * 100, []

            def fetch(write):
                calls.append(1)
                time.sleep(0.2)
                write(data)

            def worker():
                DiskCache(Path(tmp)).get_blob(sha(data), fetch)

            threads = [threading.Thread(target=worker) for _ in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(len(calls), 1)

    def test_jury_cache_detects_damage(self):
        with tempfile.TemporaryDirectory() as tmp:
            jc = DiskJuryCache(DiskCache(Path(tmp)))
            key = sha(b"key")
            self.assertIsNone(jc.get(key))
            jc.put(key, b"\x7fELF-binary")
            self.assertEqual(jc.get(key), b"\x7fELF-binary")
            (Path(tmp) / "jury" / key).write_bytes(b"tampered")
            self.assertIsNone(jc.get(key))
            self.assertFalse((Path(tmp) / "jury" / key).exists())

    def test_gc_evicts_old_entries_but_not_recent_or_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = DiskCache(Path(tmp))
            old, fresh, linked = b"o" * 1000, b"f" * 1000, b"l" * 1000
            for data in (old, fresh, linked):
                cache.get_blob(sha(data), lambda w, d=data: w(d))
            now = time.time()
            os.utime(cache.blob_path(sha(old)), (now - 7200, now - 7200))
            os.utime(cache.blob_path(sha(linked)), (now - 7200, now - 7200))
            pdir = cache.problem_dir(sha(b"problem"))
            pdir.mkdir()
            os.link(cache.blob_path(sha(linked)), pdir / "t.in")
            os.utime(pdir, (now, now))  # the problem directory is in use right now
            removed = cache.gc(max_bytes=1500, min_age_s=3600)
            self.assertEqual(removed["blobs"], 1)
            self.assertFalse(cache.blob_path(sha(old)).exists())
            self.assertTrue(cache.blob_path(sha(fresh)).exists())
            self.assertTrue(cache.blob_path(sha(linked)).exists())
            self.assertTrue(pdir.exists())


class SupervisorTests(unittest.TestCase):
    class FakeProc:
        def __init__(self):
            self.code = None
            self.signals = []

        def poll(self):
            return self.code

        def send_signal(self, sig):
            self.signals.append(sig)

        def kill(self):
            self.code = -9

        def wait(self):
            return self.code

    def test_restart_with_backoff_and_no_restart_while_stopping(self):
        clock = [0.0]
        procs = []

        def spawn(slot):
            p = self.FakeProc()
            procs.append(p)
            return p

        slots = topology.plan_workers([], workers=2, pin=False)
        sup = Supervisor(WorkerConfig(token="t"), slots, spawn=spawn, clock=lambda: clock[0])
        sup.tick()
        self.assertEqual(len(procs), 2)
        procs[0].code = 1  # crash
        sup.tick()
        self.assertEqual(len(procs), 2, "not restarted immediately")
        clock[0] += 1.1
        sup.tick()
        self.assertEqual(len(procs), 3)
        procs[2].code = 1  # crashes again: backoff doubles
        sup.tick()
        clock[0] += 1.5
        sup.tick()
        self.assertEqual(len(procs), 3)
        clock[0] += 1.0
        sup.tick()
        self.assertEqual(len(procs), 4)
        sup.request_stop()
        procs[3].code = 0
        clock[0] += 100
        sup.tick()
        self.assertEqual(len(procs), 4, "no restarts while stopping")

    def test_delays(self):
        self.assertEqual(restart_delay(1, 1), 1.0)
        self.assertEqual(restart_delay(3, 1), 4.0)
        self.assertEqual(restart_delay(20, 1), 60.0)
        self.assertEqual(restart_delay(1, 78), 60.0)
        self.assertEqual(restart_delay(1, 75), 60.0)


EXAMPLES = ROOT / "problems-examples"


class RunnerHookTests(unittest.TestCase):
    def test_cancel_and_progress(self):
        problem = Problem.load(EXAMPLES / "sum-of-two")
        judge = Judge(RunnerConfig(backend="local"))
        seen = []
        report = judge.judge_submission(problem, "python", (EXAMPLES / "sum-of-two/solutions/ok.py").read_bytes(),
                                        progress=lambda *a: seen.append(a))
        total = sum(len(g.tests) for g in problem.groups)
        self.assertEqual(seen[0], ("compiling", 0, total))
        self.assertEqual(seen[-1], ("running", total, total))
        self.assertEqual([s[1] for s in seen if s[0] == "running"], list(range(total + 1)))
        self.assertEqual(report.score, report.max_score)
        calls = []
        with self.assertRaises(JudgeCancelled):
            judge.judge_submission(problem, "python", b"print(1)", cancel=lambda: calls.append(1) or len(calls) > 3)

    def test_jury_binary_is_compiled_once_with_a_shared_cache(self):
        problem = Problem.load(EXAMPLES / "pair-with-sum")
        sol = (EXAMPLES / "pair-with-sum/solutions/ok.py").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            disk = DiskJuryCache(DiskCache(Path(tmp)))
            first = Judge(RunnerConfig(backend="local", toolchain_id="t1"), jury_cache=disk)
            self.assertEqual(first.judge_submission(problem, "python", sol).score, 100)
            self.assertEqual(disk.cache.counters.snapshot()["jury_misses"], 1)
            second = Judge(RunnerConfig(backend="local", toolchain_id="t1"), jury_cache=disk)  # new process, same disk
            self.assertEqual(second.judge_submission(problem, "python", sol).score, 100)
            self.assertEqual(disk.cache.counters.snapshot()["jury_hits"], 1, "read from disk once per Judge, not per test")
            third = Judge(RunnerConfig(backend="local", toolchain_id="t2"), jury_cache=disk)  # new compiler: rebuilt
            third.judge_submission(problem, "python", sol)
            self.assertEqual(disk.cache.counters.snapshot()["jury_misses"], 2)

    def test_jury_key_depends_on_everything(self):
        base = jury_key(b"src", "cpp", ["g++", "-O2"], "t")
        self.assertNotEqual(base, jury_key(b"src2", "cpp", ["g++", "-O2"], "t"))
        self.assertNotEqual(base, jury_key(b"src", "c", ["g++", "-O2"], "t"))
        self.assertNotEqual(base, jury_key(b"src", "cpp", ["g++", "-O3"], "t"))
        self.assertNotEqual(base, jury_key(b"src", "cpp", ["g++", "-O2"], "u"))
        m = MemoryJuryCache()
        m.put("k", b"v")
        self.assertEqual(m.get("k"), b"v")


def read_systemd_env(path: Path) -> dict:
    """Parses like systemd's EnvironmentFile: whole rest of the line is the value, no trailing comments."""
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, _, value = line.partition("=")
        env[key] = value.strip().strip("\"'")
    return env


class DeployFilesTests(unittest.TestCase):
    def test_worker_env_example_is_valid_for_systemd(self):
        env = read_systemd_env(ROOT / "deploy/env/worker.env.example")
        cfg = WorkerConfig.from_env(env)
        cfg.validate()  # CHANGE_ME is a non-empty token; every number must parse
        self.assertEqual((cfg.workers, cfg.reserve_cores, cfg.box_base, cfg.backend), (5, 1, 0, "isolate"))

    def test_backend_env_example_is_valid(self):
        from app.settings import Settings
        s = Settings.from_env(read_systemd_env(ROOT / "deploy/env/backend.env.example"))
        self.assertTrue(s.database_url.startswith("postgresql+psycopg://"))
        self.assertEqual(s.queue.max_attempts, 3)

    def test_unit_stop_timeout_exceeds_drain_timeout(self):
        unit = (ROOT / "deploy/systemd/judge-worker.service").read_text()
        stop = int([l for l in unit.splitlines() if l.startswith("TimeoutStopSec=")][0].split("=")[1])
        self.assertGreater(stop, WorkerConfig().drain_timeout_s + 20)
        self.assertNotIn("NoNewPrivileges", "\n".join(l for l in unit.splitlines() if not l.startswith("#")))


if __name__ == "__main__":
    unittest.main()
