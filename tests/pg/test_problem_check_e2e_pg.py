"""Checks of problem versions end to end (session 4a.4): import -> pending_check + a queued check -> the real internal
API (uvicorn on a free localhost port) -> a real WorkerProcess (local sandbox) -> version status and check report.

The test packages in tests/problems/check-* and the examples in problems-examples/ are imported as they are. Skipped
unless JUDGE_TEST_DATABASE_URL is set. The isolate backend is not used here (sandbox-check / redteam by hand).
"""
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

EXAMPLES = ROOT / "problems-examples"
PACKAGES = ROOT / "tests" / "problems"

try:
    import uvicorn
    HAVE_UVICORN = True
except ImportError:
    HAVE_UVICORN = False

if URL and HAVE_SA:
    from sqlalchemy import select

    from app import problem_checks as checks, queue as q
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.contests import service
    from app.contests.spec import parse
    from app.internal_main import create_internal_app
    from app.models import Judgement, ProblemVersion, Submission, VersionCheck, VersionStatus
    from app.problems.importer import import_package, store_blob
    from app.settings import ImportSettings, QueueSettings, Settings
    from app.timeutil import zone
    from worker.config import WorkerConfig
    from worker.process import WorkerProcess
    from worker.topology import WorkerSlot

# what every package must end as: (status, report reason)
EXPECTED = {
    "check-verdicts": ("ready", None),
    "check-wrong-expect": ("failed", "solution_mismatch"),
    "check-broken-checker": ("failed", "checker_compile_failed"),
    "check-broken-interactor": ("failed", "interactor_compile_failed"),
    "sum-of-two": ("ready", None),
    "pair-with-sum": ("ready", None),
    "guess-number": ("ready", None),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@requires_pg
@unittest.skipUnless(HAVE_UVICORN, "needs uvicorn")
@mock.patch.dict(os.environ, {"JUDGE_ALLOW_UNSAFE_LOCAL": "1"})
class ProblemCheckEndToEndTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blobs = BlobStore(self.tmp / "blobs")
        settings = Settings(blob_dir=str(self.tmp / "blobs"), queue=QueueSettings(check_timeout_s=120))
        app = create_internal_app(settings, session_factory=self.Session)
        port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                                    lifespan="off"))
        thread = threading.Thread(target=self.server.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, self.server, "should_exit", True)
        deadline = time.monotonic() + 10
        while not self.server.started:
            self.assertLess(time.monotonic(), deadline, "the internal API did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{port}"
        self.token = q.create_worker_token(self.s, "e2e", ["cpu", "problem_check"])
        self.actor = audit.Actor(cli="cli:judge")
        self.s.commit()

    def start_worker(self, index=0):
        cfg = WorkerConfig(api_url=self.url, token=self.token, machine="e2e", backend="local", pin_cpus=False,
                           cache_dir=self.tmp / "cache", poll_interval_s=0.05, heartbeat_interval_s=0.2)
        worker = WorkerProcess(cfg, WorkerSlot(index, (), index * 2))
        codes = []
        thread = threading.Thread(target=lambda: codes.append(worker.run()), daemon=True)
        thread.start()

        def stop():
            worker.stop.set()
            thread.join(30)
            self.assertEqual(codes, [0], "the worker stopped cleanly")
        self.addCleanup(stop)
        return worker

    def imported(self, root):
        result = import_package(self.s, self.blobs, ImportSettings(), self.actor, self.now(), directory=root)
        self.s.commit()
        return result

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def wait_checks(self, timeout=240.0):
        deadline = time.monotonic() + timeout
        while True:
            with self.Session() as s:
                open_ = s.execute(select(VersionCheck.id).where(VersionCheck.status != "done")).scalars().all()
            if not open_:
                return
            self.assertLess(time.monotonic(), deadline, f"checks still open: {open_}")
            time.sleep(0.2)

    def version(self, version_id):
        with self.Session() as s:
            return s.get(ProblemVersion, version_id)

    def test_every_package_ends_as_expected_with_a_readable_report(self):
        results = {}
        for name in EXPECTED:
            root = PACKAGES / name if (PACKAGES / name).is_dir() else EXAMPLES / name
            r = self.imported(root)
            self.assertEqual((r.kind, r.status), ("created", "pending_check"), name)
            results[name] = r.version.id
        quiz = self.imported(EXAMPLES / "quiz-python-basics")
        self.assertEqual((quiz.status, quiz.check), ("ready", None), "nothing to check")

        self.start_worker()
        self.wait_checks()
        reports = {}
        for name, (status, reason) in EXPECTED.items():
            v = self.version(results[name])
            with self.subTest(package=name):
                self.assertEqual((v.status.value, v.check_report["reason"]), (status, reason), v.check_report)
                self.assertEqual(v.check_report["worker"], "e2e-w0")
            reports[name] = v.check_report

        verdicts = {s["name"]: (s["expected"], s["got"], s["ok"]) for s in reports["check-verdicts"]["solutions"]}
        self.assertEqual(verdicts, {"solutions/ok.py": ("OK", "OK", True), "solutions/wa.py": ("WA", "WA", True),
                                    "solutions/tl.py": ("TL", "TL", True), "solutions/re.py": ("RE", "RE", True),
                                    "solutions/ce.cpp": ("CE", "CE", True)})
        wrong = {s["name"]: s for s in reports["check-wrong-expect"]["solutions"]}
        self.assertEqual((wrong["solutions/wa.py"]["got"], wrong["solutions/wa.py"]["ok"]), ("WA", False))
        self.assertEqual(wrong["solutions/wa.py"]["first_fail"]["verdict"], "WA")
        self.assertEqual((wrong["solutions/slow.py"]["got"], wrong["solutions/slow.py"]["first_fail"]["verdict"]),
                         ("TL", "TL"))
        self.assertTrue(wrong["solutions/ok.py"]["ok"])
        for name, role in (("check-broken-checker", "checker"), ("check-broken-interactor", "interactor")):
            programs = reports[name]["programs"]
            self.assertEqual([(p["role"], p["ok"]) for p in programs], [(role, False)])
            self.assertIn("error", programs[0]["log"], "the compiler's message is in the report")
            self.assertEqual(reports[name]["solutions"], [], "no solution runs against a broken jury program")
        self.assertEqual([(p["role"], p["ok"]) for p in reports["pair-with-sum"]["programs"]], [("checker", True)])
        self.assertEqual([(p["role"], p["ok"]) for p in reports["guess-number"]["programs"]], [("interactor", True)])

    def test_a_ready_version_is_pinned_and_judged_and_a_recheck_runs_on_the_same_worker(self):
        r = self.imported(EXAMPLES / "sum-of-two")
        spec = "format_version: 1\nslug: c1\ntitle: C\nstarts_at: 2026-01-01 09:00\nproblems:\n  - {label: A, problem: sum-of-two}\n"
        with self.assertRaises(service.ContestError) as refused:
            service.apply(self.s, self.actor, parse(spec, zone("UTC")), self.now())
        self.assertIn("v1 pending_check", str(refused.exception))
        self.s.rollback()

        self.start_worker()
        self.wait_checks()
        self.assertEqual(self.version(r.version.id).status, VersionStatus.ready)
        service.apply(self.s, self.actor, parse(spec, zone("UTC")), self.now())
        self.s.commit()
        cp = service.visible_row(self.s, service.get_contest(self.s, "c1"), "A")
        source = store_blob(self.s, self.blobs, (EXAMPLES / "sum-of-two/solutions/ok.py").read_bytes())
        sub = Submission(user_id=self.student(1).id, contest_problem_id=cp.id, language_key="python",
                         source_sha256=source)
        self.s.add(sub)
        self.s.flush()
        j = q.enqueue(self.s, sub)
        self.s.commit()

        again = checks.recheck(self.s, self.actor, self.s.get(ProblemVersion, r.version.id), self.now())
        self.s.commit()
        self.assertEqual(again.kind, "created")
        deadline = time.monotonic() + 120
        while True:
            with self.Session() as s:
                done = s.get(VersionCheck, again.check.id).status.value == "done"
                judged = s.execute(select(Judgement.status).where(Judgement.id == j.id)).scalar_one().value
            if done and judged == "finished":
                break
            self.assertLess(time.monotonic(), deadline, (done, judged))
            time.sleep(0.2)
        with self.Session() as s:
            self.assertEqual(s.get(Judgement, j.id).verdict.value, "OK")
            first_started = s.get(Judgement, j.id).started_at
            check_started = s.get(VersionCheck, again.check.id).started_at
            self.assertLessEqual(first_started, check_started, "the waiting submission went first (D-049)")
            self.assertEqual(s.get(ProblemVersion, r.version.id).status, VersionStatus.ready)
            self.assertEqual(len(checks.history(s, r.version.id)), 2)


if __name__ == "__main__":
    unittest.main()
