"""Checks of problem versions by workers (session 4a.4; docs/QUEUE.md "Проверки задач", D-048..D-055).

A worker compiles the checker and the interactor of a version and runs its reference solutions against the
expected verdicts. The rows live in `version_checks`, not in `judgements`: checks are outside the fair queue and
outside the students' load indicator.

* A worker gets a check only from `queue.claim_next`, after the fair claim of submissions found nothing for it
  (two phases under the one claim advisory lock), and only while fewer than `max_concurrent_checks` run.
* `attempt` is the fencing token, as in judgements; `failures` counts lost workers and internal errors, at
  `max_attempts` the check ends failed.
* The outcome of a finished check becomes the version status, its report is copied to
  problem_versions.check_report. A recheck does not touch the status until it ends (D-054).

Functions take a Session and never commit (the caller owns the transaction), like app.queue. This module does not
import app.queue: the queue calls it (claim, register, deregister, reaper, load).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .models import (CheckOutcome, CheckReason, CheckStatus, Contest, ContestProblem, ContestState, Problem,
                     ProblemVersion, Program, ProgramRole, QueueEvent, VersionCheck, VersionStatus, Worker)
from .settings import QueueSettings

CAPABILITY = "problem_check"
TIMEOUT_GRACE_S = 60.0  # the worker stops itself at the timeout; the reaper steps in only this much later
# programs that make a version worth checking: a custom checker, an interactor, reference solutions (D-051)
CHECKED_ROLES = (ProgramRole.checker, ProgramRole.interactor, ProgramRole.solution)
# reasons a report may carry (docs/WORKER_PROTOCOL.md); a ready report has none
REPORT_REASONS = frozenset({"checker_compile_failed", "interactor_compile_failed", "solution_mismatch", "timeout",
                            "internal_error", "worker_lost", "nothing_to_check"})
MAX_REPORT_BYTES = 512 * 1024  # compile logs are cut by the worker; a bigger report is a bug, not data


class CheckError(Exception):
    """A refusal with a stable code (judgectl prints the text, the admin API returns the code)."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class CheckLeaseLost(Exception):
    """The check is not held by this worker with this attempt (any more)."""


class BadCheckResult(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(session: Session, kind: str, check: VersionCheck, worker_id=None, **details) -> None:
    session.add(QueueEvent(kind=kind, worker_id=worker_id, details={"check_id": check.id,
                                                                    "problem_version_id": check.problem_version_id,
                                                                    **details}))


# ---------------------------------------------------------------------------------- what is checked
def needs_check(session: Session, version_id: int) -> bool:
    """True if the version has a custom checker, an interactor or reference solutions (D-051)."""
    return session.execute(select(func.count()).select_from(Program).where(
        Program.version_id == version_id, Program.role.in_(CHECKED_ROLES))).scalar_one() > 0


def enqueue_check(session: Session, version: ProblemVersion, reason: CheckReason,
                  requested_by: Optional[int] = None) -> VersionCheck:
    """A new queued check. Raises IntegrityError if the version already has an unfinished one."""
    check = VersionCheck(problem_version_id=version.id, reason=reason, status=CheckStatus.queued,
                         requested_by=requested_by, attempt=0, failures=0)
    session.add(check)
    session.flush()
    _event(session, "check_queued", check, reason=reason.value, requested_by=requested_by)
    return check


@dataclass
class RecheckOutcome:
    kind: str  # "created" | "already_queued" | "already_running"
    check: VersionCheck


def request_recheck(session: Session, version: ProblemVersion, requested_by: Optional[int] = None) -> RecheckOutcome:
    """Check a version again (e.g. after a compiler change). The status stays as it is until the check ends."""
    session.execute(select(ProblemVersion.id).where(ProblemVersion.id == version.id).with_for_update())
    problem = session.get(Problem, version.problem_id)
    if problem.archived:
        raise CheckError(f"problem {problem.slug} is archived: unarchive it first (judgectl problem unarchive "
                         f"{problem.slug})", "problem_archived")
    active = active_check(session, version.id)
    if active is not None:
        return RecheckOutcome("already_queued" if active.status == CheckStatus.queued else "already_running", active)
    return RecheckOutcome("created", enqueue_check(session, version, CheckReason.recheck, requested_by))


def recheck(session: Session, actor, version: ProblemVersion, now: datetime) -> RecheckOutcome:
    """request_recheck for judgectl and the admin API, written to audit_log (problem.recheck) when a check is queued."""
    from .auth import audit  # here: the queue imports this module, auth is not needed there

    out = request_recheck(session, version, actor.user_id)
    if out.kind == "created":
        audit.record(session, actor, "problem.recheck", now, object_type="problem_version", object_id=version.id,
                     details={"slug": session.get(Problem, version.problem_id).slug, "version": version.version,
                              "check_id": out.check.id, "status": version.status.value})
    return out


def active_check(session: Session, version_id: int) -> Optional[VersionCheck]:
    return session.execute(select(VersionCheck).where(
        VersionCheck.problem_version_id == version_id,
        VersionCheck.status.in_((CheckStatus.queued, CheckStatus.running)))).scalar_one_or_none()


def history(session: Session, version_id: int, limit: int = 10) -> list:
    return list(session.execute(select(VersionCheck).where(VersionCheck.problem_version_id == version_id)
                                .order_by(VersionCheck.id.desc()).limit(limit)).scalars())


# ---------------------------------------------------------------------------------- claim (phase 2)
def running_count(session: Session) -> int:
    return session.execute(select(func.count()).select_from(VersionCheck).where(
        VersionCheck.status == CheckStatus.running)).scalar_one()


def claim_check(session: Session, worker: Worker, settings: QueueSettings) -> Optional[VersionCheck]:
    """Phase 2 of the claim. Call ONLY from queue.claim_next, which already holds the claim advisory lock and found
    no submission for this worker.

    The quota is exact without races: every claim takes the same transaction-level advisory lock first, so claims run
    one after another, and the count below is read after the lock is held (READ COMMITTED takes a new snapshot per
    statement), i.e. it sees the check the previous claimer committed. Other writers (finish, release, reaper) only
    lower the count, so a stale read can only refuse a check, never exceed the quota.
    """
    if CAPABILITY not in (worker.capabilities or []):
        return None
    if running_count(session) >= settings.max_concurrent_checks:
        return None
    pick = select(VersionCheck.id).where(VersionCheck.status == CheckStatus.queued) \
        .order_by(VersionCheck.created_at, VersionCheck.id).limit(1).with_for_update(skip_locked=True)
    check_id = session.execute(pick).scalar_one_or_none()
    if check_id is None:
        return None
    session.execute(update(VersionCheck).where(VersionCheck.id == check_id).values(
        status=CheckStatus.running, worker_id=worker.id, attempt=VersionCheck.attempt + 1, started_at=func.now(),
        heartbeat_at=func.now(), progress_done=None, progress_total=None))
    worker.state = "busy"
    check = session.get(VersionCheck, check_id, populate_existing=True)
    _event(session, "check_claimed", check, worker.id, attempt=check.attempt)
    return check


def held_by(session: Session, worker_id: int) -> list:
    return list(session.execute(select(VersionCheck).where(
        VersionCheck.worker_id == worker_id, VersionCheck.status == CheckStatus.running).with_for_update()).scalars())


def checking_worker_ids(session: Session):
    """A subquery: the workers that hold a running check (not capacity for submissions, D-050)."""
    return select(VersionCheck.worker_id).where(VersionCheck.status == CheckStatus.running,
                                                VersionCheck.worker_id.is_not(None))


# ---------------------------------------------------------------------------------- heartbeat / release
def heartbeat(session: Session, worker: Worker, check_id: int, attempt: Optional[int],
              done: Optional[int], total: Optional[int]) -> bool:
    """Record a heartbeat about a check. Returns cancel: True if the worker does not hold it any more."""
    values = {"heartbeat_at": func.now()}
    if done is not None:
        values["progress_done"] = done
    if total is not None:
        values["progress_total"] = total
    result = session.execute(update(VersionCheck).where(
        VersionCheck.id == check_id, VersionCheck.worker_id == worker.id, VersionCheck.attempt == attempt,
        VersionCheck.status == CheckStatus.running).values(**values))
    return result.rowcount == 0


def _requeue(check: VersionCheck) -> None:
    check.status, check.worker_id, check.started_at, check.heartbeat_at = CheckStatus.queued, None, None, None
    check.progress_done = check.progress_total = None


def release(session: Session, check: VersionCheck, reason: str) -> None:
    """Back to the queue without counting a failure (the worker is stopping)."""
    _event(session, "check_released", check, check.worker_id, reason=reason, attempt=check.attempt)
    _requeue(check)


def release_by_worker(session: Session, worker: Worker, check_id: int, attempt: int, reason: str) -> bool:
    check = _locked(session, check_id)
    if check is None or check.worker_id != worker.id or check.attempt != attempt or check.status != CheckStatus.running:
        return False
    release(session, check, reason)
    if worker.state == "busy":
        worker.state = "idle"
    return True


def _locked(session: Session, check_id: int) -> Optional[VersionCheck]:
    return session.execute(select(VersionCheck).where(VersionCheck.id == check_id).with_for_update()).scalar_one_or_none()


# ---------------------------------------------------------------------------------- the end of a check
def _close(session: Session, check: VersionCheck, outcome: CheckOutcome, report: dict,
           error: Optional[str] = None) -> None:
    check.status, check.outcome, check.report, check.finished_at = CheckStatus.done, outcome, report, func.now()
    check.error = error
    if check.progress_total is not None and outcome == CheckOutcome.ready:
        check.progress_done = check.progress_total
    version = session.execute(select(ProblemVersion).where(ProblemVersion.id == check.problem_version_id)
                              .with_for_update()).scalar_one()
    version.status = VersionStatus(outcome.value)
    version.check_report = report
    _event(session, "check_done", check, check.worker_id, outcome=outcome.value, reason=report.get("reason"))


def nothing_to_check_report() -> dict:
    """The report of a version that has no custom checker, no interactor and no reference solutions (D-051)."""
    return {"outcome": CheckOutcome.ready.value, "reason": "nothing_to_check", "by": "server", "programs": [],
            "solutions": []}


def server_report(reason: str, error: str) -> dict:
    """The report of a check the server ended (the worker sent none): gave up or timed out."""
    return {"outcome": CheckOutcome.failed.value, "reason": reason, "error": error, "by": "server",
            "programs": [], "solutions": []}


def fail_attempt(session: Session, check: VersionCheck, reason: str, error: str, settings: QueueSettings) -> str:
    """An attempt went wrong (worker lost, internal error): requeue, or end failed after max_attempts."""
    check.failures += 1
    check.error = error
    if check.failures >= settings.max_attempts:
        _close(session, check, CheckOutcome.failed,
               server_report(reason, f"gave up after {check.failures} failed attempts; last: {error}"), check.error)
        return "gave_up"
    _event(session, "check_requeued", check, check.worker_id, reason=reason, error=error, failures=check.failures)
    _requeue(check)
    return "requeued"


def finish(session: Session, worker: Worker, check_id: int, payload: dict, settings: QueueSettings) -> str:
    """Record a worker's result: {"attempt", "outcome": "ready"|"failed", "report": {...}} or, when the worker itself
    failed (not the problem), {"attempt", "error": "..."}. Returns "recorded" | "already_recorded" | "requeued".

    Raises CheckLeaseLost if the check is not held by this worker with this attempt, BadCheckResult on a bad payload.
    """
    check = _locked(session, check_id)
    if check is None:
        raise CheckLeaseLost("no such check")
    if check.worker_id != worker.id or check.attempt != payload.get("attempt"):
        raise CheckLeaseLost("the check belongs to another worker or attempt")
    if check.status == CheckStatus.done:
        if (check.report or {}).get("by") == "server":
            raise CheckLeaseLost("the server already ended this check (timeout or gave up)")
        return "already_recorded"  # the same result delivered twice (network retry)
    if check.status != CheckStatus.running:
        raise CheckLeaseLost("the check is not in progress")
    outcome, report, error = payload.get("outcome"), payload.get("report"), payload.get("error")
    if outcome is None:
        if not error:
            raise BadCheckResult("neither an outcome nor an error")
        if worker.state == "busy":
            worker.state = "idle"
        kind = fail_attempt(session, check, "internal_error", str(error), settings)
        return "recorded" if kind == "gave_up" else "requeued"
    try:
        outcome = CheckOutcome(outcome)
    except ValueError as exc:
        raise BadCheckResult(f"bad outcome: {outcome!r}") from exc
    _validate_report(report, outcome)
    if worker.state == "busy":
        worker.state = "idle"
    _close(session, check, outcome, report)
    return "recorded"


def _validate_report(report, outcome: CheckOutcome) -> None:
    if not isinstance(report, dict) or report.get("outcome") != outcome.value:
        raise BadCheckResult("the report must be an object with the same outcome")
    if "by" in report:
        raise BadCheckResult("'by' is set by the server only")
    reason = report.get("reason")
    if reason is not None and reason not in REPORT_REASONS:
        raise BadCheckResult(f"unknown reason: {reason!r}")
    if outcome == CheckOutcome.failed and reason is None:
        raise BadCheckResult("a failed report needs a reason")
    for key in ("programs", "solutions"):
        if not isinstance(report.get(key, []), list):
            raise BadCheckResult(f"'{key}' must be a list")
    if len(json.dumps(report)) > MAX_REPORT_BYTES:
        raise BadCheckResult(f"the report is larger than {MAX_REPORT_BYTES} bytes")


# ---------------------------------------------------------------------------------- reaper
def reap(session: Session, settings: QueueSettings) -> dict:
    """Checks of lost workers go back to the queue (a failure each); checks over the time limit end failed at once."""
    now = _now()
    stale = now - timedelta(seconds=settings.heartbeat_timeout_s)
    too_old = now - timedelta(seconds=settings.check_timeout_s + TIMEOUT_GRACE_S)
    rows = session.execute(
        select(VersionCheck).outerjoin(Worker, Worker.id == VersionCheck.worker_id)
        .where(VersionCheck.status == CheckStatus.running, or_(
            Worker.id.is_(None), Worker.last_heartbeat.is_(None), Worker.last_heartbeat < stale,
            VersionCheck.heartbeat_at.is_(None), VersionCheck.heartbeat_at < stale,
            VersionCheck.started_at < too_old))
        .with_for_update(of=VersionCheck, skip_locked=True)
    ).scalars().all()
    out = {"checks_requeued": 0, "checks_failed": 0, "checks_timed_out": 0}
    for check in rows:
        if check.started_at is not None and check.started_at < too_old:
            limit = int(settings.check_timeout_s)
            _close(session, check, CheckOutcome.failed,
                   server_report("timeout", f"the check took longer than {limit} s (JUDGE_CHECK_TIMEOUT_SEC)"))
            out["checks_timed_out"] += 1
        elif fail_attempt(session, check, "worker_lost", "worker lost (no heartbeat)", settings) == "gave_up":
            out["checks_failed"] += 1
        else:
            out["checks_requeued"] += 1
    return out


# ---------------------------------------------------------------------------------- for the admin
def overview(session: Session) -> dict:
    """Counts for /system and judgectl status (not shown to students)."""
    counts = dict(session.execute(select(VersionCheck.status, func.count()).where(
        VersionCheck.status != CheckStatus.done).group_by(VersionCheck.status)).all())
    return {"queued": counts.get(CheckStatus.queued, 0), "running": counts.get(CheckStatus.running, 0)}


# ---------------------------------------------------------------------------------- what admins are told
def where_report(slug: str, number: int) -> str:
    """Where the check report of a version is (refusals and warnings name it)."""
    return f"judgectl problem show {slug}@{number}, GET /api/admin/problems/{slug}/versions/{number}"


def not_ready_reason(slug: str, version: ProblemVersion) -> str:
    """Why a version cannot be pinned, and where to look (D-055)."""
    state = {VersionStatus.pending_check: "is pending_check: its check is not finished yet",
             VersionStatus.failed: "is failed: its check did not pass"}.get(version.status, f"is {version.status.value}")
    return f"{slug} v{version.version} {state}; only ready versions can be pinned (report: " \
           f"{where_report(slug, version.version)})"


def failed_pin_warning(slug: str, number: int, reason: Optional[str]) -> str:
    return (f"{slug} v{number} is pinned but failed its check ({reason or 'see the report'}): submissions are still "
            f"judged; fix the problem and pin a ready version (report: {where_report(slug, number)})")


def pinned_failed(session: Session, contest_id: Optional[int] = None) -> list:
    """Visible contest problems pinned to a version whose (re)check failed (D-054). Archived contests are left out
    unless one contest is asked for."""
    query = (select(Contest.slug, ContestProblem.label, Problem.slug, ProblemVersion.version,
                    ProblemVersion.check_report)
             .join(ContestProblem, ContestProblem.contest_id == Contest.id)
             .join(ProblemVersion, ProblemVersion.id == ContestProblem.problem_version_id)
             .join(Problem, Problem.id == ProblemVersion.problem_id)
             .where(ProblemVersion.status == VersionStatus.failed, ContestProblem.hidden.is_(False))
             .order_by(Contest.slug, ContestProblem.position, ContestProblem.id))
    query = query.where(Contest.id == contest_id) if contest_id is not None \
        else query.where(Contest.state != ContestState.archived)
    out = []
    for contest, label, slug, number, report in session.execute(query).all():
        reason = (report or {}).get("reason")
        out.append({"contest": contest, "label": label, "problem": slug, "version": number, "reason": reason,
                    "warning": failed_pin_warning(slug, number, reason)})
    return out


def check_row(check: VersionCheck, slug: Optional[str] = None, number: Optional[int] = None) -> dict:
    """One check for lists and the version card (the full report stays in check_report / `report`)."""
    report = check.report or {}
    row = {"id": check.id, "reason": check.reason.value, "status": check.status.value,
           "outcome": check.outcome.value if check.outcome else None, "report_reason": report.get("reason"),
           "attempt": check.attempt, "failures": check.failures, "worker_id": check.worker_id,
           "progress": None if check.progress_total is None else f"{check.progress_done or 0}/{check.progress_total}",
           "requested_by": check.requested_by, "error": check.error, "created_at": check.created_at,
           "started_at": check.started_at, "finished_at": check.finished_at}
    if slug is not None:
        row = {"problem": slug, "version": number, **row}
    return row


def recent(session: Session, limit: int = 20) -> list:
    """The newest checks of every version, newest first: rows of check_row with the problem and the version."""
    rows = session.execute(select(VersionCheck, Problem.slug, ProblemVersion.version)
                           .join(ProblemVersion, ProblemVersion.id == VersionCheck.problem_version_id)
                           .join(Problem, Problem.id == ProblemVersion.problem_id)
                           .order_by(VersionCheck.id.desc()).limit(limit)).all()
    return [check_row(c, slug, number) for c, slug, number in rows]


def system_section(session: Session, recent_failed: int = 5) -> dict:
    """The "checks" section of /system and judgectl status."""
    failed = session.execute(select(VersionCheck, Problem.slug, ProblemVersion.version)
                             .join(ProblemVersion, ProblemVersion.id == VersionCheck.problem_version_id)
                             .join(Problem, Problem.id == ProblemVersion.problem_id)
                             .where(VersionCheck.outcome == CheckOutcome.failed)
                             .order_by(VersionCheck.finished_at.desc(), VersionCheck.id.desc())
                             .limit(recent_failed)).all()
    return {**overview(session),
            "pending_versions": session.execute(select(func.count()).select_from(ProblemVersion).where(
                ProblemVersion.status == VersionStatus.pending_check)).scalar_one(),
            "recent_failed": [check_row(c, slug, number) for c, slug, number in failed],
            "pinned_failed": pinned_failed(session)}
