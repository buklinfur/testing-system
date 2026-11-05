"""Access rules of the class feed and manual assignments (D-059, docs/CLASSROOM.md "Доступ"): pure functions.

No database, no clock of their own: `now` (server time, UTC, the moment the request was received) and everything
that needs the database are passed in by the service layer:
* `live_group_ids` - the groups (not archived) the student is in now; `audience` - the rows of feed_item_audience
  (group_id / user_id); in_audience() turns them into the one boolean the other rules take;
* `extensions` - the due moments of the extensions that apply to the student (app/extensions.py);
* for a contest item: the contest, the participation and whether the student is in a group of the contest.

Every date rule is app/deadline_rules.py (available_from, the effective due moment, the after-due policies), the
same as for contest problems; the visibility of a contest item is decided by contests/access.py. This module only
combines them with the state of the item and its audience.

Admins may do everything except hand work in. For students:
* an item exists in the feed when it is published, addressed to them (a group they are in now, or personally) and
  available (available_from, inclusive); a contest item also needs can_view_contest or can_see_own_results;
* a draft does not exist; an archived item is gone from the feed and refuses work (item_archived), but the
  student's own work and released grade stay visible (can_see_own_work);
* the statement of an assignment is hidden past the effective due moment with after_due_statement = hidden, also
  for a student who already has work; their submissions, status and released grade stay visible.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from .. import deadline_rules as rules
from ..contests import access as contest_access
from ..deadline_rules import Decision

ANSWER_TEXT_MAX_BYTES = 64 * 1024  # the default of JUDGE_ASSIGNMENT_TEXT_MAX_KB (the service passes the setting)


def _v(value) -> str:
    return getattr(value, "value", value)


def is_admin(user) -> bool:
    return contest_access.is_admin(user)


def in_audience(user, audience: Iterable, live_group_ids: Iterable[int]) -> bool:
    """Addressed to the student now: personally, or through a group not archived that they are in now. `audience`:
    rows with group_id / user_id; `live_group_ids`: the student's current groups that are not archived."""
    groups = set(live_group_ids)
    return any(row.user_id == user.id if row.user_id is not None else row.group_id in groups for row in audience)


def item_listed(user, item, now: datetime, in_audience: bool, *, contest=None, participation=None,
                in_contest_group: bool = False) -> bool:
    """Whether the item is in the student's feed. A contest item is shown only while contests/access.py lets the
    student see the contest (can_view_contest or can_see_own_results); the item gives no access by itself."""
    if is_admin(user):
        return True
    if _v(item.state) != "published" or not in_audience or not rules.available(item, now):
        return False
    if _v(item.kind) == "contest":
        if contest is None:
            raise ValueError("a contest item needs its contest")
        return contest_access.can_view_contest(user, contest, participation, now, in_contest_group) \
            or contest_access.can_see_own_results(user, contest, participation)
    return True


def can_view_assignment(user, item, now: datetime, in_audience: bool, extensions: Iterable[datetime] = ()) -> bool:
    """The statement of an assignment: listed, and not hidden after the effective due moment (after_due_statement =
    hidden hides it from everyone, a student with work included). Archived: hidden (the work stays visible)."""
    if is_admin(user):
        return True
    if _v(item.kind) != "assignment" or not item_listed(user, item, now, in_audience):
        return False
    return not rules.statement_hidden_after_due(item, now, extensions)


def can_see_own_work(user, item, has_work: bool) -> bool:
    """The student's own work (submissions, status, released grade): always once it exists and the item is not a
    draft - without the audience, after any deadline, with the statement hidden, archived."""
    return is_admin(user) or (has_work and _v(item.state) != "draft")


def can_submit_work(user, item, now: datetime, *, in_audience: bool, extensions: Iterable[datetime] = (),
                    work_status: Optional[str] = None, text: Optional[str] = None,
                    text_limit: int = ANSWER_TEXT_MAX_BYTES) -> Decision:
    """Whether a hand-in received at `now` is accepted, and whether it is late. `work_status`: the cached status of
    the student's work (None: no work yet); `text`: the text answer (None or blank: none). The order of the checks
    is fixed (docs/CLASSROOM.md): the first that fails gives the code."""
    if is_admin(user):
        return Decision(False, "not_a_student")
    if _v(item.kind) != "assignment":
        return Decision(False, "not_an_assignment")
    state = _v(item.state)
    if state == "draft":
        return Decision(False, "not_published")
    if state == "archived":
        return Decision(False, "item_archived")
    if not in_audience:
        return Decision(False, "no_access")
    if not rules.available(item, now):
        return Decision(False, "not_available_yet")
    deadline = rules.after_due_submit(item, now, extensions)
    if not deadline:
        return deadline  # deadline_passed
    if _v(work_status) == "graded":
        return Decision(False, "already_graded")
    answer = _v(item.answer_kind)
    blank = text is None or not text.strip()
    if answer == "text" and blank:
        return Decision(False, "answer_required")
    if answer == "mark" and not blank:
        return Decision(False, "text_not_expected")
    if not blank and len(text.encode("utf-8")) > text_limit:
        return Decision(False, "text_too_long")
    return Decision(True, "late" if deadline.late else "ok", late=deadline.late)


def missing(item, now: datetime, has_submissions: bool, extensions: Iterable[datetime] = ()) -> bool:
    """«Не сдано»: no submission and the effective due moment has passed (a flag, not a status)."""
    return not has_submissions and rules.past_due(item, now, extensions)
