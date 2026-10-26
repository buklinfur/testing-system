"""The internal worker API (FastAPI + PostgreSQL) end to end through TestClient.
Needs: fastapi, httpx, sqlalchemy, psycopg; skipped unless JUDGE_TEST_DATABASE_URL is set."""
import tempfile
import unittest

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

try:
    import httpx  # noqa: F401
    import fastapi  # noqa: F401
    HAVE_API = True
except ImportError:
    HAVE_API = False

if URL and HAVE_SA and HAVE_API:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app import queue as q
    from app.blobstore import BlobStore
    from app.internal_main import create_internal_app
    from app.models import Blob, Judgement, JudgementStatus, TestCase
    from app.settings import Settings
    from protocol.manifest import manifest_digest


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


if __name__ == "__main__":
    unittest.main()
