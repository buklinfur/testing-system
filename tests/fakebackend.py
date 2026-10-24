"""In-memory stand-in for the backend's internal API (stdlib http.server), implementing the same protocol
and the same semantics as backend/app/queue.py (fencing by attempt, fair claim order, release, requeue on
register). Lets the whole worker be tested without PostgreSQL / FastAPI.

It is a model of the contract, not a test of the real backend: the real one is covered by tests/pg.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from backend.app.loadinfo import fair_key  # type: ignore  # noqa: E402  (pure module)
from protocol.manifest import assemble_manifest, manifest_digest

TOKEN = "jw_test_token"


def import_package(root: Path, blobs: dict, version_id: int, first_test_id: int = 1) -> dict:
    """Package directory -> manifest (what module 4's importer will do against the database)."""
    data = yaml.safe_load((root / "problem.yaml").read_text(encoding="utf-8"))

    def blob(path: Path):
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        blobs[sha] = raw
        return sha

    groups, tid = [], first_test_id
    for gpos, raw in enumerate(data.get("test_groups", [])):
        patterns = raw["tests"] if isinstance(raw["tests"], list) else [raw["tests"]]
        files = sorted({p for pat in patterns for p in root.glob(pat)})
        tests = []
        for tpos, f in enumerate(files):
            ans = f.with_suffix(".out")
            tests.append({"id": tid, "position": tpos, "name": f.stem, "input_sha256": blob(f),
                          "answer_sha256": blob(ans) if ans.exists() else None})
            tid += 1
        groups.append({"id": gpos + 1, "position": gpos, "name": raw["name"], "points": raw.get("points", 0),
                       "scoring": raw.get("scoring", "sum"), "visibility": raw.get("visibility", "hidden"),
                       "feedback": raw.get("feedback"), "show_in_statement": raw.get("show_in_statement", False),
                       "depends_on": raw.get("depends_on", []), "tests": tests})
    programs = []
    if (data.get("checker") or {}).get("type") == "custom":
        p = root / data["checker"]["path"]
        programs.append({"role": "checker", "name": p.name, "language": "cpp", "source_sha256": blob(p)})
    if data.get("interactor"):
        p = root / data["interactor"]["path"]
        programs.append({"role": "interactor", "name": p.name, "language": data["interactor"].get("language", "cpp"),
                         "source_sha256": blob(p)})
    config = {k: v for k, v in data.items() if k not in ("test_groups", "title", "slug", "type", "solutions", "tags")}
    return assemble_manifest({"id": version_id, "slug": data["slug"], "type": data["type"], "titles": data["title"],
                              "config": config}, groups, programs)


class FakeBackend:
    def __init__(self, max_attempts: int = 3) -> None:
        self.lock = threading.RLock()
        self.blobs: dict = {}
        self.versions: dict = {}  # id -> manifest
        self.judgements: dict = {}
        self.workers: dict = {}
        self.events: list = []
        self.results: dict = {}
        self.max_attempts = max_attempts
        self.next_id = 1
        self.blob_downloads = 0
        self.manifest_downloads = 0
        self.server = None
        self.thread = None

    # --------------------------------------------------------------------- test API
    def add_problem(self, root: Path, version_id: int) -> dict:
        m = import_package(root, self.blobs, version_id, first_test_id=version_id * 1000)
        self.versions[version_id] = m
        return m

    def submit(self, *, user: int, version_id: int, source: bytes = b"", language: str = "python", priority: int = 0,
               answers=None, is_archive=False, entry=None) -> int:
        with self.lock:
            sha = hashlib.sha256(source).hexdigest()
            self.blobs[sha] = source
            jid = self.next_id
            self.next_id += 1
            self.judgements[jid] = dict(id=jid, user_id=user, version_id=version_id, language=language,
                                        source_sha256=sha, status="queued", worker=None, attempt=0, failures=0,
                                        priority=priority, created_at=time.monotonic(), started_at=None,
                                        answers=answers, is_archive=is_archive, entry=entry, verdict=None,
                                        done=None, total=None, error=None)
            return jid

    def expire_worker(self, name: str) -> None:
        """What the reaper does for a worker without heartbeat."""
        with self.lock:
            wid = self.workers[name]["id"]
            for j in self.judgements.values():
                if j["worker"] == wid and j["status"] in ("compiling", "running"):
                    self._fail(j, "worker lost")

    def _fail(self, j: dict, reason: str) -> None:
        j["failures"] += 1
        if j["failures"] >= self.max_attempts:
            j.update(status="internal_error", verdict="IE", error=reason)
        else:
            j.update(status="queued", worker=None, started_at=None)
        self.events.append((j["id"], reason))

    # --------------------------------------------------------------------- protocol
    def handle(self, method: str, path: str, headers, body: bytes):
        if headers.get("Authorization") != f"Bearer {TOKEN}":
            return 401, {"detail": "invalid token"}
        path = path.split("?")[0].replace("/internal/worker/v1", "")
        data = json.loads(body) if body else {}
        with self.lock:
            if path == "/register":
                name = data["name"]
                w = self.workers.get(name) or {"id": len(self.workers) + 1, "name": name}
                for j in self.judgements.values():
                    if j["worker"] == w["id"] and j["status"] in ("compiling", "running"):
                        self._fail(j, "worker restarted")
                w.update(instance=str(uuid.uuid4()), capabilities=data["capabilities"], info=data["info"], state="idle")
                self.workers[name] = w
                return 200, {"worker_id": w["id"], "instance": w["instance"]}
            worker = next((w for w in self.workers.values() if str(w["id"]) == headers.get("X-Worker-Id")), None)
            if worker is None:
                return 403, {"detail": "unknown worker"}
            if worker["instance"] != headers.get("X-Worker-Instance"):
                return 409, {"detail": {"code": "superseded"}}
            if path == "/deregister":
                worker["state"] = "stopped"
                return 200, {"ok": True}
            if path == "/claim":
                return 200, {"job": self._claim(worker)}
            if path == "/heartbeat":
                worker["state"], worker["stats"] = data["state"], data["stats"]
                cancel = False
                if data["judgement_id"] is not None:
                    j = self.judgements[data["judgement_id"]]
                    if (j["worker"], j["attempt"]) != (worker["id"], data["attempt"]) or j["status"] not in ("compiling", "running"):
                        cancel = True
                    else:
                        j.update(done=data["tests_done"], total=data["tests_total"])
                return 200, {"cancel": cancel}
            m = re.fullmatch(r"/problem-versions/(\d+)/manifest", path)
            if m:
                self.manifest_downloads += 1
                manifest = self.versions[int(m.group(1))]
                return 200, {"manifest_sha256": manifest_digest(manifest), "manifest": manifest}
            m = re.fullmatch(r"/blobs/([0-9a-f]{64})", path)
            if m:
                if m.group(1) not in self.blobs:
                    return 404, {"detail": "no such blob"}
                self.blob_downloads += 1
                return 200, self.blobs[m.group(1)]
            m = re.fullmatch(r"/judgements/(\d+)/release", path)
            if m:
                j = self.judgements[int(m.group(1))]
                ok = (j["worker"], j["attempt"]) == (worker["id"], data["attempt"]) and j["status"] in ("compiling", "running")
                if ok:
                    j.update(status="queued", worker=None, started_at=None)
                return 200, {"released": ok}
            m = re.fullmatch(r"/judgements/(\d+)/result", path)
            if m:
                j = self.judgements[int(m.group(1))]
                if (j["worker"], j["attempt"]) != (worker["id"], data["attempt"]):
                    return 409, {"detail": {"code": "lease_lost"}}
                if j["status"] in ("finished", "internal_error"):
                    return 200, {"status": "already_recorded"}
                if data["verdict"] == "IE":
                    j["error"] = data.get("error")
                    if j["failures"] + 1 < self.max_attempts:
                        self._fail(j, "internal error")
                        return 200, {"status": "requeued"}
                    j["failures"] += 1
                    j.update(status="internal_error", verdict="IE")
                    return 200, {"status": "recorded"}
                j.update(status="finished", verdict=data["verdict"])
                self.results[j["id"]] = data
                return 200, {"status": "recorded"}
        return 404, {"detail": "no route " + path}

    def _claim(self, worker: dict):
        queued = [j for j in self.judgements.values() if j["status"] == "queued"]
        if not queued:
            return None
        inflight: dict = {}
        for j in self.judgements.values():
            if j["status"] in ("compiling", "running"):
                inflight[j["user_id"]] = inflight.get(j["user_id"], 0) + 1
        recent: dict = {}
        for j in self.judgements.values():
            if j["started_at"] is not None and time.monotonic() - j["started_at"] < 300:
                recent[j["user_id"]] = recent.get(j["user_id"], 0) + 1
        best = min(queued, key=lambda j: fair_key(dict(j, user_id=j["user_id"]), inflight, recent))
        best.update(status="compiling", worker=worker["id"], attempt=best["attempt"] + 1, started_at=time.monotonic())
        manifest = self.versions[best["version_id"]]
        return {"judgement_id": best["id"], "attempt": best["attempt"], "reason": "initial", "submission_id": best["id"],
                "problem_version_id": best["version_id"], "manifest_sha256": manifest_digest(manifest),
                "language": best["language"], "source_sha256": best["source_sha256"], "is_archive": best["is_archive"],
                "entry_file": best["entry"], "answers": best["answers"], "deadline_s": 600}

    # --------------------------------------------------------------------- http plumbing
    def start(self) -> str:
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def _do(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                status, payload = backend.handle(method, self.path, self.headers, body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream" if isinstance(payload, bytes) else "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
