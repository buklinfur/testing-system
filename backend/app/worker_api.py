"""Internal API for workers: /internal/worker/v1/*  (contract: docs/WORKER_PROTOCOL.md).

Served by app.internal_main on a separate localhost-only port that is NOT published through the reverse proxy.
Every handler commits explicitly; the session dependency only guarantees rollback and close.

Covered by tests/pg/test_worker_api_pg.py (FastAPI TestClient on a real PostgreSQL).
"""
from __future__ import annotations

from typing import Callable, Iterator, Literal, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import problem_checks as checks
from . import queue as q
from .blobstore import BlobStore, valid_sha256
from .manifest_db import build_manifest, manifest_digest_for_claim
from .models import Judgement, Program, ProgramRole, VersionCheck, Worker, WorkerToken

router = APIRouter(prefix="/internal/worker/v1")


# ----------------------------------------------------------------------------- wiring
def get_settings(request: Request):
    return request.app.state.settings


def get_blobs(request: Request) -> BlobStore:
    return request.app.state.blobs


def get_session(request: Request) -> Iterator[Session]:
    session = request.app.state.session_factory()
    try:
        yield session
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def _bearer(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization[7:].strip()


def token_auth(authorization: Optional[str] = Header(None), session: Session = Depends(get_session)) -> WorkerToken:
    token = q.authenticate(session, _bearer(authorization))
    if token is None:
        raise HTTPException(401, "invalid or revoked token")
    return token


def current_worker(token: WorkerToken = Depends(token_auth), session: Session = Depends(get_session),
                   x_worker_id: Optional[int] = Header(None), x_worker_instance: Optional[str] = Header(None)) -> Worker:
    if x_worker_id is None:
        raise HTTPException(400, "X-Worker-Id header is required (register first)")
    worker = session.get(Worker, x_worker_id)
    if worker is None or worker.token_id != token.id:
        raise HTTPException(403, "unknown worker")
    if worker.instance_id is None:  # before superseded: this instance said goodbye itself (D-068)
        raise HTTPException(409, {"code": "deregistered",
                                  "message": "this worker deregistered: register again to start a new instance"})
    if worker.instance_id != x_worker_instance:
        raise HTTPException(409, {"code": "superseded", "message": "another process registered under this worker name"})
    worker.last_heartbeat = func.now()  # any authenticated call proves the worker is alive
    return worker


# ----------------------------------------------------------------------------- bodies
class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    capabilities: list[str] = ["cpu"]
    info: dict = {}


class HeartbeatBody(BaseModel):
    state: str = "idle"
    judgement_id: Optional[int] = None
    check_id: Optional[int] = None  # a check of a problem version (then tests_done/total count solutions)
    attempt: Optional[int] = None
    phase: Optional[str] = None
    tests_done: Optional[int] = None
    tests_total: Optional[int] = None
    stats: dict = {}


class ReleaseBody(BaseModel):
    attempt: int
    reason: str = ""


class TestOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    test_case_id: int
    verdict: str
    time_ms: Optional[int] = None
    memory_kb: Optional[int] = None
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    tl_kind: Optional[Literal["cpu", "wall"]] = None
    message: Optional[str] = None
    stderr_excerpt: Optional[str] = None
    output_excerpt: Optional[str] = None
    visible_to_student: bool = False


class ResultBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    attempt: int
    verdict: str
    score: str = "0"
    max_score: str = "0"
    compile_log: Optional[str] = None
    error: Optional[str] = None
    summary: Optional[dict] = None
    tests: list[TestOut] = []
    timing: Optional[dict] = None


class CheckResultBody(BaseModel):
    """The end of a check: an outcome with its report, or (the worker itself failed, not the problem) an error."""
    model_config = ConfigDict(extra="ignore")
    attempt: int
    outcome: Optional[str] = None
    report: Optional[dict] = None
    error: Optional[str] = None


# ----------------------------------------------------------------------------- jobs
def judgement_job(session: Session, j: Judgement, settings) -> dict:
    sub = j.submission
    return {
        "kind": "judgement", "release": f"/judgements/{j.id}/release",
        "judgement_id": j.id, "attempt": j.attempt, "reason": j.reason.value, "submission_id": sub.id,
        "problem_version_id": j.problem_version_id,
        "manifest_sha256": manifest_digest_for_claim(session, j.problem_version_id),
        "language": sub.language_key, "source_sha256": sub.source_sha256, "is_archive": sub.is_archive,
        "entry_file": sub.entry_file, "answers": sub.answers, "deadline_s": settings.queue.max_judging_s,
    }


def check_job(session: Session, check: VersionCheck, settings) -> dict:
    """A check of a problem version (docs/WORKER_PROTOCOL.md). The manifest has no solutions (its digest is the cache
    key of every worker), so the reference solutions travel in the job and are downloaded as blobs."""
    version = check.version
    solutions = session.execute(select(Program).where(
        Program.version_id == version.id, Program.role == ProgramRole.solution).order_by(Program.id)).scalars()
    return {
        "kind": "problem_check", "release": f"/problem-checks/{check.id}/release",
        "check_id": check.id, "attempt": check.attempt, "reason": check.reason.value,
        "problem": f"{version.problem.slug}@{version.version}",
        "problem_version_id": version.id,
        "manifest_sha256": manifest_digest_for_claim(session, version.id),
        "solutions": [{"name": p.name, "language": p.language_key, "source_sha256": p.source_sha256,
                       "expect": p.expected_verdict.value if p.expected_verdict else None} for p in solutions],
        "deadline_s": settings.queue.check_timeout_s,
    }


# ----------------------------------------------------------------------------- endpoints
@router.post("/register")
def register(body: RegisterBody, token: WorkerToken = Depends(token_auth), session: Session = Depends(get_session),
             settings=Depends(get_settings)):
    try:
        worker = q.register_worker(session, token, body.name, body.capabilities, body.info, settings.queue)
    except q.TokenError as exc:
        raise HTTPException(403, str(exc))
    session.commit()
    return {"worker_id": worker.id, "instance": worker.instance_id,
            "heartbeat_interval_s": 5, "heartbeat_timeout_s": settings.queue.heartbeat_timeout_s}


@router.post("/deregister")
def deregister(worker: Worker = Depends(current_worker), session: Session = Depends(get_session)):
    q.deregister_worker(session, worker)
    session.commit()
    return {"ok": True}


@router.post("/claim")
def claim(worker: Worker = Depends(current_worker), session: Session = Depends(get_session),
          settings=Depends(get_settings)):
    """The next job: a judgement (always first) or, when this worker has no submission to take, a check of a
    problem version (only with the capability problem_check, within the quota; docs/QUEUE.md)."""
    got = q.claim_next(session, worker, settings.queue)
    if got is None:
        worker.state = "idle"
        session.commit()
        return {"job": None}
    job = judgement_job(session, got, settings) if isinstance(got, Judgement) else check_job(session, got, settings)
    session.commit()
    return {"job": job}


@router.post("/heartbeat")
def heartbeat(body: HeartbeatBody, worker: Worker = Depends(current_worker), session: Session = Depends(get_session),
              settings=Depends(get_settings)):
    """{cancel, reclaimed}: reclaimed lists the jobs this worker held without naming them, returned to the queue."""
    reply = q.heartbeat(session, worker, state=body.state, judgement_id=body.judgement_id, attempt=body.attempt,
                        phase=body.phase, tests_done=body.tests_done, tests_total=body.tests_total, stats=body.stats,
                        check_id=body.check_id, settings=settings.queue)
    session.commit()
    return reply


@router.get("/problem-versions/{version_id}/manifest")
def manifest(version_id: int, worker: Worker = Depends(current_worker), session: Session = Depends(get_session)):
    built = build_manifest(session, version_id)
    if built is None:
        raise HTTPException(404, "no such problem version")
    session.commit()
    data, digest = built
    return {"manifest_sha256": digest, "manifest": data}


@router.get("/blobs/{sha256}")
def blob(sha256: str, worker: Worker = Depends(current_worker), session: Session = Depends(get_session),
         blobs: BlobStore = Depends(get_blobs)):
    if not valid_sha256(sha256):
        raise HTTPException(400, "bad sha256")
    path = blobs.path(sha256)
    if not path.is_file():
        raise HTTPException(404, "no such blob")
    session.commit()  # records the heartbeat before the (possibly long) download
    return StreamingResponse(blobs.iter_chunks(sha256), media_type="application/octet-stream",
                             headers={"Content-Length": str(path.stat().st_size), "Cache-Control": "immutable"})


@router.post("/judgements/{judgement_id}/release")
def release(judgement_id: int, body: ReleaseBody, worker: Worker = Depends(current_worker),
            session: Session = Depends(get_session)):
    done = q.release(session, worker, judgement_id, body.attempt, body.reason)
    session.commit()
    return {"released": done}


def _rejected(session: Session, worker: Worker, judgement_id: int, attempt, reason: str, settings) -> HTTPException:
    """A refused result fails the attempt (queue.reject_result): requeued, or internal_error after max_attempts, with
    the reason in the judgement's history. 422 tells the worker; it logs the error and takes the next job."""
    session.rollback()
    outcome = q.reject_result(session, worker, judgement_id, attempt, reason, settings.queue)
    session.commit()
    return HTTPException(422, {"code": "bad_result", "message": reason, "judgement": outcome})


@router.post("/judgements/{judgement_id}/result")
def result(judgement_id: int, payload: dict = Body(...), worker: Worker = Depends(current_worker),
           session: Session = Depends(get_session), settings=Depends(get_settings)):
    # validated here, not by fastapi: a malformed result must fail its attempt too, not leave the job held
    try:
        body = ResultBody.model_validate(payload)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])
        raise _rejected(session, worker, judgement_id, payload.get("attempt"), f"malformed result: {problems}",
                        settings)
    try:
        outcome = q.finish(session, worker, judgement_id, body.model_dump(), settings.queue)
    except q.LeaseLost as exc:
        session.rollback()
        raise HTTPException(409, {"code": "lease_lost", "message": str(exc)})
    except q.BadResult as exc:
        raise _rejected(session, worker, judgement_id, body.attempt, str(exc), settings)
    session.commit()
    return {"status": outcome}


@router.post("/problem-checks/{check_id}/release")
def release_check(check_id: int, body: ReleaseBody, worker: Worker = Depends(current_worker),
                  session: Session = Depends(get_session)):
    done = checks.release_by_worker(session, worker, check_id, body.attempt, body.reason)
    session.commit()
    return {"released": done}


@router.post("/problem-checks/{check_id}/result")
def check_result(check_id: int, body: CheckResultBody, worker: Worker = Depends(current_worker),
                 session: Session = Depends(get_session), settings=Depends(get_settings)):
    try:
        outcome = checks.finish(session, worker, check_id, body.model_dump(), settings.queue)
    except checks.CheckLeaseLost as exc:
        session.rollback()
        raise HTTPException(409, {"code": "lease_lost", "message": str(exc)})
    except checks.BadCheckResult as exc:
        session.rollback()
        raise HTTPException(422, str(exc))
    session.commit()
    return {"status": outcome}
