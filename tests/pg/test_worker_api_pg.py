"""The internal worker API (FastAPI + PostgreSQL) end to end through TestClient.
Needs: fastapi, httpx, sqlalchemy, psycopg; skipped unless JUDGE_TEST_DATABASE_URL is set."""
import tempfile
import unittest
import warnings
from unittest import mock

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

try:
    import httpx  # noqa: F401
    import fastapi  # noqa: F401
    HAVE_API = True
except ImportError:
    HAVE_API = False

if URL and HAVE_SA and HAVE_API:
    from fastapi.testclient import TestClient
    from sqlalchemy import select, text

    from app import problem_checks as checks, queue as q
    from app.blobstore import BlobStore
    from app.internal_main import create_internal_app
    from app.models import (Blob, CheckReason, CheckStatus, Judgement, JudgementStatus, ProblemVersion, Program,
                            ProgramRole, TestCase, TestResult, Verdict, VersionCheck, VersionStatus)
    from app.settings import QueueSettings, Settings
    from protocol.manifest import manifest_digest
    from worker.client import ApiClient

BASE = "/internal/worker/v1"
# the fields of a judgement job before 4a.4; `kind` and `release` are the additions (a worker of 4a.3 ignores them)
JUDGEMENT_JOB_FIELDS_4A3 = {"judgement_id", "attempt", "reason", "submission_id", "problem_version_id",
                            "manifest_sha256", "language", "source_sha256", "is_archive", "entry_file", "answers",
                            "deadline_s"}


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class WorkerApiTests(PgCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blobs = BlobStore(self.tmp.name)
        self.in_sha, self.out_sha, self.src_sha = (self.blobs.put(b"1 2\n"), self.blobs.put(b"3\n"), self.blobs.put(b"print(3)\n"))
        for sha in (self.in_sha, self.out_sha, self.src_sha):
            self.s.merge(Blob(sha256=sha, size_bytes=1))
        self.s.flush()
        for t in self.seed["tests"]:
            self.s.get(TestCase, t.id).input_sha256, self.s.get(TestCase, t.id).answer_sha256 = self.in_sha, self.out_sha
        self.sub, self.j = self.submission(self.student(1))
        self.sub.source_sha256 = self.src_sha
        self.token = q.create_worker_token(self.s, "main", ["cpu"])
        self.s.commit()
        app = create_internal_app(Settings(blob_dir=self.tmp.name), session_factory=self.Session)
        self.client = TestClient(app)
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def headers(self, reg):
        return {**self.auth, "X-Worker-Id": str(reg["worker_id"]), "X-Worker-Instance": reg["instance"]}

    def register(self, name="w0"):
        r = self.client.post("/internal/worker/v1/register", headers=self.auth, json={"name": name, "capabilities": ["cpu"], "info": {}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_full_cycle(self):
        base = "/internal/worker/v1"
        self.assertEqual(self.client.post(f"{base}/claim").status_code, 401)
        self.assertEqual(self.client.post(f"{base}/claim", headers={"Authorization": "Bearer nope"}).status_code, 401)
        reg = self.register()
        h = self.headers(reg)
        job = self.client.post(f"{base}/claim", headers=h).json()["job"]
        self.assertEqual((job["judgement_id"], job["attempt"], job["language"]), (self.j.id, 1, "python"))
        m = self.client.get(f"{base}/problem-versions/{job['problem_version_id']}/manifest", headers=h).json()
        self.assertEqual(manifest_digest(m["manifest"]), m["manifest_sha256"])
        self.assertEqual(m["manifest_sha256"], job["manifest_sha256"], "claim and manifest agree on the digest")
        self.assertEqual(len(m["manifest"]["groups"][0]["tests"]), 2)
        blob = self.client.get(f"{base}/blobs/{self.in_sha}", headers=h)
        self.assertEqual((blob.status_code, blob.content), (200, b"1 2\n"))
        self.assertEqual(self.client.get(f"{base}/blobs/{'0' * 64}", headers=h).status_code, 404)
        hb = self.client.post(f"{base}/heartbeat", headers=h, json={"state": "busy", "judgement_id": job["judgement_id"], "attempt": 1,
                                                                    "phase": "running", "tests_done": 1, "tests_total": 2, "stats": {"jobs_done": 0}})
        self.assertEqual(hb.json(), {"cancel": False})
        tests = [{"test_case_id": t["id"], "verdict": "OK", "time_ms": 3, "visible_to_student": True}
                 for t in m["manifest"]["groups"][0]["tests"]]
        body = {"attempt": 1, "verdict": "OK", "score": "100", "max_score": "100", "tests": tests, "summary": {"groups": []}}
        self.assertEqual(self.client.post(f"{base}/judgements/{job['judgement_id']}/result", headers=h, json=body).json(), {"status": "recorded"})
        self.assertEqual(self.client.post(f"{base}/judgements/{job['judgement_id']}/result", headers=h, json=body).json(), {"status": "already_recorded"})
        with self.Session() as s:
            self.assertEqual(s.get(Judgement, self.j.id).status, JudgementStatus.finished)
        self.assertEqual(self.client.post(f"{base}/claim", headers=h).json(), {"job": None})

    def test_tl_kind_is_stored_for_tl_only_and_validated(self):
        base = "/internal/worker/v1"
        h = self.headers(self.register())
        job = self.client.post(f"{base}/claim", headers=h).json()["job"]
        ids = [t.id for t in self.seed["tests"]]
        url = f"{base}/judgements/{job['judgement_id']}/result"
        bad = {"attempt": 1, "verdict": "TL", "tests": [{"test_case_id": ids[0], "verdict": "TL", "tl_kind": "moon"}]}
        self.assertEqual(self.client.post(url, headers=h, json=bad).status_code, 422)
        body = {"attempt": 1, "verdict": "TL", "score": "0", "max_score": "100", "summary": {"groups": []},
                "tests": [{"test_case_id": ids[0], "verdict": "TL", "tl_kind": "wall"},
                          {"test_case_id": ids[1], "verdict": "OK", "tl_kind": "cpu"}]}  # a kind on OK is ignored
        self.assertEqual(self.client.post(url, headers=h, json=body).json(), {"status": "recorded"})
        with self.Session() as s:
            kinds = dict(s.execute(select(TestResult.test_case_id, TestResult.tl_kind)
                                   .where(TestResult.judgement_id == job["judgement_id"])).all())
        self.assertEqual(kinds, {ids[0]: "wall", ids[1]: None})

    def test_stale_attempt_gets_409(self):
        base = "/internal/worker/v1"
        h = self.headers(self.register())
        job = self.client.post(f"{base}/claim", headers=h).json()["job"]
        r = self.client.post(f"{base}/judgements/{job['judgement_id']}/result", headers=h,
                             json={"attempt": 7, "verdict": "OK", "score": "1", "tests": []})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"))

    def test_second_instance_supersedes_the_first(self):
        first = self.register("same")
        self.register("same")
        r = self.client.post("/internal/worker/v1/claim", headers=self.headers(first))
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "superseded"))

    def test_revoked_token_and_foreign_names(self):
        reg = self.register()
        from sqlalchemy import text
        with self.Session() as s:
            s.execute(text("UPDATE worker_tokens SET revoked_at = now()"))
            s.commit()
        self.assertEqual(self.client.post("/internal/worker/v1/claim", headers=self.headers(reg)).status_code, 401)

    def test_status_reads_one_moment_of_the_database(self):
        seen = []
        real = q.read_one_moment

        def spy(session):
            moment = real(session)
            seen.append(session.execute(text("SHOW transaction_isolation")).scalar_one())
            return moment
        with mock.patch.object(q, "read_one_moment", spy):
            self.assertEqual(self.client.get("/internal/status").status_code, 200)
            self.assertEqual(self.client.get("/internal/metrics").status_code, 200)
        self.assertEqual(seen, ["repeatable read", "repeatable read"])

    def test_proxied_traffic_is_refused_and_metrics_work(self):
        for header in ("cf-ray", "X-Forwarded-For", "Forwarded"):
            r = self.client.get("/internal/health", headers={header: "1.2.3.4"})
            self.assertEqual(r.status_code, 403, header)
        r = self.client.post("/internal/worker/v1/register", headers={**self.auth, "X-Forwarded-For": "1.2.3.4"},
                             json={"name": "w0"})
        self.assertEqual(r.status_code, 403, "refused before authentication, even with a valid token")
        self.assertEqual(self.client.get("/internal/health").json(), {"ok": True})
        text_out = self.client.get("/internal/metrics").text
        self.assertIn("judge_queue_length 1", text_out)
        status = self.client.get("/internal/status").json()
        self.assertIn("workers", status)



@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class CheckProtocolTests(PgCase):
    """Checks of problem versions through the worker API (session 4a.4, docs/WORKER_PROTOCOL.md)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blobs = BlobStore(self.tmp.name)
        self.sol_sha = self.blobs.put(b"print(3)\n")
        self.s.merge(Blob(sha256=self.sol_sha, size_bytes=9))
        self.version = self.s.get(ProblemVersion, self.seed["version"].id)
        self.version.status = VersionStatus.pending_check
        for name, expect in (("solutions/ok.py", Verdict.OK), ("solutions/wa.py", Verdict.WA)):
            self.s.add(Program(version_id=self.version.id, role=ProgramRole.solution, name=name, language_key="python",
                               source_sha256=self.sol_sha, expected_verdict=expect))
        self.s.flush()
        self.check = checks.enqueue_check(self.s, self.version, CheckReason.import_)
        self.token = q.create_worker_token(self.s, "main", ["cpu", "problem_check"])
        self.s.commit()
        self.settings = Settings(blob_dir=self.tmp.name, queue=QueueSettings(check_timeout_s=321.0))
        self.client = TestClient(create_internal_app(self.settings, session_factory=self.Session))
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def register(self, name, caps=("cpu", "problem_check")):
        r = self.client.post(f"{BASE}/register", headers=self.auth,
                             json={"name": name, "capabilities": list(caps), "info": {}})
        self.assertEqual(r.status_code, 200, r.text)
        reg = r.json()
        return {**self.auth, "X-Worker-Id": str(reg["worker_id"]), "X-Worker-Instance": reg["instance"]}

    def claim(self, h):
        r = self.client.post(f"{BASE}/claim", headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["job"]

    def report(self, outcome="ready", **extra):
        return {"outcome": outcome, "reason": None if outcome == "ready" else "solution_mismatch", "programs": [],
                "solutions": [{"name": "solutions/ok.py", "expected": "OK", "got": "OK", "ok": True}], **extra}

    def result(self, h, check_id, body):
        return self.client.post(f"{BASE}/problem-checks/{check_id}/result", headers=h, json=body)

    def stored(self):
        with self.Session() as s:
            c, v = s.get(VersionCheck, self.check.id), s.get(ProblemVersion, self.version.id)
            return c.status, c.attempt, c.failures, v.status

    # ------------------------------------------------------------- compatibility
    def test_a_judgement_job_has_the_old_fields_and_kind(self):
        self.submission(self.student(1))
        self.s.commit()
        job = self.claim(self.register("w0"))
        self.assertEqual(set(job), JUDGEMENT_JOB_FIELDS_4A3 | {"kind", "release"})
        self.assertEqual((job["kind"], job["release"]), ("judgement", f"/judgements/{job['judgement_id']}/release"))

    def test_the_worker_client_of_4a3_reads_the_new_job(self):
        """worker/client.py (unchanged since 4a.3) returns the job dict as it came: an extra field is just a key that
        WorkerProcess.run_job never reads (it reads the old fields by name)."""
        sub, j = self.submission(self.student(1))
        self.s.commit()
        api = ApiClient("http://testserver", self.token)
        api.session = self.client  # the real client code over the app, without a network
        api.session.headers["Authorization"] = f"Bearer {self.token}"
        with warnings.catch_warnings():  # TestClient frowns on the client's timeout= argument
            warnings.simplefilter("ignore")
            api.register("old-worker", ["cpu"], {})
            job = api.claim()
            self.assertEqual((job["judgement_id"], job["attempt"], job["language"], job["kind"]),
                             (j.id, 1, "python", "judgement"))
            api.heartbeat("busy", job["judgement_id"], job["attempt"], "running", 0, 2, {})
            api.release(job["judgement_id"], job["attempt"], "worker_stopping")
            self.assertEqual(api.claim()["attempt"], 2)
            self.assertIsNone(api.claim(), "no capability problem_check: the queued check is not offered")

    # ------------------------------------------------------------- capability
    def test_a_worker_without_problem_check_never_gets_a_check(self):
        old = self.register("old", caps=("cpu",))
        for _ in range(3):
            self.assertIsNone(self.claim(old), "a queued check is not offered to a worker without the capability")
        sub, j = self.submission(self.student(1))
        self.s.commit()
        job = self.claim(old)
        self.assertEqual((job["kind"], job["judgement_id"]), ("judgement", j.id), "submissions still go to it")
        self.assertIsNone(self.claim(old))
        self.assertEqual(self.stored()[0], CheckStatus.queued)
        new = self.register("new")
        self.assertEqual(self.claim(new)["kind"], "problem_check")

    def test_a_token_without_problem_check_cannot_announce_it(self):
        token = q.create_worker_token(self.s, "old-machine", ["cpu"])
        self.s.commit()
        r = self.client.post(f"{BASE}/register", headers={"Authorization": f"Bearer {token}"},
                             json={"name": "w9", "capabilities": ["cpu", "problem_check"], "info": {}})
        self.assertEqual(r.status_code, 403)

    # ------------------------------------------------------------- the cycle
    def test_full_check_cycle(self):
        h = self.register("w0")
        job = self.claim(h)
        self.assertEqual((job["kind"], job["release"]), ("problem_check", f"/problem-checks/{self.check.id}/release"))
        self.assertEqual((job["check_id"], job["attempt"], job["reason"], job["problem_version_id"]),
                         (self.check.id, 1, "import", self.version.id))
        self.assertEqual(job["deadline_s"], 321.0, "JUDGE_CHECK_TIMEOUT_SEC")
        self.assertEqual(job["solutions"], [
            {"name": "solutions/ok.py", "language": "python", "source_sha256": self.sol_sha, "expect": "OK"},
            {"name": "solutions/wa.py", "language": "python", "source_sha256": self.sol_sha, "expect": "WA"}])
        m = self.client.get(f"{BASE}/problem-versions/{job['problem_version_id']}/manifest", headers=h).json()
        self.assertEqual(m["manifest_sha256"], job["manifest_sha256"])
        self.assertNotIn("solutions", m["manifest"]["config"], "the manifest (the cache key) does not change")
        self.assertEqual(self.client.get(f"{BASE}/blobs/{self.sol_sha}", headers=h).content, b"print(3)\n")

        hb = self.client.post(f"{BASE}/heartbeat", headers=h, json={
            "state": "busy", "check_id": job["check_id"], "attempt": 1, "tests_done": 1, "tests_total": 2, "stats": {}})
        self.assertEqual(hb.json(), {"cancel": False})
        with self.Session() as s:
            c = s.get(VersionCheck, self.check.id)
            self.assertEqual((c.progress_done, c.progress_total), (1, 2))

        body = {"attempt": 1, "outcome": "ready", "report": self.report()}
        self.assertEqual(self.result(h, job["check_id"], body).json(), {"status": "recorded"})
        self.assertEqual(self.result(h, job["check_id"], body).json(), {"status": "already_recorded"},
                         "a repeated result (network retry) is safe")
        self.assertEqual(self.stored(), (CheckStatus.done, 1, 0, VersionStatus.ready))
        with self.Session() as s:
            self.assertEqual(s.get(ProblemVersion, self.version.id).check_report, self.report())
        self.assertIsNone(self.claim(h))

    def test_a_bad_report_gets_422_and_changes_nothing(self):
        h = self.register("w0")
        job = self.claim(h)
        for bad in ({"attempt": 1},                                                      # neither outcome nor error
                    {"attempt": 1, "outcome": "maybe", "report": self.report()},
                    {"attempt": 1, "outcome": "ready", "report": self.report("failed")},  # outcomes differ
                    {"attempt": 1, "outcome": "failed", "report": self.report("failed", reason="whim")},
                    {"attempt": 1, "outcome": "ready", "report": self.report(by="server")},
                    {"attempt": 1, "outcome": "ready", "report": "text"},                # not an object (pydantic)
                    {"outcome": "ready", "report": self.report()}):                       # no attempt (pydantic)
            with self.subTest(bad=bad):
                self.assertEqual(self.result(h, job["check_id"], bad).status_code, 422)
        self.assertEqual(self.stored(), (CheckStatus.running, 1, 0, VersionStatus.pending_check))
        self.assertEqual(self.result(h, job["check_id"], {"attempt": 1, "outcome": "ready",
                                                          "report": self.report()}).json(), {"status": "recorded"})

    def test_fencing_gives_409_lease_lost(self):
        h0 = self.register("w0")
        job = self.claim(h0)
        r = self.result(h0, job["check_id"], {"attempt": 7, "outcome": "ready", "report": self.report()})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"), "a wrong attempt")
        h1 = self.register("w1")
        r = self.result(h1, job["check_id"], {"attempt": 1, "outcome": "ready", "report": self.report()})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"), "another worker")
        r = self.result(h0, 99999, {"attempt": 1, "outcome": "ready", "report": self.report()})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"), "no such check")

        # released and taken by another worker: the first one's heartbeat cancels, its result and release fail
        rel = self.client.post(f"{BASE}/problem-checks/{job['check_id']}/release", headers=h0,
                               json={"attempt": 1, "reason": "worker_stopping"})
        self.assertEqual(rel.json(), {"released": True})
        self.assertEqual(self.stored(), (CheckStatus.queued, 1, 0, VersionStatus.pending_check), "not a failure")
        job2 = self.claim(h1)
        self.assertEqual((job2["check_id"], job2["attempt"]), (job["check_id"], 2))
        hb = self.client.post(f"{BASE}/heartbeat", headers=h0, json={"state": "busy", "check_id": job["check_id"],
                                                                     "attempt": 1, "stats": {}})
        self.assertEqual(hb.json(), {"cancel": True})
        again = self.client.post(f"{BASE}/problem-checks/{job['check_id']}/release", headers=h0,
                                 json={"attempt": 1, "reason": "worker_stopping"})
        self.assertEqual(again.json(), {"released": False})
        r = self.result(h0, job["check_id"], {"attempt": 1, "outcome": "ready", "report": self.report()})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"))
        self.assertEqual(self.result(h1, job["check_id"], {"attempt": 2, "outcome": "failed",
                                                           "report": self.report("failed")}).json(),
                         {"status": "recorded"})
        self.assertEqual(self.stored(), (CheckStatus.done, 2, 0, VersionStatus.failed))

    def test_a_result_after_the_server_ended_the_check_is_lease_lost(self):
        h = self.register("w0")
        job = self.claim(h)
        with self.Session() as s:
            s.execute(text("UPDATE version_checks SET started_at = now() - interval '1 hour'"))
            s.commit()
            self.assertEqual(checks.reap(s, self.settings.queue)["checks_timed_out"], 1)
            s.commit()
        r = self.result(h, job["check_id"], {"attempt": 1, "outcome": "ready", "report": self.report()})
        self.assertEqual((r.status_code, r.json()["detail"]["code"]), (409, "lease_lost"))
        self.assertEqual(self.stored()[3], VersionStatus.failed)

    def test_an_error_of_the_worker_requeues_the_check(self):
        h = self.register("w0")
        job = self.claim(h)
        r = self.result(h, job["check_id"], {"attempt": 1, "error": "disk full"})
        self.assertEqual(r.json(), {"status": "requeued"})
        self.assertEqual(self.stored(), (CheckStatus.queued, 1, 1, VersionStatus.pending_check))

    def test_check_routes_are_refused_through_a_proxy(self):
        h = self.register("w0")
        job = self.claim(h)
        for header in ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host"):
            for path in (f"/problem-checks/{job['check_id']}/result", f"/problem-checks/{job['check_id']}/release",
                         "/claim"):
                with self.subTest(header=header, path=path):
                    r = self.client.post(BASE + path, headers={**h, header: "203.0.113.7"},
                                         json={"attempt": 1, "outcome": "ready", "report": self.report()})
                    self.assertEqual(r.status_code, 403)
        self.assertEqual(self.stored(), (CheckStatus.running, 1, 0, VersionStatus.pending_check), "nothing reached it")


if __name__ == "__main__":
    unittest.main()
