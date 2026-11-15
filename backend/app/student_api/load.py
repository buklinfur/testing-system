"""GET /api/load: the load indicator for any fully logged-in user (a student, and an admin too): queue length, busy
workers, expected wait, and for a student their own best place in the queue. One moment of the database (D-067,
docs/QUEUE.md «Снимок на один момент»)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import queue
from ..auth_api import Current, current_session, get_db
from ..jsonable import plain
from ..models import Role

router = APIRouter()


@router.get("/load")
def get_load(request: Request, db: Session = Depends(get_db), cur: Current = Depends(current_session)):
    """{level: free | busy | overloaded | offline, queue_length, workers_online, workers_busy, typical_seconds,
    my_position, my_wait_seconds} (my_* only for a student with something queued)."""
    me = cur.user.id if cur.user.role == Role.student else None
    db.commit()  # end the transaction the session check read in: the snapshot starts its own (read_one_moment)
    at = queue.read_one_moment(db)
    out = queue.load_snapshot(db, request.app.state.settings.queue, user_id=me).to_dict()
    db.rollback()
    out["server_time"] = at
    return plain(out)
