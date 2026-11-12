"""Comment threads of the class feed: write, delete, read, read marks, the teachers' inbox (D-072, D-074, D-075,
docs/CLASSROOM.md §9).

Who may do what is classroom/comment_rules.py; these functions collect from the database what the rules need,
carry the decision out and flush, never commit (the caller owns the transaction: judgectl one per command, the admin
API and 4b.2 one per request). A refusal raises FeedError with a stable code; the caller rolls back.

Identity. A student is always a User row - the session's in 4b.2, the command line's in dev_seed - and acts only in
their own private thread: student functions take no student id. A teacher writes as an audit.Actor (an admin of the
web, or the console: author null, the console user's name in the audit log). Readers are a User (student or admin)
or CONSOLE, which reads like a teacher and has no read marks.

Threads come into being only by a write: the first comment of a thread inserts the row with INSERT ... ON CONFLICT
DO NOTHING on the partial unique index and reads it back, so two first comments at once make one thread. Reading,
read marks and counters never insert a thread.

Rate. A student's comment takes the advisory lock (locks.COMMENT_RATE, user id) before anything is read, then
counts their comments of the last RATE_WINDOW_S seconds (deleted ones too) and writes the new one with
created_at = the same `now`: the window and the row are in the same clock, and concurrent comments of one student
are counted one after another. Teachers have no rate and take no lock.

What a student sees of a comment: the display name of the author and whether a teacher wrote it (the console:
TEACHER_NAME), never a login; deleted comments do not exist for them. Teachers see everything, deleted comments with
who deleted them and why. Unread = comments of others, not deleted, after the reader's mark; the mark only grows.
«Awaiting reply» = the last comment not deleted was written by a student.

Audit: comment.post and comment.delete of teachers, without the text. Students' comments are not audited (like
hand-ins); a student deleting their own comment is recorded in the row (deleted_by).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, aliased

from .. import locks
from ..auth import audit
from ..models import (AssignmentWork, Comment, CommentThread, FeedItem, FeedItemAudience, FeedItemKind, Group,
                      GroupMember, Role, ThreadRead, User)
from . import access
from . import comment_rules as rules
from . import items as feed
from .items import FeedError

TEACHER_NAME = "Преподаватель"  # what students see as the author of a comment written from the console
PAGE_MAX = 200
REASON_MAX = 200  # comments.delete_reason


class _Console:
    """The reader of judgectl: a teacher without a user row and without read marks."""

    id = None
    role = Role.admin
    login = None
    display_name = TEACHER_NAME


CONSOLE = _Console()


def _v(value):
    return getattr(value, "value", value)


def _raise(decision) -> None:
    if not decision:
        raise FeedError(decision.reason, decision.reason)


def _student(user: User) -> None:
    if _v(user.role) != "student":
        raise FeedError(f"{user.login} is not a student: teachers write with teacher_post", "not_a_student")


def _teacher(db: Session, actor: audit.Actor):
    """The reader the rules see for an actor: CONSOLE, or the admin's User row."""
    if actor.user_id is None:
        return CONSOLE
    user = db.get(User, actor.user_id)
    if user is None or user.role != Role.admin:
        raise ValueError(f"actor {actor.label} is not an admin")
    return user


def _page(limit: int) -> int:
    return max(1, min(int(limit), PAGE_MAX))


# ----------------------------------------------------------------------------- threads and what the rules need
def _thread(db: Session, item: FeedItem, student_id: Optional[int]) -> Optional[CommentThread]:
    who = CommentThread.student_id.is_(None) if student_id is None else CommentThread.student_id == student_id
    return db.execute(select(CommentThread).where(CommentThread.item_id == item.id, who)).scalar_one_or_none()


def _thread_for_write(db: Session, item: FeedItem, student_id: Optional[int], now: datetime) -> CommentThread:
    """The thread, created by this first comment if it does not exist yet (one row even under concurrency)."""
    found = _thread(db, item, student_id)
    if found is not None:
        return found
    insert = pg_insert(CommentThread).values(item_id=item.id, student_id=student_id, created_at=now)
    if student_id is None:
        insert = insert.on_conflict_do_nothing(index_elements=["item_id"],
                                               index_where=CommentThread.student_id.is_(None))
    else:
        insert = insert.on_conflict_do_nothing(index_elements=["item_id", "student_id"],
                                               index_where=CommentThread.student_id.is_not(None))
    db.execute(insert)
    return _thread(db, item, student_id)


def _shared_inputs(db: Session, item: FeedItem, user: User) -> dict:
    """What comment_rules needs about a student and the shared thread of the item."""
    return feed.view_inputs(db, item, user)


def _has_work(db: Session, item: FeedItem, student: User) -> bool:
    return db.execute(select(AssignmentWork.id).where(AssignmentWork.item_id == item.id,
                                                      AssignmentWork.user_id == student.id)).first() is not None


def _can_read(db: Session, viewer, item: FeedItem, student: Optional[User], now: datetime,
              thread: Optional[CommentThread]) -> bool:
    """Whether `viewer` reads the thread of (item, student) now; student None: the shared thread. A teacher reads a
    private thread only of a student who is addressed now or has work or a thread there (as grade_work)."""
    if student is None:
        if access.is_admin(viewer):
            return True
        return rules.can_read_shared(viewer, item, now, **_shared_inputs(db, item, viewer))
    if access.is_admin(viewer):
        return (_v(student.role) == "student" and item.kind == FeedItemKind.assignment
                and (thread is not None or _has_work(db, item, student) or feed.in_audience(db, item, student)))
    if viewer.id != student.id:
        return False  # never even a query about another student's thread
    return rules.can_read_private(viewer, item, student, now, in_audience=feed.in_audience(db, item, viewer),
                                  thread_exists=thread is not None)


def _readable_thread(db: Session, viewer, item: FeedItem, student: Optional[User],
                     now: datetime) -> Optional[CommentThread]:
    """The thread (None: no comments yet) of a pair the viewer may read; thread_not_found otherwise."""
    thread = _thread(db, item, None if student is None else student.id)
    if not _can_read(db, viewer, item, student, now, thread):
        raise FeedError(f"no comment thread of item {item.id} here", "thread_not_found")
    return thread


# ----------------------------------------------------------------------------- writing
def _recent(db: Session, user: User, now: datetime) -> int:
    """The student's comments of the last RATE_WINDOW_S seconds, deleted ones included."""
    return db.execute(select(func.count()).select_from(Comment).where(
        Comment.author_id == user.id,
        Comment.created_at > now - timedelta(seconds=rules.RATE_WINDOW_S))).scalar_one()


def _add(db: Session, thread: CommentThread, author_id: Optional[int], body: str, now: datetime) -> Comment:
    comment = Comment(thread_id=thread.id, author_id=author_id, body=body, created_at=now)
    db.add(comment)
    db.flush()
    return comment


def post_shared(db: Session, user: User, item: FeedItem, now: datetime, body: Optional[str], *,
                limit: Optional[int] = rules.RATE_PER_WINDOW) -> Comment:
    """A student's comment in the shared thread of the item (comment_rules.can_post_shared). `limit`: the setting
    JUDGE_COMMENT_RATE_PER_MIN (0: none)."""
    _student(user)
    locks.take_pair(db, locks.COMMENT_RATE, user.id)
    _raise(rules.can_post_shared(user, item, now, body=body, recent=_recent(db, user, now), limit=limit,
                                 **_shared_inputs(db, item, user)))
    return _add(db, _thread_for_write(db, item, None, now), user.id, body, now)


def post_private(db: Session, user: User, item: FeedItem, now: datetime, body: Optional[str], *,
                 limit: Optional[int] = rules.RATE_PER_WINDOW) -> Comment:
    """A student's comment in their own private thread of an assignment (comment_rules.can_post_private)."""
    _student(user)
    locks.take_pair(db, locks.COMMENT_RATE, user.id)
    _raise(rules.can_post_private(user, item, user, now, in_audience=feed.in_audience(db, item, user), body=body,
                                  thread_exists=_thread(db, item, user.id) is not None,
                                  recent=_recent(db, user, now), limit=limit))
    return _add(db, _thread_for_write(db, item, user.id, now), user.id, body, now)


def teacher_post(db: Session, actor: audit.Actor, item: FeedItem, now: datetime, body: Optional[str], *,
                 student: Optional[User] = None) -> Comment:
    """A teacher's comment: in the shared thread (student None), or in the private thread of `student` (a reply,
    or the first message to them). No rate. Audited as comment.post, without the text."""
    teacher = _teacher(db, actor)
    if student is None:
        _raise(rules.can_post_shared(teacher, item, now, in_audience=False, body=body))
    else:
        thread = _thread(db, item, student.id)
        _raise(rules.can_post_private(teacher, item, student, now, body=body,
                                      in_audience=feed.in_audience(db, item, student),
                                      has_work=_has_work(db, item, student), thread_exists=thread is not None))
    thread = _thread_for_write(db, item, None if student is None else student.id, now)
    comment = _add(db, thread, actor.user_id, body, now)
    audit.record(db, actor, "comment.post", now, object_type="comment", object_id=comment.id,
                 details={"item": item.id, "thread": "shared" if student is None else "private",
                          "student": None if student is None else student.login, "length": len(body)})
    return comment


# ----------------------------------------------------------------------------- deleting
def _locked_comment(db: Session, comment_id: int) -> Optional[tuple[Comment, CommentThread, FeedItem]]:
    row = db.execute(select(Comment).where(Comment.id == comment_id).with_for_update()
                     .execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        return None
    thread = db.get(CommentThread, row.thread_id)
    return row, thread, db.get(FeedItem, thread.item_id)


def delete_own(db: Session, user: User, comment_id: int, now: datetime) -> Comment:
    """A student deletes their own comment (soft: teachers still see the text). A comment the student does not see
    - another student's private thread, an item outside their feed, deleted already, missing - is
    comment_not_found, the same answer for all of them; a visible comment of someone else is not_your_comment.
    Not audited: deleted_by in the row says who."""
    _student(user)
    found = _locked_comment(db, comment_id)
    if found is not None:
        comment, thread, item = found
        student = None if thread.student_id is None else db.get(User, thread.student_id)
        if rules.visible_to(user, comment) and _can_read(db, user, item, student, now, thread):
            _raise(rules.can_delete(user, comment, can_read=True))
            comment.deleted_at, comment.deleted_by = now, user.id
            db.flush()
            return comment
    raise FeedError(f"no comment {comment_id}", "comment_not_found")


def delete_comment(db: Session, actor: audit.Actor, comment_id: int, now: datetime,
                   reason: Optional[str] = None) -> Comment:
    """A teacher deletes any comment (moderation): soft, with an optional reason (at most REASON_MAX characters).
    Audited as comment.delete: the author, the thread and the reason, not the text."""
    _teacher(db, actor)
    found = _locked_comment(db, comment_id)
    if found is None:
        raise FeedError(f"no comment {comment_id}", "comment_not_found")
    comment, thread, item = found
    reason = " ".join((reason or "").split()) or None
    if reason is not None and len(reason) > REASON_MAX:
        raise FeedError(f"a reason is at most {REASON_MAX} characters", "reason_too_long")
    _raise(rules.can_delete(CONSOLE, comment, can_read=True))  # a teacher: only already_deleted refuses
    comment.deleted_at, comment.deleted_by, comment.delete_reason = now, actor.user_id, reason
    db.flush()
    author = db.get(User, comment.author_id) if comment.author_id is not None else None
    student = db.get(User, thread.student_id) if thread.student_id is not None else None
    audit.record(db, actor, "comment.delete", now, object_type="comment", object_id=comment.id,
                 details={"item": item.id, "thread": "shared" if student is None else "private",
                          "student": None if student is None else student.login,
                          "author": "console" if author is None else author.login, "reason": reason})
    return comment


# ----------------------------------------------------------------------------- reading
def _author(user: Optional[User], teacher_view: bool) -> dict:
    if user is None:  # the console
        out = {"display_name": TEACHER_NAME, "teacher": True}
        return {"id": None, "login": None, **out, "console": True} if teacher_view else out
    out = {"display_name": user.display_name, "teacher": user.role == Role.admin}
    return {"id": user.id, "login": user.login, **out, "console": False} if teacher_view else out


def _comment_view(comment: Comment, author: Optional[User], deleter: Optional[User], viewer) -> dict:
    if not access.is_admin(viewer):
        return {"id": comment.id, "at": comment.created_at, "body": comment.body,
                "author": _author(author, False), "mine": comment.author_id == viewer.id}
    deleted = comment.deleted_at is not None
    return {"id": comment.id, "at": comment.created_at, "body": comment.body, "author": _author(author, True),
            "deleted": deleted, "deleted_at": comment.deleted_at,
            "deleted_by": None if not deleted else (
                {"id": None, "login": None, "console": True} if deleter is None
                else {"id": deleter.id, "login": deleter.login, "console": False}),
            "delete_reason": comment.delete_reason}


def comment_card(db: Session, viewer, comment: Comment) -> dict:
    """One comment as `viewer` sees it in a thread, with where it is: the item, the thread and its kind, the student
    of a private thread. No access check: for the answer to a comment the viewer has just written or deleted (the
    admin API, judgectl)."""
    thread = db.get(CommentThread, comment.thread_id)
    author = db.get(User, comment.author_id) if comment.author_id is not None else None
    deleter = db.get(User, comment.deleted_by) if comment.deleted_by is not None else None
    student = db.get(User, thread.student_id) if thread.student_id is not None else None
    return {**_comment_view(comment, author, deleter, viewer), "thread_id": thread.id, "item": thread.item_id,
            "kind": "shared" if student is None else "private",
            "student": None if student is None else (
                {"id": student.id, "login": student.login, "display_name": student.display_name}
                if access.is_admin(viewer) else {"display_name": student.display_name})}


def _can_post(db: Session, viewer, item: FeedItem, student: Optional[User], now: datetime,
              thread: Optional[CommentThread]) -> Optional[str]:
    """None when the viewer may write in the thread now, else the code (the body and the rate aside)."""
    probe = "?"
    if access.is_admin(viewer):
        decision = (rules.can_post_shared(viewer, item, now, in_audience=False, body=probe) if student is None else
                    rules.can_post_private(viewer, item, student, now, body=probe,
                                           in_audience=feed.in_audience(db, item, student),
                                           has_work=_has_work(db, item, student), thread_exists=thread is not None))
    elif student is None:
        decision = rules.can_post_shared(viewer, item, now, body=probe, **_shared_inputs(db, item, viewer))
    else:
        decision = rules.can_post_private(viewer, item, student, now, body=probe,
                                          in_audience=feed.in_audience(db, item, viewer),
                                          thread_exists=thread is not None)
    return None if decision else decision.reason


def _thread_view(db: Session, viewer, item: FeedItem, student: Optional[User], now: datetime,
                 after: Optional[int], limit: int) -> dict:
    thread = _readable_thread(db, viewer, item, student, now)
    limit = _page(limit)
    comments, more = [], False
    if thread is not None:
        Author, Deleter = aliased(User), aliased(User)
        query = (select(Comment, Author, Deleter).outerjoin(Author, Author.id == Comment.author_id)
                 .outerjoin(Deleter, Deleter.id == Comment.deleted_by).where(Comment.thread_id == thread.id))
        if not access.is_admin(viewer):
            query = query.where(Comment.deleted_at.is_(None))
        if after is not None:
            query = query.where(Comment.id > after)
        rows = db.execute(query.order_by(Comment.id).limit(limit + 1)).all()
        more = len(rows) > limit
        comments = [_comment_view(c, a, d, viewer) for c, a, d in rows[:limit]]
    cannot = _can_post(db, viewer, item, student, now, thread)
    return {"thread_id": None if thread is None else thread.id, "item": item.id,
            "kind": "shared" if student is None else "private",
            "student": None if student is None else (
                {"id": student.id, "login": student.login, "display_name": student.display_name}
                if access.is_admin(viewer) else {"display_name": student.display_name}),
            "comments_enabled": item.comments_enabled, "can_post": cannot is None, "cannot_post": cannot,
            "comments": comments, "next_after": comments[-1]["id"] if more else None,
            "unread": None if viewer.id is None else (_unread(db, viewer, thread) if thread is not None else 0)}


def shared_thread(db: Session, viewer, item: FeedItem, now: datetime, *, after: Optional[int] = None,
                  limit: int = PAGE_MAX) -> dict:
    """The shared thread of the item, oldest first, a page of `limit` comments after the id `after`
    (`next_after`: the cursor of the next page). thread_not_found when the viewer may not read it."""
    return _thread_view(db, viewer, item, None, now, after, limit)


def private_thread(db: Session, viewer, item: FeedItem, student: User, now: datetime, *,
                   after: Optional[int] = None, limit: int = PAGE_MAX) -> dict:
    """The private thread of `student` on the item. A student reads only their own (`student` is their own User;
    another one is thread_not_found); a teacher reads the thread of a student addressed now or with work or a
    thread there."""
    return _thread_view(db, viewer, item, student, now, after, limit)


# ----------------------------------------------------------------------------- read marks
def _unread_filter(reader_id: int):
    return and_(Comment.deleted_at.is_(None), Comment.author_id.is_distinct_from(reader_id),
                Comment.id > func.coalesce(ThreadRead.last_read_comment_id, 0))


def _unread(db: Session, viewer, thread: CommentThread) -> int:
    return db.execute(select(func.count()).select_from(Comment)
                      .outerjoin(ThreadRead, and_(ThreadRead.thread_id == Comment.thread_id,
                                                  ThreadRead.user_id == viewer.id))
                      .where(Comment.thread_id == thread.id, _unread_filter(viewer.id))).scalar_one()


def _reader(viewer) -> None:
    if viewer.id is None:
        raise ValueError("the console has no read marks")


def unread_count(db: Session, viewer, item: FeedItem, now: datetime, *, student: Optional[User] = None) -> int:
    """Unread comments of the thread of (item, student) for the viewer: of others, not deleted, after their mark.
    No thread yet: 0 (nothing is created)."""
    _reader(viewer)
    thread = _readable_thread(db, viewer, item, student, now)
    return 0 if thread is None else _unread(db, viewer, thread)


def mark_read(db: Session, viewer, item: FeedItem, now: datetime, *, student: Optional[User] = None,
              up_to: Optional[int] = None) -> int:
    """Mark the thread read up to the comment `up_to` (by default the last one the viewer sees); returns what is
    left unread. The mark only grows (GREATEST): an older `up_to` changes nothing. `up_to` must be a comment of
    this thread the viewer sees, else comment_not_found. No thread yet: 0, nothing is written."""
    _reader(viewer)
    thread = _readable_thread(db, viewer, item, student, now)
    if thread is None:
        if up_to is not None:
            raise FeedError(f"no comment {up_to} in this thread", "comment_not_found")
        return 0
    visible = select(func.max(Comment.id)).where(Comment.thread_id == thread.id)
    if not access.is_admin(viewer):
        visible = visible.where(Comment.deleted_at.is_(None))
    if up_to is not None:
        visible = visible.where(Comment.id == up_to)
    target = db.execute(visible).scalar_one()
    if target is None:
        if up_to is not None:
            raise FeedError(f"no comment {up_to} in this thread", "comment_not_found")
        return 0
    insert = pg_insert(ThreadRead).values(user_id=viewer.id, thread_id=thread.id, last_read_comment_id=target,
                                          updated_at=now)
    db.execute(insert.on_conflict_do_update(
        index_elements=["user_id", "thread_id"],
        set_={"last_read_comment_id": func.greatest(ThreadRead.last_read_comment_id,
                                                    insert.excluded.last_read_comment_id),
              "updated_at": now}))
    return _unread(db, viewer, thread)


def unread_by_item(db: Session, viewer, items: Iterable[FeedItem], now: datetime) -> dict:
    """{item id: unread comments} over the threads of each item the viewer reads: the shared one and, for a
    student, their own private one (for the feed of 4b.2). Threads not readable now do not count."""
    _reader(viewer)
    items = {item.id: item for item in items}
    out = {item_id: 0 for item_id in items}
    if not items:
        return out
    mine = CommentThread.student_id.is_(None) if access.is_admin(viewer) else or_(
        CommentThread.student_id.is_(None), CommentThread.student_id == viewer.id)
    rows = db.execute(select(CommentThread, func.count(Comment.id))
                      .join(Comment, Comment.thread_id == CommentThread.id)
                      .outerjoin(ThreadRead, and_(ThreadRead.thread_id == CommentThread.id,
                                                  ThreadRead.user_id == viewer.id))
                      .where(CommentThread.item_id.in_(list(items)), mine, _unread_filter(viewer.id))
                      .group_by(CommentThread.id)).all()
    for thread, count in rows:
        item = items[thread.item_id]
        student = None if thread.student_id is None else viewer
        if _can_read(db, viewer, item, student, now, thread):
            out[item.id] += count
    return out


# ----------------------------------------------------------------------------- the teachers' inbox
def threads_query(db: Session, viewer, now: datetime, *, item: Optional[FeedItem] = None,
                  group: Optional[Group] = None, awaiting: Optional[bool] = None, unread: Optional[bool] = None,
                  cursor: Optional[int] = None, limit: int = 50) -> dict:
    """Every thread for teachers, the one with the newest comment first; a page: {"items", "next_cursor"} (the
    cursor is the id of the newest comment of the last row). Filters: `item`; `group` - private threads of its
    current members (D-043) and shared threads of items addressed to it; `awaiting` - the last comment not deleted
    is a student's (deleting the teacher's reply brings the thread back); `unread` - for the viewer (not for
    CONSOLE: no marks)."""
    if not access.is_admin(viewer):
        raise ValueError("the inbox is the teachers'")
    if unread is not None:
        _reader(viewer)
    limit = _page(limit)
    reader = viewer.id
    live = Comment.deleted_at.is_(None)
    stats = (select(Comment.thread_id.label("thread_id"), func.count().label("total"),
                    func.count().filter(live).label("live"), func.max(Comment.id).label("last_id"),
                    func.max(Comment.id).filter(live).label("last_live_id"),
                    func.count().filter(_unread_filter(reader if reader is not None else -1)).label("unread"))
             .outerjoin(ThreadRead, and_(ThreadRead.thread_id == Comment.thread_id, ThreadRead.user_id == reader))
             .group_by(Comment.thread_id).subquery())
    Student, Last, Author = aliased(User), aliased(Comment), aliased(User)
    awaiting_expr = func.coalesce(Author.role == Role.student, False)
    query = (select(CommentThread, FeedItem, Student, Last, Author, stats.c.total, stats.c.live, stats.c.last_id,
                    stats.c.unread, awaiting_expr.label("awaiting"))
             .join(stats, stats.c.thread_id == CommentThread.id)
             .join(FeedItem, FeedItem.id == CommentThread.item_id)
             .outerjoin(Student, Student.id == CommentThread.student_id)
             .outerjoin(Last, Last.id == stats.c.last_live_id)
             .outerjoin(Author, Author.id == Last.author_id))
    if item is not None:
        query = query.where(CommentThread.item_id == item.id)
    if group is not None:
        members = select(GroupMember.user_id).where(GroupMember.group_id == group.id,
                                                    GroupMember.removed_at.is_(None))
        addressed = select(FeedItemAudience.item_id).where(FeedItemAudience.group_id == group.id)
        query = query.where(or_(CommentThread.student_id.in_(members),
                                and_(CommentThread.student_id.is_(None), CommentThread.item_id.in_(addressed))))
    if awaiting is not None:
        query = query.where(awaiting_expr.is_(awaiting))
    if unread is not None:
        query = query.where(stats.c.unread > 0 if unread else stats.c.unread == 0)
    if cursor is not None:
        query = query.where(stats.c.last_id < cursor)
    rows = db.execute(query.order_by(stats.c.last_id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    out = []
    for thread, it, student, last, author, total, live_n, last_id, unread_n, waits in rows[:limit]:
        out.append({
            "thread_id": thread.id, "kind": "shared" if student is None else "private",
            "item": {"id": it.id, "title": it.title, "kind": it.kind.value, "state": it.state.value},
            "student": None if student is None else {"id": student.id, "login": student.login,
                                                     "display_name": student.display_name},
            "comments": live_n, "deleted": total - live_n, "last_id": last_id,
            "last": None if last is None else {"id": last.id, "at": last.created_at,
                                               "by_student": author is not None and author.role == Role.student},
            "awaiting_reply": bool(waits), "unread": None if reader is None else unread_n})
    return {"items": out, "next_cursor": out[-1]["last_id"] if more else None}
