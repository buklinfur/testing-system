"""Rejudges chosen by the filters of view.py (R33, docs/QUEUE.md for what one rejudge does).

A rejudge of many submissions is two steps for the admin API: count first, then do it while the count is still
the same (`expect`), so a click never rejudges more than the teacher saw. It needs a narrowing filter (a contest
problem, a version or submission ids): there is no "rejudge the whole server". judgectl and the API call these.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from .. import queue as q
from ..auth import audit
from ..models import ContestProblem, Submission
from .view import Filters, ViewError, submissions_query


def select_ids(db: Session, f: Filters) -> list[tuple[int, int]]:
    """(submission id, contest problem id) of every submission the filter picks, oldest first."""
    if not f.narrowing():
        raise ViewError("say what to rejudge: a contest and a label, a contest problem, a problem version or "
                        "submission ids", "rejudge_needs_filter")
    query = submissions_query(db, f).with_only_columns(Submission.id, ContestProblem.id).order_by(Submission.id)
    return [tuple(r) for r in db.execute(query).all()]


def count(db: Session, f: Filters) -> int:
    return len(select_ids(db, f))


def run(db: Session, actor: audit.Actor, f: Filters, now: datetime, expect: Optional[int] = None) -> dict:
    """Rejudge what the filter picks; with `expect`, only if it still picks exactly that many (else ViewError
    count_changed, nothing done). One audit record for the batch: the filter and the counts."""
    picked = select_ids(db, f)
    if expect is not None and expect != len(picked):
        error = ViewError(f"the filter picks {len(picked)} submission(s) now, not {expect}: check again",
                          "count_changed", 409)
        error.affected = len(picked)
        raise error
    result = {"affected": len(picked), "contest_problems": sorted({cp for _s, cp in picked}),
              "created": 0, "retargeted": 0, "already_running": 0}
    for submission_id, _cp in picked:
        result[q.request_rejudge(db, submission_id, requested_by=actor.user_id).kind] += 1
    audit.record(db, actor, "submissions.rejudge", now, object_type="submissions",
                 details={"filter": f.as_dict(), **result})
    return result


def one(db: Session, actor: audit.Actor, submission_id: int, now: datetime) -> dict:
    if db.get(Submission, submission_id) is None:
        raise ViewError(f"no submission {submission_id}", "submission_not_found", 404)
    outcome = q.request_rejudge(db, submission_id, requested_by=actor.user_id)
    audit.record(db, actor, "submission.rejudge", now, object_type="submission", object_id=submission_id,
                 details={"kind": outcome.kind, "judgement": outcome.judgement.id})
    return {"submission": submission_id, "kind": outcome.kind, "judgement": outcome.judgement.id}
