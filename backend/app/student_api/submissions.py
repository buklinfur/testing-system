"""/api/submissions of a student: their own list, the view of one (with the poll interval while it is judged) and its
source (docs/STUDENT_API.md §6). Another student's submission is 404 submission_not_found, as one that is not there."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import queue
from ..auth_api import Current, get_db, now
from ..ids import Id, Limit
from ..jsonable import plain
from ..models import Judgement, JudgementStatus
from ..student import submissions as sv
from ..student.common import StudentError
from .deps import blobs, refused, student_session

router = APIRouter()

POLL_MIN_S, POLL_MAX_S = 2, 10


def poll_after_s(status: Optional[JudgementStatus], position: Optional[int], online: int) -> Optional[int]:
    """Seconds until the client should ask again: None when nothing is being judged; 2 while it is judged; in the
    queue 2 more for every full round of the workers ahead of it (place 1..online: 2 s), at most 10; no worker
    online: 10 at once."""
    if status is None:
        return None
    if status != JudgementStatus.queued:
        return POLL_MIN_S
    if online <= 0:
        return POLL_MAX_S
    if position is None:
        return POLL_MIN_S
    return min(POLL_MAX_S, POLL_MIN_S + 2 * ((position - 1) // online))


@router.get("/submissions")
def list_submissions(request: Request, contest: Optional[str] = Query(None, max_length=64),
                     label: Optional[str] = Query(None, max_length=8), cursor: Optional[Id] = None,
                     limit: Limit = 50, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The student's own submissions, newest first (next_cursor: pass it as cursor). contest: one contest (one the
    student sees, also only for their results); label needs contest (422 label_needs_contest)."""
    try:
        return plain(sv.my_submissions(db, cur.user, now(request), contest=contest, label=label, cursor=cursor,
                                       limit=limit))
    except StudentError as exc:
        raise refused(exc) from exc


@router.get("/submissions/{submission_id}")
def get_submission(submission_id: Id, request: Request, db: Session = Depends(get_db),
                   cur: Current = Depends(student_session)):
    """The student's view of one of their submissions: status queued | judging | done, the verdict and what the
    contest's policy shows now; rechecking while a rejudge runs; poll_after_s while something is judged."""
    at = now(request)
    try:
        out = sv.my_submission(db, cur.user, blobs(request), submission_id, at)
    except StudentError as exc:
        raise refused(exc) from exc
    active = db.execute(select(Judgement.id, Judgement.status).where(
        Judgement.submission_id == submission_id, Judgement.status.in_(sv.ACTIVE))
        .order_by(Judgement.id.desc()).limit(1)).first()
    position, online = None, 0
    if active is not None and active.status == JudgementStatus.queued:
        position, online = queue.poll_inputs(db, request.app.state.settings.queue, active.id, at)
    poll = poll_after_s(active.status if active else None, position, online)
    if poll is not None:  # nothing judged: no poll_after_s at all
        out["poll_after_s"] = poll
    return plain(out)


@router.get("/submissions/{submission_id}/source")
def get_source(submission_id: Id, request: Request, db: Session = Depends(get_db),
               cur: Current = Depends(student_session)):
    """The source of one's own submission: a single file as text, an archive as a zip. 404 file_not_found for a
    submission of answers."""
    try:
        return sv.my_source(db, cur.user, blobs(request), submission_id, request=request)
    except StudentError as exc:
        raise refused(exc) from exc
