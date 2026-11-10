"""Students' work on manual assignments: hand-ins, grades, returns, releases, and what is seen of them (D-059,
docs/CLASSROOM.md §4, §5, §7).

Locking. Every change of a work - a hand-in, a grade, a return, a release - locks its row of assignment_works
(SELECT ... FOR UPDATE) and decides on the status read under that lock, so concurrent changes go one after
another. The first change of a pair (item, student) creates the row with INSERT ... ON CONFLICT (item_id, user_id)
DO NOTHING and then locks it: two first hand-ins at once make one work. A release of many works locks them in the
order of their ids (no deadlock between two releases).

The status of a work is a cache of its history, written in the same transaction as the event (grading.next_status
applied to the event). `now` is the moment the request was received, taken once by the caller before the body is
read (D-041); a hand-in keeps created_at = now and late as decided then, never recomputed.

Identity. The student is always a User row - the session's in 4b.2, the command line's in judgectl - and a work is
found by (item, student); no function takes a work id from outside.

Functions flush but never commit; a refusal raises FeedError and the caller rolls back.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import false, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..auth import audit
from ..blobstore import BlobStore
from ..models import (AssignmentReview, AssignmentSubmission, AssignmentSubmissionFile, AssignmentWork, FeedItem,
                      FeedItemAudience, FeedItemKind, FeedItemState, Group, GroupMember, ReviewAction, Role,
                      SubmissionFileKind, User, WorkStatus)
from ..problems.importer import store_blob
from . import access, grading
from . import items as feed
from .items import FeedError

ANSWER_FILE = "answer.md"
COMMENT_MAX = 2000
# the order of the events of a work: by time, a hand-in before a review at the same moment, then by id
SUBMISSION_RANK, REVIEW_RANK = 0, 1


def _v(value):
    return getattr(value, "value", value)


# ----------------------------------------------------------------------------- the row of a work
def work_of(db: Session, item: FeedItem, user: User) -> Optional[AssignmentWork]:
    return db.execute(select(AssignmentWork).where(AssignmentWork.item_id == item.id,
                                                   AssignmentWork.user_id == user.id)).scalar_one_or_none()


def _lock(db: Session, item: FeedItem, user: User) -> Optional[AssignmentWork]:
    return db.execute(select(AssignmentWork).where(AssignmentWork.item_id == item.id,
                                                   AssignmentWork.user_id == user.id)
                      .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()


def _lock_or_create(db: Session, item: FeedItem, user: User, now: datetime) -> tuple[AssignmentWork, Optional[str]]:
    """(the locked row, its status before this event; None when this call created it). The provisional status of a
    new row is overwritten by the caller in the same transaction."""
    row = _lock(db, item, user)
    if row is not None:
        return row, row.status.value
    created = db.execute(pg_insert(AssignmentWork)
                         .values(item_id=item.id, user_id=user.id, status=WorkStatus.submitted, status_at=now,
                                 created_at=now)
                         .on_conflict_do_nothing(index_elements=["item_id", "user_id"])
                         .returning(AssignmentWork.id)).scalar_one_or_none()
    row = _lock(db, item, user)
    return row, None if created is not None else row.status.value


def _set_status(work: AssignmentWork, prior: Optional[str], event: str, now: datetime) -> None:
    t = grading.next_status(prior, event)
    if not t:
        raise FeedError(t.reason, t.reason)
    if work.status != WorkStatus(t.status):
        work.status = WorkStatus(t.status)
        work.status_at = now
    elif event != "release":
        work.status_at = now


def _assignment(item: FeedItem) -> None:
    if item.kind != FeedItemKind.assignment:
        raise FeedError(f"item {item.id} is not an assignment", "not_an_assignment")


def _student(user: User) -> None:
    if user.role != Role.student:
        raise FeedError(f"{user.login} is not a student", "not_a_student")


def _comment(text: Optional[str]) -> Optional[str]:
    text = (text or "").strip()
    if len(text) > COMMENT_MAX:
        raise FeedError(f"a comment is at most {COMMENT_MAX} characters", "comment_too_long")
    return text or None


# ----------------------------------------------------------------------------- a hand-in (the student)
def submit_work(db: Session, user: User, item: FeedItem, now: datetime, *, blobs: BlobStore,
                text: Optional[str] = None, text_limit: int = access.ANSWER_TEXT_MAX_BYTES,
                ip: Optional[str] = None) -> tuple[AssignmentSubmission, object]:
    """A hand-in of the student of the session, received at `now`. The rules (classroom/access.can_submit_work)
    are checked before anything is written - the text limit before the blob is stored - and again on the status
    read under the lock. Returns (the hand-in, the decision: late or not)."""
    in_audience = feed.in_audience(db, item, user)
    extensions = feed.due_moments(db, item, user)
    current = work_of(db, item, user)

    def decide(status):
        d = access.can_submit_work(user, item, now, in_audience=in_audience, extensions=extensions,
                                   work_status=status, text=text, text_limit=text_limit)
        if not d:
            raise FeedError(d.reason, d.reason)
        return d

    decide(_v(current.status) if current is not None else None)
    work, prior = _lock_or_create(db, item, user, now)
    decision = decide(prior)
    sub = AssignmentSubmission(work_id=work.id, created_at=now, late=decision.late, ip=ip)
    db.add(sub)
    db.flush()
    if text is not None and text.strip():
        data = text.encode("utf-8")
        sha = store_blob(db, blobs, data)
        db.flush()  # the blobs row first: no relationship tells the unit of work the order
        db.add(AssignmentSubmissionFile(submission_id=sub.id, kind=SubmissionFileKind.text, path=ANSWER_FILE,
                                        blob_sha256=sha, size=len(data), position=0))
    _set_status(work, prior, "submit", now)
    db.flush()
    return sub, decision


# ----------------------------------------------------------------------------- the teacher
def grade_work(db: Session, actor: audit.Actor, item: FeedItem, student: User, now: datetime, *,
               score: Optional[Decimal] = None, passed: Optional[bool] = None, comment: Optional[str] = None,
               release: bool = False) -> dict:
    """A grade on the scale of the assignment (grading.check_grade), with or without a hand-in. `score`: a string, a
    JSON number or a Decimal (grading.parse_points; anything else is score_invalid). With `release` and
    grades_release = manual the grade is released in the same transaction (both rows get the same `at`; the order
    of events keeps the release after it). With grading = none or immediate nothing is held back."""
    _assignment(item)
    _student(student)
    if item.state == FeedItemState.draft:
        raise FeedError("the item is a draft", "not_published")
    comment = _comment(comment)
    if score is not None:
        parsed = grading.parse_points(score)
        if parsed is None:
            raise FeedError(f"score {score!r}: a number like 8 or 4.5 (at most two decimals, no exponent)",
                            "score_invalid")
        score = parsed
    check = grading.check_grade(item, score, passed)
    if not check:
        raise FeedError(check.reason, check.reason)
    if work_of(db, item, student) is None and not feed.in_audience(db, item, student):
        raise FeedError(f"{student.login} is not addressed by item {item.id} and has no work in it", "not_addressed")
    work, prior = _lock_or_create(db, item, student, now)
    _set_status(work, prior, "grade", now)
    review = AssignmentReview(work_id=work.id, action=ReviewAction.graded, score=score, passed=passed,
                              comment=comment, reviewer_id=actor.user_id, at=now)
    db.add(review)
    db.flush()
    released = False
    if release and not grading.seen_at_once(item):
        db.add(AssignmentReview(work_id=work.id, action=ReviewAction.released, release_of=review.id,
                                reviewer_id=actor.user_id, at=now))
        released = True
    db.flush()
    audit.record(db, actor, "work.grade", now, object_type="assignment_work", object_id=work.id,
                 details={"item": item.id, "login": student.login, "score": None if score is None else str(score),
                          "passed": passed, "comment": comment, "released": released, "from": prior})
    return {"work_id": work.id, "review_id": review.id, "status": work.status.value, "released": released,
            "seen_at_once": grading.seen_at_once(item)}


def return_work(db: Session, actor: audit.Actor, item: FeedItem, student: User, now: datetime,
                comment: str) -> dict:
    """Back for rework, with a comment the student sees at once; they may hand in again."""
    _assignment(item)
    _student(student)
    comment = _comment(comment)
    if comment is None:
        raise FeedError("a return needs a comment: what to rework", "comment_required")
    work = _lock(db, item, student)
    if work is None:
        raise FeedError("nothing_to_return", "nothing_to_return")
    prior = work.status.value
    _set_status(work, prior, "return", now)
    review = AssignmentReview(work_id=work.id, action=ReviewAction.returned, comment=comment,
                              reviewer_id=actor.user_id, at=now)
    db.add(review)
    db.flush()
    audit.record(db, actor, "work.return", now, object_type="assignment_work", object_id=work.id,
                 details={"item": item.id, "login": student.login, "comment": comment, "from": prior})
    return {"work_id": work.id, "review_id": review.id, "status": work.status.value}


def release_grades(db: Session, actor: audit.Actor, item: FeedItem, now: datetime,
                   students: Optional[Iterable[User]] = None) -> list[dict]:
    """Release the current grade of every work of the item (or of these students): one transaction, works locked in
    the order of their ids. A work that does not fit (no work, not graded, already released) is skipped with its
    reason, it does not stop the others. Returns [{user_id, login, result}] - result "released" or the reason."""
    _assignment(item)
    if grading.seen_at_once(item):
        raise FeedError("grades of this item are seen at once (grades_release immediate or grading none)",
                        "release_not_needed")
    query = (select(AssignmentWork, User).join(User, User.id == AssignmentWork.user_id)
             .where(AssignmentWork.item_id == item.id))
    wanted = None
    if students is not None:
        wanted = {u.id: u for u in students}
        query = query.where(AssignmentWork.user_id.in_(list(wanted) or [-1]))
    rows = db.execute(query.order_by(AssignmentWork.id).with_for_update(of=AssignmentWork)
                      .execution_options(populate_existing=True)).all()
    results, released = [], []
    for work, user in rows:
        t = grading.next_status(work.status.value, "release")
        reviews = list(db.execute(select(AssignmentReview).where(AssignmentReview.work_id == work.id)
                                  .order_by(AssignmentReview.id)).scalars())
        target, code = grading.release_target(item, reviews) if t else (None, t.reason)
        if target is None:
            results.append({"user_id": user.id, "login": user.login, "result": code})
            continue
        db.add(AssignmentReview(work_id=work.id, action=ReviewAction.released, release_of=target.id,
                                reviewer_id=actor.user_id, at=now))
        results.append({"user_id": user.id, "login": user.login, "result": "released"})
        released.append(user.login)
    if wanted is not None:
        have = {user.id for _w, user in rows}
        results += [{"user_id": u.id, "login": u.login, "result": "nothing_to_release"}
                    for uid, u in wanted.items() if uid not in have]
    db.flush()
    if released:
        audit.record(db, actor, "work.release", now, object_type="feed_item", object_id=item.id,
                     details={"item": item.id, "released": released,
                              "skipped": {r["login"]: r["result"] for r in results if r["result"] != "released"}})
    return sorted(results, key=lambda r: r["login"])


# ----------------------------------------------------------------------------- what is seen
def events(db: Session, work: AssignmentWork) -> list[tuple]:
    """The hand-ins and reviews of a work in the order they happened: one query ordered by (at, kind, id), a
    hand-in before a review at the same moment, rows of one kind by id (a grade and its release in one transaction
    share `at`). Pairs ("submission", row) / ("review", row), as grading.student_view takes them."""
    subs = select(literal("submission").label("kind"), literal(SUBMISSION_RANK).label("rank"),
                  AssignmentSubmission.id.label("id"), AssignmentSubmission.created_at.label("at")) \
        .where(AssignmentSubmission.work_id == work.id)
    revs = select(literal("review").label("kind"), literal(REVIEW_RANK).label("rank"), AssignmentReview.id.label("id"),
                  AssignmentReview.at.label("at")).where(AssignmentReview.work_id == work.id)
    both = union_all(subs, revs).subquery()
    order = db.execute(select(both.c.kind, both.c.id).order_by(both.c.at, both.c.rank, both.c.id)).all()
    by_kind = {
        "submission": {r.id: r for r in db.execute(select(AssignmentSubmission)
                                                   .where(AssignmentSubmission.work_id == work.id)).scalars()},
        "review": {r.id: r for r in db.execute(select(AssignmentReview)
                                               .where(AssignmentReview.work_id == work.id)).scalars()},
    }
    return [(kind, by_kind[kind][row_id]) for kind, row_id in order]


def student_work(db: Session, user: User, item: FeedItem, now: datetime) -> Optional[dict]:
    """What the student of the session sees of their own work on the item (grading.student_view), or None when
    there is nothing for them: no work and the item not in their feed, or a draft."""
    work = work_of(db, item, user)
    if work is None:
        if not access.item_listed(user, item, now, feed.in_audience(db, item, user)) \
                or item.kind != FeedItemKind.assignment:
            return None
        found = []
    elif not access.can_see_own_work(user, item, True):
        return None
    else:
        found = events(db, work)
    return grading.student_view(item, now, found, feed.due_moments(db, item, user))


def answer_text(db: Session, blobs: BlobStore, submission: AssignmentSubmission) -> Optional[str]:
    """The text answer of a hand-in (answer.md), as a string (bytes that are not UTF-8 replaced)."""
    row = db.execute(select(AssignmentSubmissionFile).where(AssignmentSubmissionFile.submission_id == submission.id,
                                                            AssignmentSubmissionFile.path == ANSWER_FILE)
                     ).scalar_one_or_none()
    if row is None:
        return None
    data = blobs.get(row.blob_sha256)
    return None if data is None else data.decode("utf-8", errors="replace")


def work_detail(db: Session, blobs: BlobStore, item: FeedItem, student: User, now: datetime) -> Optional[dict]:
    """The teacher's view of one work: every hand-in with its text, every review (unreleased grades too), the
    cached status and what the student sees. None: no work."""
    work = work_of(db, item, student)
    if work is None:
        return None
    found = events(db, work)
    out_events = []
    for kind, row in found:
        if kind == "submission":
            out_events.append({"kind": "submission", "id": row.id, "at": row.created_at, "late": row.late,
                               "text": answer_text(db, blobs, row)})
        else:
            out_events.append({"kind": "review", "id": row.id, "at": row.at, "action": row.action.value,
                               "score": row.score, "passed": row.passed, "comment": row.comment,
                               "reviewer_id": row.reviewer_id, "release_of": row.release_of})
    return {"work_id": work.id, "item": item.id, "user_id": student.id, "login": student.login,
            "status": work.status.value, "status_at": work.status_at, "events": out_events,
            "student_sees": grading.student_view(item, now, found, feed.due_moments(db, item, student))}


def work_card(db: Session, blobs: BlobStore, item: FeedItem, student: User, now: datetime) -> Optional[dict]:
    """The teacher's card of a pair (item, student): work_detail when the work exists; for a student addressed now
    (a group they are in, or personally) without work - status assigned, no events, what they would see; None when
    the student has no work and is not addressed (a work of the same student on another item included). `missing`
    is computed in both cases."""
    out = work_detail(db, blobs, item, student, now)
    if out is None:
        if item.kind != FeedItemKind.assignment or not feed.in_audience(db, item, student):
            return None
        out = {"work_id": None, "item": item.id, "user_id": student.id, "login": student.login,
               "status": ASSIGNED, "status_at": None, "events": [],
               "student_sees": grading.student_view(item, now, [], feed.due_moments(db, item, student))}
    out["display_name"] = student.display_name
    out["missing"] = out["student_sees"]["missing"]
    return out


# ----------------------------------------------------------------------------- the review queue
ASSIGNED = "assigned"  # no work yet (not stored): a status of the queue only
QUEUE_STATUSES = (ASSIGNED,) + tuple(s.value for s in WorkStatus)


def _member_ids(db: Session, group: Group) -> set:
    """Current members of the group (D-043), archived or not: the filter `group` of the queue."""
    return set(db.execute(select(GroupMember.user_id).where(GroupMember.group_id == group.id,
                                                            GroupMember.removed_at.is_(None))).scalars())


def addressee_ids(db: Session, item: FeedItem) -> set:
    """The students addressed by the item now: personally, or through a group not archived they are in now."""
    personal = select(FeedItemAudience.user_id).where(FeedItemAudience.item_id == item.id,
                                                      FeedItemAudience.user_id.is_not(None))
    through_groups = (select(GroupMember.user_id)
                      .join(FeedItemAudience, FeedItemAudience.group_id == GroupMember.group_id)
                      .join(Group, Group.id == GroupMember.group_id)
                      .where(FeedItemAudience.item_id == item.id, GroupMember.removed_at.is_(None),
                             Group.archived_at.is_(None)))
    return set(db.execute(personal).scalars()) | set(db.execute(through_groups).scalars())


def _last_late():
    """The `late` of the latest hand-in of a work (null: no hand-in), as a correlated subquery."""
    return (select(AssignmentSubmission.late).where(AssignmentSubmission.work_id == AssignmentWork.id)
            .order_by(AssignmentSubmission.created_at.desc(), AssignmentSubmission.id.desc()).limit(1)
            .scalar_subquery())


def queue_row(db: Session, item: FeedItem, user: User, work: Optional[AssignmentWork], now: datetime) -> dict:
    """One line of the review queue: the status (assigned without work), the flags late and missing, the number of
    hand-ins and the current grade for the teacher (the last graded row; `released`: the student sees it)."""
    found = events(db, work) if work is not None else []
    submissions = [row for kind, row in found if kind == "submission"]
    reviews = [row for kind, row in found if kind == "review"]
    current = next((r for r in reversed(reviews) if r.action == ReviewAction.graded), None)
    seen = grading.visible_grade(item, reviews)
    return {"item": item.id, "item_title": item.title, "user_id": user.id, "login": user.login,
            "display_name": user.display_name, "work_id": None if work is None else work.id,
            "status": ASSIGNED if work is None else work.status.value,
            "status_at": None if work is None else work.status_at,
            "submissions": len(submissions), "last_submitted_at": submissions[-1].created_at if submissions else None,
            "late": bool(submissions) and bool(submissions[-1].late),
            "missing": access.missing(item, now, bool(submissions), feed.due_moments(db, item, user)),
            "grade": None if current is None else {"score": current.score, "passed": current.passed,
                                                   "released": seen is not None and seen.id == current.id}}


def review_queue(db: Session, now: datetime, *, item: Optional[FeedItem] = None, group: Optional[Group] = None,
                 status: Optional[str] = None, late: Optional[bool] = None, missing: Optional[bool] = None,
                 cursor: Optional[int] = None, limit: int = 50) -> dict:
    """The review queue, newest first, a page of `limit` rows: {"items": [queue_row], "next_cursor"}.

    * With `item`: every student addressed now and everyone with work on it, a row each (status assigned without
      work), the cursor is the user id. `missing` («не сдано») is computed per student with their extensions.
    * Without `item`: only works that exist, on every assignment, the cursor is the work id; `missing` needs an
      item (missing_needs_item: a student without work has no row here).
    `group`: current members of the group (D-043); `status`: one of QUEUE_STATUSES; `late`: of the latest hand-in."""
    if status is not None and status not in QUEUE_STATUSES:
        raise FeedError(f"status is one of {', '.join(QUEUE_STATUSES)}", "item_invalid")
    members = _member_ids(db, group) if group is not None else None
    if item is not None:
        _assignment(item)
        have = {w.user_id: w for w in db.execute(select(AssignmentWork).where(AssignmentWork.item_id == item.id))
                .scalars()}
        ids = addressee_ids(db, item) | set(have)
        if members is not None:
            ids &= members
        if cursor is not None:
            ids = {i for i in ids if i < cursor}
        users = {u.id: u for u in db.execute(select(User).where(User.id.in_(list(ids) or [-1]))).scalars()}
        rows = []
        for uid in sorted(ids, reverse=True):
            row = queue_row(db, item, users[uid], have.get(uid), now)
            if (status is None or row["status"] == status) and (late is None or row["late"] == late) \
                    and (missing is None or row["missing"] == missing):
                rows.append(row)
                if len(rows) > limit:
                    break
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": rows, "next_cursor": rows[-1]["user_id"] if more else None}
    if missing is not None:
        raise FeedError("missing («не сдано») is computed for one item: give the item", "missing_needs_item")
    query = (select(AssignmentWork, User, FeedItem).join(User, User.id == AssignmentWork.user_id)
             .join(FeedItem, FeedItem.id == AssignmentWork.item_id))
    if status == ASSIGNED:
        query = query.where(false())  # a work that exists is never «assigned»
    elif status is not None:
        query = query.where(AssignmentWork.status == WorkStatus(status))
    if late is not None:
        query = query.where(func.coalesce(_last_late(), false()).is_(late))
    if members is not None:
        query = query.where(AssignmentWork.user_id.in_(list(members) or [-1]))
    if cursor is not None:
        query = query.where(AssignmentWork.id < cursor)
    found = db.execute(query.order_by(AssignmentWork.id.desc()).limit(limit + 1)).all()
    more = len(found) > limit
    found = found[:limit]
    return {"items": [queue_row(db, it, user, work, now) for work, user, it in found],
            "next_cursor": found[-1][0].id if more and found else None}
