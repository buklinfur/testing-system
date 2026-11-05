"""Feed items: create, edit, publish, archive, delete, audience, extensions, feeds (D-059, docs/CLASSROOM.md §7).

Functions flush but never commit; the caller owns the transaction (judgectl: one per command; the admin API: one
per request). Every change is written to audit_log (item.*). A refusal raises FeedError with a stable code; the
caller rolls back. Who may see or hand in what is decided by classroom/access.py; these functions only carry it out
and collect what it needs from the database.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .. import extensions as ext
from ..auth import audit
from ..contests import access as contest_access
from ..contests import service as contests
from ..models import (AfterDueStatement, AfterDueSubmit, AnswerKind, AssignmentReview, AssignmentWork, Contest,
                      ContestGroup, DeadlineExtension, FeedItem, FeedItemAudience, FeedItemKind, FeedItemState,
                      GradesRelease, GradingScale, Group, GroupMember, ReviewAction, Role, User)
from ..timeutil import iso_utc
from . import access
from . import grading as grading_rules

TITLE_MAX = 200
EDITABLE = ("title", "body_md", "available_from", "due_at", "after_due_statement", "after_due_submit", "grading",
            "max_points", "answer_kind", "grades_release")
GRADING_FIELDS = ("grading", "max_points")


class FeedError(ValueError):
    """A refusal with a stable `code` for the API and judgectl, every reason in `problems`, extra data in `data`."""

    def __init__(self, message, code: str, **data):
        self.code = code
        self.problems = [message] if isinstance(message, str) else list(message)
        self.data = data
        super().__init__("; ".join(self.problems))


def _v(value):
    return getattr(value, "value", value)


# ----------------------------------------------------------------------------- reading
def get_item(db: Session, item_id: int, lock: bool = False) -> FeedItem:
    query = select(FeedItem).where(FeedItem.id == item_id)
    item = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if item is None:
        raise FeedError(f"no feed item {item_id}", "item_not_found")
    return item


def items_query(*, group: Optional[Group] = None, kind: Optional[str] = None, states: Optional[Iterable[str]] = None):
    """A select of feed items for lists (the admin API pages it by id, judgectl orders it): addressed to `group`, of
    one kind, in these states."""
    query = select(FeedItem)
    if group is not None:
        query = query.where(FeedItem.id.in_(select(FeedItemAudience.item_id)
                                            .where(FeedItemAudience.group_id == group.id)))
    if kind is not None:
        query = query.where(FeedItem.kind == FeedItemKind(kind))
    if states is not None:
        query = query.where(FeedItem.state.in_([FeedItemState(s) for s in states]))
    return query


def audience_rows(db: Session, item: FeedItem) -> list[FeedItemAudience]:
    return list(db.execute(select(FeedItemAudience).where(FeedItemAudience.item_id == item.id)
                           .order_by(FeedItemAudience.id)).scalars())


def live_group_ids(db: Session, user: User) -> set[int]:
    """The groups (not archived) the student is in now: what access.in_audience needs."""
    return set(db.execute(select(GroupMember.group_id).join(Group, Group.id == GroupMember.group_id)
                          .where(GroupMember.user_id == user.id, GroupMember.removed_at.is_(None),
                                 Group.archived_at.is_(None))).scalars())


def in_audience(db: Session, item: FeedItem, user: User) -> bool:
    return access.in_audience(user, audience_rows(db, item), live_group_ids(db, user))


def due_moments(db: Session, item: FeedItem, user: User) -> list[datetime]:
    """access.py's `extensions` for this student and item (own and current groups)."""
    return ext.due_moments(db, DeadlineExtension.feed_item_id, item.id, user)


def feed_order():
    """Newest first: available_from, else published_at, else created_at."""
    return (func.coalesce(FeedItem.available_from, FeedItem.published_at, FeedItem.created_at).desc(),
            FeedItem.id.desc())


def group_feed(db: Session, group: Group, include_drafts: bool = True) -> list[FeedItem]:
    """The items addressed to a group, newest first (available_from, else published_at): the teacher's view."""
    query = (select(FeedItem).join(FeedItemAudience, FeedItemAudience.item_id == FeedItem.id)
             .where(FeedItemAudience.group_id == group.id))
    if not include_drafts:
        query = query.where(FeedItem.state != FeedItemState.draft)
    return list(db.execute(query.order_by(*feed_order())).scalars())


def student_feed(db: Session, user: User, now: datetime) -> list[FeedItem]:
    """The student's feed: items of their current groups and those addressed to them, each once, newest first, as
    access.item_listed decides (a contest item through the contest's own rules). The endpoint is 4b.2's."""
    groups = live_group_ids(db, user)
    addressed = select(FeedItemAudience.item_id).where(
        (FeedItemAudience.user_id == user.id) | FeedItemAudience.group_id.in_(groups or [-1]))
    items = list(db.execute(select(FeedItem).where(FeedItem.id.in_(addressed), FeedItem.state ==
                                                   FeedItemState.published).order_by(*feed_order())).scalars())
    out = []
    for item in items:
        kw = {}
        if item.kind == FeedItemKind.contest:
            contest = db.get(Contest, item.contest_id)
            participation, in_group = contests.access_inputs(db, contest, user)
            kw = dict(contest=contest, participation=participation, in_contest_group=in_group)
        if access.item_listed(user, item, now, True, **kw):  # every item here is addressed to the student
            out.append(item)
    return out


def feed_items_of_group(db: Session, group: Group) -> list[FeedItem]:
    """Items whose audience names the group (any state): shown when the group is archived (K6)."""
    return group_feed(db, group)


def contest_warning(db: Session, item: FeedItem) -> Optional[str]:
    """For a contest item: why addressees may not see it. The item never gives access; only the contest does."""
    if item.kind != FeedItemKind.contest:
        return None
    contest = db.get(Contest, item.contest_id)
    mode = _v(contest.access_mode)
    if mode == "open":
        return None
    rows = audience_rows(db, item)
    notes = []
    if mode in contest_access.GROUP_MODES:
        attached = set(db.execute(select(ContestGroup.group_id).where(ContestGroup.contest_id == contest.id)).scalars())
        missing = sorted(db.get(Group, r.group_id).name for r in rows
                         if r.group_id is not None and r.group_id not in attached)
        if missing:
            notes.append(f"groups not attached to contest {contest.slug}: {', '.join(missing)}")
    if mode == "code":
        notes.append(f"contest {contest.slug} is entered with its invite code: addressees without it do not see it")
    elif mode == "groups" and any(r.user_id is not None for r in rows):
        notes.append(f"students addressed personally see contest {contest.slug} only while in a group of it")
    if not notes:
        return None
    return "; ".join(notes) + " (the item gives no access: attach the groups in contest.yaml or give the code)"


# ----------------------------------------------------------------------------- validation
def _fields(item: Optional[FeedItem], kind: str, values: dict) -> dict:
    """The fields of the item after the change, as plain values, with the defaults of a new assignment."""
    current = {name: _v(getattr(item, name)) for name in EDITABLE} if item is not None else {
        "title": None, "body_md": "", "available_from": None, "due_at": None, "after_due_statement": "visible",
        "after_due_submit": "late", "grading": None, "max_points": None, "answer_kind": None, "grades_release": None}
    if item is None and kind == "assignment":
        current.update(answer_kind="text", grades_release="manual")
    current.update({k: _v(v) for k, v in values.items()})
    return current


def _validate(kind: str, f: dict) -> None:
    """The CHECKs of feed_items as refusals with codes, the first problem's code."""
    problems = []  # (code, message)
    title = " ".join((f["title"] or "").split())
    if not title or len(title) > TITLE_MAX:
        problems.append(("title_invalid", f"a title is 1..{TITLE_MAX} characters"))
    f["title"] = title
    if f["body_md"] is None:
        f["body_md"] = ""
    if f["available_from"] is not None and f["due_at"] is not None and not f["available_from"] < f["due_at"]:
        problems.append(("deadline_invalid", "available_from must be before due_at"))
    if f["after_due_statement"] not in {m.value for m in AfterDueStatement}:
        problems.append(("item_invalid", f"after_due_statement: {f['after_due_statement']!r}"))
    if f["after_due_submit"] not in {m.value for m in AfterDueSubmit}:
        problems.append(("item_invalid", f"after_due_submit: {f['after_due_submit']!r}"))
    if kind == "assignment":
        grading = f["grading"]
        if grading not in {m.value for m in GradingScale}:
            problems.append(("grading_invalid", "grading is points, pass_fail or none"))
        elif grading == "points":
            points = grading_rules.parse_points(f["max_points"]) if f["max_points"] is not None else None
            if points is None or not points > 0 or points >= Decimal("1000000"):
                problems.append(("max_points_invalid", "grading points needs max_points > 0 (like 10 or 4.5: at "
                                                       "most two decimals, no exponent)"))
            else:
                f["max_points"] = points
        elif f["max_points"] is not None:
            problems.append(("max_points_invalid", f"max_points is only for grading points, not {grading}"))
        if f["answer_kind"] in ("files", "text_or_files"):
            problems.append(("answer_kind_invalid", "file answers come with session 4v.3: answer_kind mark or text"))
        elif f["answer_kind"] not in ("mark", "text"):
            problems.append(("answer_kind_invalid", "answer_kind is mark or text"))
        if f["grades_release"] not in {m.value for m in GradesRelease}:
            problems.append(("item_invalid", "grades_release is immediate or manual"))
    else:
        if f["due_at"] is not None or f["after_due_statement"] != "visible" or f["after_due_submit"] != "late":
            problems.append(("item_invalid", "only assignments have a deadline and after-due policies"))
        if any(f[k] is not None for k in ("grading", "max_points", "answer_kind", "grades_release")):
            problems.append(("item_invalid", "only assignments have grading and answer settings"))
    if problems:
        raise FeedError([m for _c, m in problems], problems[0][0])


def _apply(item: FeedItem, f: dict) -> list:
    """Set the fields; the list of changed names."""
    enums = {"after_due_statement": AfterDueStatement, "after_due_submit": AfterDueSubmit, "grading": GradingScale,
             "answer_kind": AnswerKind, "grades_release": GradesRelease}
    changed = []
    for name in EDITABLE:
        value = f[name]
        if name in enums and value is not None:
            value = enums[name](value)
        if getattr(item, name) != value:
            setattr(item, name, value)
            changed.append(name)
    return changed


def _details(item: FeedItem) -> dict:
    return {"kind": item.kind.value, "title": item.title,
            **({"contest_id": item.contest_id} if item.contest_id is not None else {})}


# ----------------------------------------------------------------------------- changes
def create_item(db: Session, actor: audit.Actor, now: datetime, *, kind: str, title: str, body_md: str = "",
                contest: Optional[Contest] = None, **fields) -> FeedItem:
    """A new item, a draft. kind contest needs `contest`; an assignment takes the deadline, grading and answer
    fields (grading is required; answer_kind text and grades_release manual by default)."""
    if kind == "material":
        raise FeedError("materials come with session 4v.4", "kind_not_available")
    if kind not in ("contest", "assignment"):
        raise FeedError(f"kind is contest or assignment, not {kind!r}", "item_invalid")
    if (kind == "contest") != (contest is not None):
        raise FeedError("a contest item names its contest; an assignment names none",
                        "contest_required" if kind == "contest" else "item_invalid")
    unknown = sorted(set(fields) - set(EDITABLE))
    if unknown:
        raise FeedError(f"unknown fields: {', '.join(unknown)}", "item_invalid")
    if kind == "assignment" and fields.get("grading") is None:
        raise FeedError("an assignment needs grading: points (with max_points), pass_fail or none", "grading_invalid")
    f = _fields(None, kind, {"title": title, "body_md": body_md, **fields})
    _validate(kind, f)
    item = FeedItem(kind=FeedItemKind(kind), state=FeedItemState.draft,
                    contest_id=contest.id if contest is not None else None, created_by=actor.user_id, created_at=now,
                    updated_at=now)
    _apply(item, f)
    db.add(item)
    db.flush()
    audit.record(db, actor, "item.create", now, object_type="feed_item", object_id=item.id, details=_details(item))
    return item


def has_grades(db: Session, item: FeedItem) -> bool:
    return db.execute(select(exists().where(AssignmentReview.work_id == AssignmentWork.id,
                                            AssignmentWork.item_id == item.id,
                                            AssignmentReview.action == ReviewAction.graded))).scalar()


def update_item(db: Session, actor: audit.Actor, item: FeedItem, now: datetime, **values) -> list:
    """Change editable fields (EDITABLE): only the ones given. A new scale pass_fail or none without max_points
    clears max_points; with a max_points not null it is max_points_invalid, and so is points without max_points
    (the same rule for the admin API and judgectl). The scale (grading, max_points) is fixed once a grade exists
    (grading_locked); a new due_at or policy never recomputes the `late` of hand-ins already received. Returns the
    changed names (empty: nothing changed, nothing audited)."""
    unknown = sorted(set(values) - set(EDITABLE))
    if unknown:
        raise FeedError(f"these fields cannot be changed: {', '.join(unknown)}", "item_invalid")
    if _v(values.get("grading")) in ("pass_fail", "none") and "max_points" not in values:
        values = {**values, "max_points": None}  # another scale drops max_points; one sent with it is refused
    f = _fields(item, item.kind.value, values)
    _validate(item.kind.value, f)
    scale_changes = [n for n in GRADING_FIELDS if _v(getattr(item, n)) != f[n]
                     and not (n == "max_points" and item.max_points is not None and f[n] is not None
                              and Decimal(item.max_points) == Decimal(f[n]))]
    if scale_changes and has_grades(db, item):
        raise FeedError("the scale cannot change once a grade exists", "grading_locked")
    before = {n: _v(getattr(item, n)) for n in EDITABLE}
    changed = _apply(item, f)
    if not changed:
        return []
    item.updated_at = now
    db.flush()
    audit.record(db, actor, "item.update", now, object_type="feed_item", object_id=item.id,
                 details={**_details(item), "changes": {n: [_show(before[n]), _show(_v(getattr(item, n)))]
                                                        for n in changed if n != "body_md"},
                          **({"body_md": "changed"} if "body_md" in changed else {})})
    return changed


def _show(value):
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def publish(db: Session, actor: audit.Actor, item: FeedItem, now: datetime) -> bool:
    """draft -> published (with available_from in the future: a delayed publication). Needs an addressee."""
    if item.state == FeedItemState.published:
        return False
    if item.state == FeedItemState.archived:
        raise FeedError("the item is archived: unarchive it", "item_archived")
    if not audience_rows(db, item):
        raise FeedError("the item has no addressee: add a group or a student first", "no_audience")
    item.state = FeedItemState.published
    item.published_at = item.published_at or now
    item.updated_at = now
    db.flush()
    audit.record(db, actor, "item.publish", now, object_type="feed_item", object_id=item.id,
                 details={**_details(item), "available_from": iso_utc(item.available_from)})
    return True


def archive(db: Session, actor: audit.Actor, item: FeedItem, now: datetime) -> bool:
    """Gone from the feed, refuses hand-ins; students keep their work and released grades. A draft is deleted
    instead (delete_item)."""
    if item.state == FeedItemState.archived:
        return False
    if item.state == FeedItemState.draft:
        raise FeedError("a draft is not archived: delete it", "item_not_published")
    item.state = FeedItemState.archived
    item.updated_at = now
    db.flush()
    audit.record(db, actor, "item.archive", now, object_type="feed_item", object_id=item.id, details=_details(item))
    return True


def unarchive(db: Session, actor: audit.Actor, item: FeedItem, now: datetime) -> bool:
    if item.state != FeedItemState.archived:
        return False
    if not audience_rows(db, item):
        raise FeedError("the item has no addressee: add a group or a student first", "no_audience")
    item.state = FeedItemState.published
    item.updated_at = now
    db.flush()
    audit.record(db, actor, "item.unarchive", now, object_type="feed_item", object_id=item.id,
                 details=_details(item))
    return True


def delete_item(db: Session, actor: audit.Actor, item: FeedItem, now: datetime) -> dict:
    """Only a draft without works and extensions (its audience goes with it); anything else is archived."""
    if item.state != FeedItemState.draft:
        raise FeedError("only a draft is deleted: archive a published item", "item_not_draft")
    if db.execute(select(exists().where(AssignmentWork.item_id == item.id))).scalar():
        raise FeedError("the item has students' work: archive it", "item_has_works")
    if db.execute(select(exists().where(DeadlineExtension.feed_item_id == item.id))).scalar():
        raise FeedError("the item has deadline extensions: remove them first", "item_has_extensions")
    details = _details(item)
    item_id = item.id
    db.delete(item)
    db.flush()
    audit.record(db, actor, "item.delete", now, object_type="feed_item", object_id=item_id, details=details)
    return details


def set_audience(db: Session, actor: audit.Actor, item: FeedItem, now: datetime, *, groups: Iterable[Group] = (),
                 users: Iterable[User] = ()) -> dict:
    """Make the audience exactly these groups and students. A group must not be archived (one already in the
    audience may stay: it just gives nothing, K6); a student must be a student. A published item keeps at least
    one addressee. Returns {"added": [...], "removed": [...]} (group names, logins)."""
    groups, users = list(groups), list(users)
    rows = audience_rows(db, item)
    have_groups = {r.group_id: r for r in rows if r.group_id is not None}
    have_users = {r.user_id: r for r in rows if r.user_id is not None}
    archived = [g.name for g in groups if g.archived_at is not None and g.id not in have_groups]
    if archived:
        raise FeedError(f"archived groups cannot be addressed: {', '.join(archived)}", "group_archived",
                        groups=archived)
    not_students = [u.login for u in users if u.role != Role.student]
    if not_students:
        raise FeedError(f"only students are addressed: {', '.join(not_students)}", "not_a_student",
                        logins=not_students)
    if item.state != FeedItemState.draft and not groups and not users:
        raise FeedError("a published item keeps at least one addressee", "no_audience")
    want_groups, want_users = {g.id: g for g in groups}, {u.id: u for u in users}
    added, removed = [], []
    for gid, group in want_groups.items():
        if gid not in have_groups:
            db.add(FeedItemAudience(item_id=item.id, group_id=gid, added_at=now, added_by=actor.user_id))
            added.append(f"group {group.name}")
    for uid, user in want_users.items():
        if uid not in have_users:
            db.add(FeedItemAudience(item_id=item.id, user_id=uid, added_at=now, added_by=actor.user_id))
            added.append(f"student {user.login}")
    for gid, row in have_groups.items():
        if gid not in want_groups:
            removed.append(f"group {db.get(Group, gid).name}")
            db.delete(row)
    for uid, row in have_users.items():
        if uid not in want_users:
            removed.append(f"student {db.get(User, uid).login}")
            db.delete(row)
    db.flush()
    if added or removed:
        audit.record(db, actor, "item.audience", now, object_type="feed_item", object_id=item.id,
                     details={**_details(item), "added": added, "removed": removed})
    return {"added": added, "removed": removed}


def add_audience(db: Session, actor: audit.Actor, item: FeedItem, now: datetime, *, groups: Iterable[Group] = (),
                 users: Iterable[User] = ()) -> dict:
    rows = audience_rows(db, item)
    cur_groups = [db.get(Group, r.group_id) for r in rows if r.group_id is not None]
    cur_users = [db.get(User, r.user_id) for r in rows if r.user_id is not None]
    return set_audience(db, actor, item, now, groups=_uniq(cur_groups + list(groups)),
                        users=_uniq(cur_users + list(users)))


def remove_audience(db: Session, actor: audit.Actor, item: FeedItem, now: datetime, *,
                    groups: Iterable[Group] = (), users: Iterable[User] = ()) -> dict:
    drop_g, drop_u = {g.id for g in groups}, {u.id for u in users}
    rows = audience_rows(db, item)
    keep_groups = [db.get(Group, r.group_id) for r in rows if r.group_id is not None and r.group_id not in drop_g]
    keep_users = [db.get(User, r.user_id) for r in rows if r.user_id is not None and r.user_id not in drop_u]
    return set_audience(db, actor, item, now, groups=keep_groups, users=keep_users)


def _uniq(rows: list) -> list:
    seen, out = set(), []
    for row in rows:
        if row.id not in seen:
            seen.add(row.id)
            out.append(row)
    return out


# ----------------------------------------------------------------------------- deadline extensions of items
def _target(item: FeedItem) -> ext.Target:
    return ext.Target(column=DeadlineExtension.feed_item_id, id=item.id, due_at=item.due_at,
                      available_from=item.available_from, name=f"item {item.id}",
                      no_deadline=f"item {item.id} has no due_at: there is nothing to extend",
                      audit_prefix="item", audit_details={"item": item.id, "title": item.title})


def add_extension(db: Session, actor: audit.Actor, item: FeedItem, due_at: datetime, now: datetime, *,
                  user: Optional[User] = None, group: Optional[Group] = None,
                  reason: Optional[str] = None) -> tuple[DeadlineExtension, Optional[datetime]]:
    """The extensions of contest problems, for an assignment (app/extensions.py, audit item.extension_add)."""
    if item.kind != FeedItemKind.assignment:
        raise FeedError(f"item {item.id} is not an assignment: it has no deadline", "not_an_assignment")
    try:
        return ext.add(db, actor, _target(item), due_at, now, user=user, group=group, reason=reason)
    except ext.ExtensionError as exc:
        raise FeedError(str(exc), exc.code) from exc


def list_extensions(db: Session, item: FeedItem) -> list[dict]:
    rows = db.execute(select(DeadlineExtension, User.login, Group.name)
                      .outerjoin(User, User.id == DeadlineExtension.user_id)
                      .outerjoin(Group, Group.id == DeadlineExtension.group_id)
                      .where(DeadlineExtension.feed_item_id == item.id).order_by(DeadlineExtension.id)).all()
    return [{"id": row.id, "item": item.id, "user": login, "group": name, "due_at": row.due_at,
             "base_due_at": item.due_at, "reason": row.reason, "created_at": row.created_at}
            for row, login, name in rows]


def remove_extension(db: Session, actor: audit.Actor, item: FeedItem, extension_id: int, now: datetime) -> dict:
    """Only an extension of this item: another id is extension_not_found, like a missing one."""
    row = db.get(DeadlineExtension, extension_id)
    if row is None or row.feed_item_id != item.id:
        raise FeedError(f"item {item.id} has no extension {extension_id}", "extension_not_found")
    return ext.remove(db, actor, row, now, "item", {"item": item.id, "title": item.title})


# ----------------------------------------------------------------------------- what the teacher sees
def item_summary(db: Session, item: FeedItem) -> dict:
    works = db.execute(select(AssignmentWork.status, func.count()).where(AssignmentWork.item_id == item.id)
                       .group_by(AssignmentWork.status)).all()
    rows = audience_rows(db, item)
    return {"id": item.id, "kind": item.kind, "state": item.state, "title": item.title,
            "contest_id": item.contest_id, "available_from": item.available_from, "due_at": item.due_at,
            "published_at": item.published_at, "created_at": item.created_at,
            "audience": {"groups": [r.group_id for r in rows if r.group_id is not None],
                         "users": [r.user_id for r in rows if r.user_id is not None]},
            "works": {status.value: n for status, n in works}, "warning": contest_warning(db, item)}


def item_detail(db: Session, item: FeedItem) -> dict:
    """item_summary with every setting, the contest, the audience by name and the number of extensions: what the
    admin API and `judgectl item show` show of one item. The row is read back first: values as stored (max_points
    with its two decimals), whether the item was just created, changed or read."""
    db.flush()
    db.refresh(item)
    out = item_summary(db, item)
    rows = audience_rows(db, item)
    groups = [db.get(Group, r.group_id) for r in rows if r.group_id is not None]
    users = [db.get(User, r.user_id) for r in rows if r.user_id is not None]
    contest = db.get(Contest, item.contest_id) if item.contest_id is not None else None
    extensions = db.execute(select(func.count()).select_from(DeadlineExtension)
                            .where(DeadlineExtension.feed_item_id == item.id)).scalar_one()
    out.update({
        "body_md": item.body_md, "after_due_statement": item.after_due_statement,
        "after_due_submit": item.after_due_submit, "grading": item.grading, "max_points": item.max_points,
        "answer_kind": item.answer_kind, "grades_release": item.grades_release, "updated_at": item.updated_at,
        "created_by": item.created_by,
        "contest": None if contest is None else {"slug": contest.slug, "title": contest.title,
                                                 "state": contest.state},
        "audience": {"groups": [{"id": gr.id, "name": gr.name, "archived": gr.archived_at is not None}
                                for gr in groups],
                     "users": [{"id": u.id, "login": u.login, "display_name": u.display_name} for u in users]},
        "extensions": extensions,
    })
    return out
