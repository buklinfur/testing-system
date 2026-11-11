"""Who reads and who writes comment threads of the class feed (D-072, D-074, docs/CLASSROOM.md §9): pure functions.

No database, no clock of their own. The service passes `now` (server time, UTC, the moment the request was
received) and everything read from the database:
* `in_audience` - the one boolean access.in_audience() makes of the audience rows and the student's current groups
  that are not archived: a student removed from the group, or whose group is archived, is not in the audience;
* `extensions` - the due moments of the extensions of the student (a hidden statement hides the shared thread);
* for a contest item: the contest, the participation and whether the student is in a group of the contest;
* `thread_exists`, `has_work` - whether the private thread / the work of the pair (item, student) exists;
* `recent` - how many comments the student wrote in the last RATE_WINDOW_S seconds, deleted ones included.

Visibility of the item itself is decided by classroom/access.py (item_listed, can_view_assignment), dates by
app/deadline_rules.py; this module adds the threads on top. Admins (teachers) read everything and write everywhere
except in a draft; no rate limit for them. A refusal is a Decision with a stable code; when several conditions fail,
the first in the documented order gives it.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Iterable, Optional

from .. import deadline_rules as rules
from ..deadline_rules import Decision
from . import access

COMMENT_MAX_CHARS = 4000  # == app.models.COMMENT_MAX_CHARS (the CHECK of comments.body; a test keeps them equal)
RATE_WINDOW_S = 60
RATE_PER_WINDOW = 10  # the default of JUDGE_COMMENT_RATE_PER_MIN (the service passes the setting)

OK = Decision(True)
# characters that show nothing on their own: a body made only of them is blank (comment_required)
INVISIBLE_CATEGORIES = {"Zs", "Zl", "Zp", "Cc", "Cf"}


def _v(value) -> str:
    return getattr(value, "value", value)


def _no(code: str) -> Decision:
    return Decision(False, code)


# --------------------------------------------------------------------------- the body
def check_body(text: Optional[str], max_chars: int = COMMENT_MAX_CHARS) -> Decision:
    """The text of a comment, stored as sent (untrusted Markdown, rendered only through a sanitizer). Stricter than
    the CHECKs of comments.body, so whatever passes here the database takes:
    * comment_invalid - NUL or a lone surrogate (PostgreSQL text cannot hold them: a 500 otherwise);
    * comment_required - nothing visible: empty, or only whitespace of any kind (NBSP, U+3000 ...), control and
      format characters (zero width space, BOM, direction marks ...);
    * comment_too_long - more than max_chars characters (code points, as char_length in PostgreSQL)."""
    if text is None:
        return _no("comment_required")
    if not isinstance(text, str) or "\x00" in text or any("\ud800" <= ch <= "\udfff" for ch in text):
        return _no("comment_invalid")
    if all(ch.isspace() or unicodedata.category(ch) in INVISIBLE_CATEGORIES for ch in text):
        return _no("comment_required")
    if len(text) > max_chars:
        return _no("comment_too_long")
    return OK


def rate_ok(recent: int, limit: Optional[int] = RATE_PER_WINDOW) -> Decision:
    """A student may write `limit` comments per RATE_WINDOW_S seconds; `recent` counts the deleted ones too, so
    deleting and writing again does not get round it. limit None or 0: no limit."""
    return _no("rate_limited") if limit and recent >= limit else OK


def _then_body_and_rate(user, body, recent, limit) -> Decision:
    checked = check_body(body)
    if not checked:
        return checked
    return OK if access.is_admin(user) else rate_ok(recent, limit)


# --------------------------------------------------------------------------- the shared thread of an item
def can_read_shared(user, item, now: datetime, *, in_audience: bool, extensions: Iterable[datetime] = (),
                    contest=None, participation=None, in_contest_group: bool = False) -> bool:
    """A student reads the shared thread where they see the item: an assignment with its statement
    (access.can_view_assignment - the thread is part of the statement page and hides with it), a contest item or a
    material item (4v.4) while it is in their feed (access.item_listed). Never in a draft or an archived item,
    outside the audience (removed from the group, the group archived) or before available_from. Turning comments
    off does not hide them."""
    if access.is_admin(user):
        return True
    if _v(item.kind) == "assignment":
        return access.can_view_assignment(user, item, now, in_audience, extensions)
    return access.item_listed(user, item, now, in_audience, contest=contest, participation=participation,
                              in_contest_group=in_contest_group)


def can_post_shared(user, item, now: datetime, *, in_audience: bool, body: Optional[str],
                    extensions: Iterable[datetime] = (), contest=None, participation=None,
                    in_contest_group: bool = False, recent: int = 0,
                    limit: Optional[int] = RATE_PER_WINDOW) -> Decision:
    """In this order, the first failure gives the code:
    1. not_published - a draft (teachers too);
    2. item_archived - (students; teachers may still write there);
    3. no_access - not in the audience now;
    4. not_available_yet - before available_from;
    5. no_access - the thread is not readable (statement hidden after the due moment, the contest not visible);
    6. comments_teacher_only - a contest item: students never write there (solutions would be discussed during the
       contest; questions to the jury are the postponed thread of a contest problem), teachers do (announcements);
    7. comments_disabled - comments_enabled is off (teachers write anyway);
    8. the body (check_body); 9. rate_limited (students)."""
    if _v(item.state) == "draft":
        return _no("not_published")
    if not access.is_admin(user):
        if _v(item.state) == "archived":
            return _no("item_archived")
        if not in_audience:
            return _no("no_access")
        if not rules.available(item, now):
            return _no("not_available_yet")
        if not can_read_shared(user, item, now, in_audience=in_audience, extensions=extensions, contest=contest,
                               participation=participation, in_contest_group=in_contest_group):
            return _no("no_access")
        if _v(item.kind) == "contest":
            return _no("comments_teacher_only")
        if not item.comments_enabled:
            return _no("comments_disabled")
    return _then_body_and_rate(user, body, recent, limit)


# --------------------------------------------------------------------------- the private thread of a student
def _identity(user, student) -> bool:
    """A student acts only in their own private thread (the service never takes a student id from outside; this is
    the second lock)."""
    return access.is_admin(user) or user.id == student.id


def can_read_private(user, item, student, now: datetime, *, in_audience: bool, thread_exists: bool) -> bool:
    """Teachers: every private thread. A student: only their own, of an assignment that is not a draft - once the
    thread exists, always (like their own work: outside the audience, after the due moment, with the statement
    hidden, archived); before that, while the assignment is in their feed (to write the first question)."""
    if access.is_admin(user):
        return True
    if not _identity(user, student) or _v(item.kind) != "assignment" or _v(item.state) == "draft":
        return False
    return thread_exists or access.item_listed(user, item, now, in_audience)


def can_post_private(user, item, student, now: datetime, *, in_audience: bool, body: Optional[str],
                     has_work: bool = False, thread_exists: bool = False, recent: int = 0,
                     limit: Optional[int] = RATE_PER_WINDOW) -> Decision:
    """The private thread of `student` on `item`. In this order, the first failure gives the code.
    A student (only their own thread):
    1. no_access - another student's thread; 2. not_an_assignment; 3. not_published; 4. item_archived;
    5. no_access - not in the audience now; 6. not_available_yet; then the body and rate_limited.
    The due moment, a hidden statement and comments_enabled do not matter: a question about a grade after the due
    moment is normal.
    A teacher: 2. not_an_assignment; 3. not_published; not_a_student - `student` is not a student; not_addressed -
    not in the audience now and neither work nor thread (as grade_work); then the body. Archived: allowed."""
    if not _identity(user, student):
        return _no("no_access")
    if _v(item.kind) != "assignment":
        return _no("not_an_assignment")
    if _v(item.state) == "draft":
        return _no("not_published")
    if access.is_admin(user):
        if _v(student.role) != "student":
            return _no("not_a_student")
        if not (in_audience or has_work or thread_exists):
            return _no("not_addressed")
    else:
        if _v(item.state) == "archived":
            return _no("item_archived")
        if not in_audience:
            return _no("no_access")
        if not rules.available(item, now):
            return _no("not_available_yet")
    return _then_body_and_rate(user, body, recent, limit)


# --------------------------------------------------------------------------- deletion
def can_delete(user, comment, *, can_read: bool) -> Decision:
    """Soft deletion. A teacher: any comment. A student: only their own, in a thread they can read now.
    Order: no_access (a student who cannot read the thread), not_your_comment, already_deleted."""
    if not access.is_admin(user):
        if not can_read:
            return _no("no_access")
        if comment.author_id != user.id:
            return _no("not_your_comment")
    if comment.deleted_at is not None:
        return _no("already_deleted")
    return OK


def visible_to(user, comment) -> bool:
    """A deleted comment does not exist for students (not even a placeholder); teachers see it with its text."""
    return access.is_admin(user) or comment.deleted_at is None
