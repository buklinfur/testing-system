"""/api/contests of a student: the list, the page of a contest, entering (with a code), the personal start and the
leaderboard (docs/STUDENT_API.md §6). The services are app/student/contests.py and standings.py."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.orm import Session

from ..auth_api import Current, client_ip, get_db, now
from ..ids import Id
from ..jsonable import plain
from ..student import contests as sc
from ..student import standings as ss
from ..student.common import StudentError
from .deps import refused, student_session
from .types import Slug

router = APIRouter()


class EnterBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[StrictStr] = Field(None, max_length=64, description="the invite code of the contest, if it has one")


@router.get("/contests")
def list_contests(request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """Contests the student sees (mode full) or took part in (mode own_results), newest start first."""
    return plain({"items": sc.my_contests(db, cur.user, now(request)), "server_time": now(request)})


@router.get("/contests/{slug}")
def get_contest(slug: Slug, request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The page of a contest with server_time; before the student's time begins the problems are not named (an empty
    list and problem_count). 404 contest_not_found: a draft, a contest of other groups, no such contest."""
    try:
        return plain(sc.contest_view(db, cur.user, slug, now(request)))
    except StudentError as exc:
        raise refused(exc) from exc


@router.post("/contests/{slug}/enter")
def enter_contest(slug: Slug, request: Request, body: Optional[EnterBody] = None, db: Session = Depends(get_db),
                  cur: Current = Depends(student_session)):
    """Enter a contest ({code} for a contest with an invite code). Every attempt is recorded, right or wrong; at most
    JUDGE_ENTER_RATE in 10 minutes (429 rate_limited with Retry-After, not recorded). The contest page with
    `entered`: ok | rejoin_with_code."""
    at = now(request)
    try:
        outcome = sc.enter(db, cur.user, slug, at, request.app.state.settings.submit, body.code if body else None,
                           ip=client_ip(request))
    except StudentError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()  # the attempt counts whatever its outcome
    if not outcome.ok:
        raise refused(StudentError(outcome.code))
    page = sc.contest_view(db, cur.user, slug, at)
    page["entered"] = outcome.code
    return plain(page)


@router.post("/contests/{slug}/start")
def start_contest(slug: Slug, request: Request, db: Session = Depends(get_db), cur: Current = Depends(student_session)):
    """The personal start («Начать») of a contest with timing personal: once, with access. The contest page."""
    at = now(request)
    try:
        sc.start(db, cur.user, slug, at)
    except StudentError as exc:
        db.rollback()
        raise refused(exc) from exc
    db.commit()
    return plain(sc.contest_view(db, cur.user, slug, at))


@router.get("/contests/{slug}/standings")
def get_standings(slug: Slug, request: Request, group: Optional[Id] = None, db: Session = Depends(get_db),
                  cur: Current = Depends(student_session)):
    """The leaderboard as the student may see it now (frozen per the contest), optionally only the current members of
    one of the contest's groups. 403 standings_closed; 404 group_not_found (not a group of this contest)."""
    try:
        return plain(ss.standings(db, cur.user, slug, now(request), group))
    except StudentError as exc:
        raise refused(exc) from exc
