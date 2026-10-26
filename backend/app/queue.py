"""The judging queue on PostgreSQL (module 3). See docs/QUEUE.md.

Functions take a SQLAlchemy Session and never commit: the caller owns the transaction
(`db.session_scope()` in the API, tests commit explicitly).

Nothing here has been executed against a real PostgreSQL by the author: run tests/pg (docs/QUEUE.md).
"""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from .loadinfo import LoadSnapshot, estimate_wait_seconds, typical_judge_seconds
from .models import (
    ContestProblem, Judgement, JudgementReason, JudgementStatus, QueueEvent, Submission, TestCase,
    TestGroup, TestResult, Verdict, Worker, WorkerToken,
)
from .settings import QueueSettings

PRIORITY_URGENT = -10
PRIORITY_NORMAL = 0
PRIORITY_REJUDGE = 10
CLAIM_LOCK_KEY = 7_340_001  # pg_advisory_xact_lock key: claims are serialized (milliseconds), see docs/QUEUE.md
ACTIVE = (JudgementStatus.queued, JudgementStatus.compiling, JudgementStatus.running)
IN_FLIGHT = (JudgementStatus.compiling, JudgementStatus.running)
DONE = (JudgementStatus.finished, JudgementStatus.internal_error)


class QueueError(Exception):
    pass


class LeaseLost(QueueError):
    """The judgement is not (any more) held by this worker with this attempt number."""


class BadResult(QueueError):
    pass


class TokenError(QueueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(session: Session, kind: str, worker_id=None, judgement_id=None, **details) -> None:
    session.add(QueueEvent(kind=kind, worker_id=worker_id, judgement_id=judgement_id, details=details))


# ---------------------------------------------------------------------------------- tokens and workers
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_worker_token(session: Session, name: str, capabilities: Optional[list] = None) -> str:
    """Returns the plain token; it is shown once and never stored (only its sha256)."""
    token = "jw_" + secrets.token_urlsafe(32)
    session.add(WorkerToken(name=name, token_hash=hash_token(token), capabilities=list(capabilities or ["cpu"])))
    session.flush()
    return token


def authenticate(session: Session, token: str) -> Optional[WorkerToken]:
    row = session.execute(select(WorkerToken).where(WorkerToken.token_hash == hash_token(token))).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    session.execute(  # at most one write per minute per token
        text("UPDATE worker_tokens SET last_used_at = now() WHERE id = :id "
             "AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"), {"id": row.id})
    return row


def register_worker(session: Session, token: WorkerToken, name: str, capabilities: list, info: dict,
                    settings: QueueSettings) -> Worker:
    """Create or take over the worker row called `name`. A new `instance_id` supersedes an older process."""
    if not set(capabilities) <= set(token.capabilities):
        raise TokenError(f"token {token.name!r} does not allow capabilities {sorted(set(capabilities) - set(token.capabilities))}")
    worker = session.execute(select(Worker).where(Worker.name == name).with_for_update()).scalar_one_or_none()
    if worker is not None and worker.token_id != token.id:
        raise TokenError(f"worker name {name!r} belongs to another token")
    if worker is None:
        worker = Worker(name=name, token_id=token.id, capabilities=list(capabilities))
        session.add(worker)
        session.flush()
    else:
        # a restarted process cannot be running anything: whatever the old instance held is lost
        for j in _held_by(session, worker.id):
            _fail_attempt(session, j, "worker restarted", settings)
    worker.instance_id = str(uuid.uuid4())
    worker.capabilities = list(capabilities)
    worker.info = info
    worker.stats = {}
    worker.state = "idle"
    worker.started_at = func.now()
    worker.last_heartbeat = func.now()
    session.flush()
    _event(session, "worker_registered", worker.id, name=name)
    return worker


def _held_by(session: Session, worker_id: int) -> list:
    return list(session.execute(
        select(Judgement).where(Judgement.worker_id == worker_id, Judgement.status.in_(IN_FLIGHT)).with_for_update()
    ).scalars())


def deregister_worker(session: Session, worker: Worker) -> None:
    for j in _held_by(session, worker.id):
        _release(session, j, "worker deregistered")
    worker.state = "stopped"
    _event(session, "worker_stopped", worker.id)


# ---------------------------------------------------------------------------------- enqueue / rejudge
def enqueue(session: Session, submission: Submission, *, problem_version_id: Optional[int] = None,
            reason: JudgementReason = JudgementReason.initial, priority: Optional[int] = None,
            requires: Optional[list] = None, requested_by: Optional[int] = None) -> Judgement:
    """Put a submission into the queue. Raises IntegrityError if it already has an unfinished judgement."""
    if problem_version_id is None:
        problem_version_id = session.get(ContestProblem, submission.contest_problem_id).problem_version_id
    if priority is None:
        priority = PRIORITY_NORMAL if reason == JudgementReason.initial else PRIORITY_REJUDGE
    j = Judgement(
        submission_id=submission.id, problem_version_id=problem_version_id, reason=reason,
        status=JudgementStatus.queued, is_current=(reason == JudgementReason.initial), priority=priority,
        requires=list(requires or ["cpu"]), requested_by=requested_by,
    )
    session.add(j)
    session.flush()
    return j


@dataclass
class RejudgeOutcome:
    kind: str  # "created" | "retargeted" | "already_running"
    judgement: Judgement


def request_rejudge(session: Session, submission_id: int, requested_by: Optional[int] = None,
                    priority: int = PRIORITY_REJUDGE) -> RejudgeOutcome:
    """History is never lost: a rejudge is a NEW judgement row. The old one stays current (and visible to the
    student) until the new one finishes without an internal error, then the current flag moves."""
    submission = session.execute(select(Submission).where(Submission.id == submission_id).with_for_update()).scalar_one()
    version_id = session.get(ContestProblem, submission.contest_problem_id).problem_version_id
    active = session.execute(select(Judgement).where(Judgement.submission_id == submission_id,
                                                     Judgement.status.in_(ACTIVE)).with_for_update()).scalar_one_or_none()
    if active is not None:
        if active.status == JudgementStatus.queued:
            active.problem_version_id = version_id  # not started yet: just aim it at the current version
            active.priority = min(active.priority, priority)
            return RejudgeOutcome("retargeted", active)
        return RejudgeOutcome("already_running", active)
    j = enqueue(session, submission, problem_version_id=version_id, reason=JudgementReason.rejudge,
                priority=priority, requested_by=requested_by)
    _event(session, "rejudge", judgement_id=j.id, submission_id=submission_id, requested_by=requested_by)
    return RejudgeOutcome("created", j)


def rejudge_contest_problem(session: Session, contest_problem_id: int, requested_by: Optional[int] = None,
                            only_current_verdicts: Optional[list] = None) -> dict:
    """Rejudge every submission of a contest problem (e.g. after tests were fixed and the version bumped)."""
    query = select(Submission.id).where(Submission.contest_problem_id == contest_problem_id).order_by(Submission.id)
    counts = {"created": 0, "retargeted": 0, "already_running": 0}
    for (submission_id,) in session.execute(query).all():
        if only_current_verdicts:
            current = session.execute(select(Judgement.verdict).where(
                Judgement.submission_id == submission_id, Judgement.is_current)).scalar_one_or_none()
            if current is None or current.value not in only_current_verdicts:
                continue
        counts[request_rejudge(session, submission_id, requested_by).kind] += 1
    return counts


# ---------------------------------------------------------------------------------- claim
# Shared by the claim and by the position query. Fair order: see loadinfo.fair_key.
_FAIR_CTES = """
inflight AS (
    SELECT s.user_id, count(*) AS n
    FROM judgements j JOIN submissions s ON s.id = j.submission_id
    WHERE j.status IN ('compiling', 'running')
    GROUP BY s.user_id
),
recent AS (
    SELECT s.user_id, count(*) AS n
    FROM judgements j JOIN submissions s ON s.id = j.submission_id
    WHERE j.started_at > now() - CAST(:window AS double precision) * interval '1 second'
    GROUP BY s.user_id
)
"""

CLAIM_SQL = text(f"""
WITH {_FAIR_CTES},
pick AS (
    SELECT j.id
    FROM judgements j
    JOIN submissions s ON s.id = j.submission_id
    LEFT JOIN inflight f ON f.user_id = s.user_id
    LEFT JOIN recent r ON r.user_id = s.user_id
    WHERE j.status = 'queued'
      AND j.requires <@ CAST(:caps AS jsonb)
    ORDER BY j.priority, COALESCE(f.n, 0), COALESCE(r.n, 0), j.created_at, j.id
    LIMIT 1
    FOR UPDATE OF j SKIP LOCKED
)
UPDATE judgements u
SET status = 'compiling', worker_id = :worker_id, started_at = now(), attempt = u.attempt + 1,
    tests_done = NULL, tests_total = NULL
FROM pick
WHERE u.id = pick.id
RETURNING u.id
""")

POSITION_SQL = text(f"""
WITH {_FAIR_CTES},
ranked AS (
    SELECT j.id, s.user_id,
           row_number() OVER (ORDER BY j.priority, COALESCE(f.n, 0), COALESCE(r.n, 0), j.created_at, j.id) AS pos
    FROM judgements j
    JOIN submissions s ON s.id = j.submission_id
    LEFT JOIN inflight f ON f.user_id = s.user_id
    LEFT JOIN recent r ON r.user_id = s.user_id
    WHERE j.status = 'queued'
)
SELECT id, user_id, pos FROM ranked
""")


def claim(session: Session, worker: Worker, settings: QueueSettings) -> Optional[Judgement]:
    """Hand the next judgement to `worker`, or None.

    The advisory lock serializes claims so that each one sees the previous one's effect: without it two
    simultaneous claims would both pick the same student's jobs (fairness is computed from a snapshot).
    FOR UPDATE SKIP LOCKED stays as the safety net against any other writer holding a queued row.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": CLAIM_LOCK_KEY})
    row = session.execute(CLAIM_SQL, {"worker_id": worker.id, "caps": json.dumps(list(worker.capabilities)),
                                      "window": settings.recent_window_s}).first()
    if row is None:
        return None
    worker.state = "busy"
    return session.get(Judgement, row.id, populate_existing=True)


def queue_position(session: Session, settings: QueueSettings, judgement_id: Optional[int] = None,
                   user_id: Optional[int] = None) -> Optional[int]:
    """1-based place in the fair order (of a judgement, or the best place of a student's queued judgements)."""
    rows = session.execute(POSITION_SQL, {"window": settings.recent_window_s}).all()
    mine = [r.pos for r in rows if (judgement_id is not None and r.id == judgement_id)
            or (user_id is not None and r.user_id == user_id)]
    return min(mine) if mine else None


# ---------------------------------------------------------------------------------- heartbeat / release
def heartbeat(session: Session, worker: Worker, *, state: str, judgement_id: Optional[int], attempt: Optional[int],
              phase: Optional[str], tests_done: Optional[int], tests_total: Optional[int], stats: dict) -> dict:
    worker.last_heartbeat = func.now()
    worker.state = state if state in ("idle", "busy", "draining", "stopped") else "idle"
    worker.stats = stats or {}
    cancel = False
    if judgement_id is not None:
        values = {"status": JudgementStatus.running if phase == "running" else JudgementStatus.compiling}
        if tests_done is not None:
            values["tests_done"] = tests_done
        if tests_total is not None:
            values["tests_total"] = tests_total
        result = session.execute(update(Judgement).where(
            Judgement.id == judgement_id, Judgement.worker_id == worker.id, Judgement.attempt == attempt,
            Judgement.status.in_(IN_FLIGHT)).values(**values))
        cancel = result.rowcount == 0  # taken away (reaper), finished elsewhere, or never ours
    return {"cancel": cancel}


def _release(session: Session, j: Judgement, reason: str) -> None:
    j.status, j.worker_id, j.started_at, j.tests_done, j.tests_total = JudgementStatus.queued, None, None, None, None
    _event(session, "released", judgement_id=j.id, reason=reason)


def release(session: Session, worker: Worker, judgement_id: int, attempt: int, reason: str) -> bool:
    """A worker that is shutting down gives a job back. It does not count as a failure of the job."""
    j = session.execute(select(Judgement).where(Judgement.id == judgement_id).with_for_update()).scalar_one_or_none()
    if j is None or j.worker_id != worker.id or j.attempt != attempt or j.status not in IN_FLIGHT:
        return False
    _release(session, j, reason)
    return True


# ---------------------------------------------------------------------------------- failures and the reaper
def _fail_attempt(session: Session, j: Judgement, reason: str, settings: QueueSettings) -> str:
    """An attempt went wrong (worker lost, internal error). Requeue, or give up after max_attempts."""
    j.failures += 1
    if j.failures >= settings.max_attempts:
        j.status, j.verdict, j.finished_at = JudgementStatus.internal_error, Verdict.IE, func.now()
        j.error = f"gave up after {j.failures} failed attempts; last reason: {reason}"
        _event(session, "gave_up", j.worker_id, j.id, reason=reason, failures=j.failures)
        return "gave_up"
    j.status, j.worker_id, j.started_at, j.tests_done, j.tests_total = JudgementStatus.queued, None, None, None, None
    _event(session, "requeued", None, j.id, reason=reason, failures=j.failures)
    return "requeued"


def reap(session: Session, settings: QueueSettings) -> dict:
    """Return judgements of lost workers to the queue. Safe to run from several processes (SKIP LOCKED)."""
    now = _now()
    lost = session.execute(
        select(Judgement).outerjoin(Worker, Worker.id == Judgement.worker_id)
        .where(Judgement.status.in_(IN_FLIGHT), or_(
            Worker.id.is_(None), Worker.last_heartbeat.is_(None),
            Worker.last_heartbeat < now - timedelta(seconds=settings.heartbeat_timeout_s),
            Judgement.started_at < now - timedelta(seconds=settings.max_judging_s)))
        .with_for_update(of=Judgement, skip_locked=True)
    ).scalars().all()
    out = {"requeued": 0, "gave_up": 0}
    for j in lost:
        reason = "judging took too long" if (j.started_at and j.started_at < now - timedelta(seconds=settings.max_judging_s)) \
            else "worker lost (no heartbeat)"
        out[_fail_attempt(session, j, reason, settings)] += 1
    # workers that went silent are shown as stopped
    session.execute(update(Worker).where(
        Worker.state != "stopped", Worker.last_heartbeat < now - timedelta(seconds=settings.heartbeat_timeout_s)
    ).values(state="stopped"))
    session.execute(text("DELETE FROM queue_events WHERE at < now() - CAST(:d AS double precision) * interval '1 day'"),
                    {"d": settings.event_retention_days})
    return out


# ---------------------------------------------------------------------------------- results
def finish(session: Session, worker: Worker, judgement_id: int, payload: dict, settings: QueueSettings) -> str:
    """Record a worker's result. Returns "recorded" | "already_recorded" | "requeued".

    Raises LeaseLost if the judgement is not held by this worker with this attempt, BadResult on a malformed result.
    """
    j = session.execute(select(Judgement).where(Judgement.id == judgement_id).with_for_update()).scalar_one_or_none()
    if j is None:
        raise LeaseLost("no such judgement")
    if j.worker_id != worker.id or j.attempt != payload.get("attempt"):
        raise LeaseLost("the judgement belongs to another worker or attempt")
    if j.status in DONE:
        return "already_recorded"  # the same result delivered twice (network retry)
    if j.status not in IN_FLIGHT:
        raise LeaseLost("the judgement is not in progress")
    try:
        verdict = Verdict(payload["verdict"])
        score = Decimal(str(payload.get("score", "0")))
    except (KeyError, ValueError, ArithmeticError) as exc:
        raise BadResult(f"bad verdict or score: {exc}") from exc

    if verdict == Verdict.IE:
        j.error = payload.get("error")
        if j.failures + 1 < settings.max_attempts:
            _fail_attempt(session, j, f"internal error: {payload.get('error')}", settings)
            return "requeued"
        j.failures += 1
        j.status, j.verdict, j.finished_at, j.score = JudgementStatus.internal_error, Verdict.IE, func.now(), Decimal(0)
        _event(session, "gave_up", worker.id, j.id, reason=payload.get("error"), failures=j.failures)
        return "recorded"  # a rejudge that ends this way stays non-current: the old verdict is kept

    tests = payload.get("tests") or []
    if tests:
        valid = set(session.execute(
            select(TestCase.id).join(TestGroup, TestGroup.id == TestCase.group_id)
            .where(TestGroup.version_id == j.problem_version_id)).scalars())
        unknown = [t["test_case_id"] for t in tests if t["test_case_id"] not in valid]
        if unknown:
            raise BadResult(f"test cases {unknown[:5]} do not belong to problem version {j.problem_version_id}")
    for t in tests:
        session.add(TestResult(
            judgement_id=j.id, test_case_id=t["test_case_id"], verdict=Verdict(t["verdict"]),
            time_ms=t.get("time_ms"), memory_kb=t.get("memory_kb"), exit_code=t.get("exit_code"),
            signal=t.get("signal"), tl_kind=t.get("tl_kind") if t["verdict"] == "TL" else None,
            checker_message=t.get("message") or None,
            stderr_excerpt=t.get("stderr_excerpt") or None, output_excerpt=t.get("output_excerpt") or None,
            visible_to_student=bool(t.get("visible_to_student")),
        ))
    j.verdict, j.score = verdict, score
    j.compile_log = payload.get("compile_log")
    j.summary = payload.get("summary") or None
    j.error = None
    j.status, j.finished_at = JudgementStatus.finished, func.now()
    if j.tests_total is not None:
        j.tests_done = j.tests_total
    session.flush()
    if not j.is_current:  # a finished rejudge takes over; the old judgement stays in the history
        session.execute(update(Judgement).where(Judgement.submission_id == j.submission_id, Judgement.is_current,
                                                Judgement.id != j.id).values(is_current=False))
        j.is_current = True
        session.flush()
    return "recorded"


# ---------------------------------------------------------------------------------- load and metrics
def _recent_durations(session: Session, limit: int = 50) -> list:
    rows = session.execute(
        select(func.extract("epoch", Judgement.finished_at - Judgement.started_at),
               func.extract("epoch", Judgement.started_at - Judgement.created_at))
        .where(Judgement.status == JudgementStatus.finished, Judgement.started_at.is_not(None),
               Judgement.finished_at > func.now() - timedelta(hours=1))
        .order_by(Judgement.finished_at.desc()).limit(limit)).all()
    return [(float(a), float(b)) for a, b in rows]


def _workers_online(session: Session, settings: QueueSettings) -> tuple:
    cutoff = _now() - timedelta(seconds=settings.heartbeat_timeout_s)
    rows = session.execute(select(Worker.state, func.count()).where(
        Worker.last_heartbeat >= cutoff, Worker.state != "stopped").group_by(Worker.state)).all()
    by_state = {state: n for state, n in rows}
    return sum(by_state.values()), by_state.get("busy", 0), by_state


def load_snapshot(session: Session, settings: QueueSettings, user_id: Optional[int] = None,
                  judgement_id: Optional[int] = None) -> LoadSnapshot:
    """Numbers for the students' load indicator: queue length, busy workers, expected wait."""
    queued = session.execute(select(func.count()).select_from(Judgement).where(
        Judgement.status == JudgementStatus.queued)).scalar_one()
    online, busy, _ = _workers_online(session, settings)
    typical = typical_judge_seconds([d for d, _w in _recent_durations(session)])
    snap = LoadSnapshot(queue_length=queued, workers_online=online, workers_busy=busy, typical_seconds=typical)
    if user_id is not None or judgement_id is not None:
        snap.my_position = queue_position(session, settings, judgement_id=judgement_id, user_id=user_id)
        if snap.my_position is not None:
            snap.my_wait_seconds = estimate_wait_seconds(snap.my_position, online, busy, typical)
    return snap


def collect_metrics(session: Session, settings: QueueSettings) -> dict:
    queued = session.execute(select(func.count(), func.min(Judgement.created_at)).where(
        Judgement.status == JudgementStatus.queued)).one()
    in_flight = session.execute(select(func.count()).select_from(Judgement).where(Judgement.status.in_(IN_FLIGHT))).scalar_one()
    online, _busy, by_state = _workers_online(session, settings)
    durations = _recent_durations(session)
    finished = dict(session.execute(select(Judgement.verdict, func.count()).where(
        Judgement.status == JudgementStatus.finished).group_by(Judgement.verdict)).all())
    requeued = session.execute(select(func.coalesce(func.sum(Judgement.failures), 0))).scalar_one()
    waits = sorted(w for _d, w in durations)
    return {
        "queue_length": queued[0],
        "oldest_queued_age_s": (_now() - queued[1]).total_seconds() if queued[1] else 0.0,
        "in_flight": in_flight, "workers_online": online, "workers_by_state": by_state,
        "typical_seconds": typical_judge_seconds([d for d, _w in durations]),
        "median_wait_s": waits[len(waits) // 2] if waits else 0.0,
        "finished_by_verdict": {(v.value if v else "none"): n for v, n in finished.items()},
        "requeued_total": int(requeued),
    }


def worker_table(session: Session, settings: QueueSettings) -> list:
    """One row per worker for the admin status page / CLI."""
    now = _now()
    held = {r.worker_id: r.id for r in session.execute(select(Judgement.worker_id, Judgement.id).where(
        Judgement.status.in_(IN_FLIGHT))).all()}
    rows = []
    for w in session.execute(select(Worker).order_by(Worker.name)).scalars():
        age = (now - w.last_heartbeat).total_seconds() if w.last_heartbeat else None
        online = age is not None and age <= settings.heartbeat_timeout_s and w.state != "stopped"
        rows.append({"id": w.id, "name": w.name, "state": w.state if online else "offline", "heartbeat_age_s": age,
                     "judgement_id": held.get(w.id), "capabilities": w.capabilities, "info": w.info, "stats": w.stats})
    return rows
