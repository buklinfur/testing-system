"""Groups (classes, D-020): create, rename, archive, members, and what a contest needs to know about them.

A group is the addressee of contests in the access modes groups / code_or_groups (contests/access.py). Access is
computed from the current membership: a student added later gets access at once, one removed loses it (their
history stays). Groups are archived, never deleted; an archived group gives no access and cannot be attached to
another contest. Only students are members.

Functions flush but never commit; the caller owns the transaction (judgectl: one per command). Every change is
written to audit_log. The student is always a User row taken from the session or the command line by the
caller, never an id from a request.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .auth import audit
from .models import (Contest, ContestGroup, DeadlineExtension, FeedItem, FeedItemAudience, Group, GroupMember, Material,
                     MaterialGroup, Role, User)

NAME_MAX = 64


class GroupError(ValueError):
    """A refusal with a stable `code` for the API (docs/ADMIN_API.md)."""

    def __init__(self, message: str, code: str = "group_error", **data):
        self.code = code
        self.data = data
        super().__init__(message)


def clean_name(raw: str) -> str:
    """'  7  Б ' -> '7 Б': inner spaces collapse, case is kept (uniqueness ignores it)."""
    name = " ".join((raw or "").split())
    if not name:
        raise GroupError("a group needs a name, e.g. 7Б", "group_name_invalid")
    if len(name) > NAME_MAX:
        raise GroupError(f"a group name is at most {NAME_MAX} characters", "group_name_invalid")
    return name


def _live_named(db: Session, name: str, but: Optional[int] = None) -> Optional[Group]:
    query = select(Group).where(func.lower(Group.name) == name.lower(), Group.archived_at.is_(None))
    if but is not None:
        query = query.where(Group.id != but)
    return db.execute(query).scalar_one_or_none()


def live_named(db: Session, raw_name: str) -> Optional[Group]:
    """The group not archived of this name (ignoring case and extra spaces), or None."""
    return _live_named(db, " ".join((raw_name or "").split()))


def archived_named(db: Session, raw_name: str) -> bool:
    name = " ".join((raw_name or "").split())
    return db.execute(select(exists().where(func.lower(Group.name) == name.lower(),
                                            Group.archived_at.is_not(None)))).scalar()


def why_kept_after_rollback(db: Session, group: Group) -> Optional[str]:
    """K3: a group a class import created goes with the import's rollback only while nothing else uses it: no
    other students (ever), no contest, no extension, no feed item addressed to it, no material of the library shown
    to it (4v.4). Returns why it stays, or None (it may be deleted)."""
    if db.execute(select(exists().where(GroupMember.group_id == group.id))).scalar():
        return "it has other students"
    if db.execute(select(exists().where(ContestGroup.group_id == group.id))).scalar():
        return "it is attached to a contest"
    if db.execute(select(exists().where(DeadlineExtension.group_id == group.id))).scalar():
        return "it has deadline extensions"
    if db.execute(select(exists().where(FeedItemAudience.group_id == group.id))).scalar():
        return "it is addressed by a feed item"
    if db.execute(select(exists().where(MaterialGroup.group_id == group.id))).scalar():
        return "the library shows materials to it"
    return None


def get_group(db: Session, ref: str) -> Group:
    """A group by `#id`, or by name (ignoring case): the group not archived, else the only archived one."""
    ref = (ref or "").strip()
    if ref.startswith("#") and ref[1:].isdigit():
        group = db.get(Group, int(ref[1:]))
        if group is None:
            raise GroupError(f"no group {ref}", "group_not_found")
        return group
    name = " ".join(ref.split())
    group = _live_named(db, name)
    if group is not None:
        return group
    archived = list(db.execute(select(Group).where(func.lower(Group.name) == name.lower())
                               .order_by(Group.id)).scalars())
    if len(archived) == 1:
        return archived[0]
    if archived:
        ids = ", ".join(f"#{g.id}" for g in archived)
        raise GroupError(f"several archived groups are named {name!r}: name one by id ({ids})", "group_ambiguous")
    raise GroupError(f"no group {name!r} (see: judgectl group list --all)", "group_not_found")


def create(db: Session, actor: audit.Actor, raw_name: str, now: datetime) -> Group:
    name = clean_name(raw_name)
    if _live_named(db, name) is not None:
        raise GroupError(f"there is already a group {name!r}", "group_name_taken")
    group = Group(name=name, created_at=now, created_by=actor.user_id)
    db.add(group)
    db.flush()
    audit.record(db, actor, "group.create", now, object_type="group", object_id=group.id, details={"name": name})
    return group


def rename(db: Session, actor: audit.Actor, group: Group, raw_name: str, now: datetime) -> bool:
    name = clean_name(raw_name)
    if name == group.name:
        return False
    if group.archived_at is None and _live_named(db, name, but=group.id) is not None:
        raise GroupError(f"there is already a group {name!r}", "group_name_taken")
    old, group.name = group.name, name
    db.flush()
    audit.record(db, actor, "group.rename", now, object_type="group", object_id=group.id,
                 details={"from": old, "to": name})
    return True


def contests_of(db: Session, group: Group) -> list[Contest]:
    return list(db.execute(select(Contest).join(ContestGroup, ContestGroup.contest_id == Contest.id)
                           .where(ContestGroup.group_id == group.id).order_by(Contest.slug)).scalars())


def feed_items_of(db: Session, group: Group) -> list[FeedItem]:
    """The feed items whose audience names the group (any state), by id."""
    return list(db.execute(select(FeedItem).join(FeedItemAudience, FeedItemAudience.item_id == FeedItem.id)
                           .where(FeedItemAudience.group_id == group.id).order_by(FeedItem.id)).scalars())


def materials_of(db: Session, group: Group) -> list[Material]:
    """The materials the library shows to the group (material_groups), by id."""
    return list(db.execute(select(Material).join(MaterialGroup, MaterialGroup.material_id == Material.id)
                           .where(MaterialGroup.group_id == group.id).order_by(Material.id)).scalars())


def archive(db: Session, actor: audit.Actor, group: Group, now: datetime) -> list[str]:
    """K6: the group stops giving access. Returns the contests it is attached to (they keep the attachment, it
    just gives nothing; contest.yaml can drop it). The feed items addressed to it (feed_items_of) and the materials
    the library shows to it (materials_of, 4v.4) stay so and give nothing either; the audit names them too."""
    attached = [c.slug for c in contests_of(db, group)]
    if group.archived_at is None:
        group.archived_at = now
        db.flush()
        audit.record(db, actor, "group.archive", now, object_type="group", object_id=group.id,
                     details={"name": group.name, "contests": attached,
                              "feed_items": [item.id for item in feed_items_of(db, group)],
                              "materials": [m.slug for m in materials_of(db, group)]})
    return attached


def unarchive(db: Session, actor: audit.Actor, group: Group, now: datetime) -> bool:
    if group.archived_at is None:
        return False
    if _live_named(db, group.name) is not None:
        raise GroupError(f"there is already a group {group.name!r}: rename one of them first", "group_name_taken")
    group.archived_at = None
    db.flush()
    audit.record(db, actor, "group.unarchive", now, object_type="group", object_id=group.id,
                 details={"name": group.name})
    return True


def _current(db: Session, group: Group, user_ids: Iterable[int]) -> dict:
    rows = db.execute(select(GroupMember).where(GroupMember.group_id == group.id,
                                                GroupMember.user_id.in_(list(user_ids)),
                                                GroupMember.removed_at.is_(None))).scalars()
    return {m.user_id: m for m in rows}


def add_members(db: Session, actor: audit.Actor, group: Group, users: list[User], now: datetime) -> dict:
    """Students only; an archived group takes nobody. Returns {"added": [...], "already": [...]} (logins)."""
    if group.archived_at is not None:
        raise GroupError(f"group {group.name!r} is archived: unarchive it first", "group_archived")
    not_students = [u.login for u in users if u.role != Role.student]
    if not_students:
        raise GroupError(f"only students are members of groups: {', '.join(not_students)}", "not_a_student",
                         logins=not_students)
    current = _current(db, group, [u.id for u in users])
    added, already = [], []
    for user in users:
        if user.id in current:
            already.append(user.login)
            continue
        db.add(GroupMember(group_id=group.id, user_id=user.id, added_at=now, added_by=actor.user_id))
        current[user.id] = True  # the same login twice on the command line
        added.append(user.login)
    db.flush()
    if added:
        audit.record(db, actor, "group.add_members", now, object_type="group", object_id=group.id,
                     details={"name": group.name, "logins": added})
    return {"added": added, "already": already}


def remove_members(db: Session, actor: audit.Actor, group: Group, users: list[User], now: datetime) -> dict:
    """Marks removed_at; the rows stay (who was in the class). Returns {"removed": [...], "not_members": [...]}."""
    current = _current(db, group, [u.id for u in users])
    removed, not_members = [], []
    for user in users:
        row = current.pop(user.id, None)
        if row is None:
            not_members.append(user.login)
            continue
        row.removed_at, row.removed_by = now, actor.user_id
        removed.append(user.login)
    db.flush()
    if removed:
        audit.record(db, actor, "group.remove_members", now, object_type="group", object_id=group.id,
                     details={"name": group.name, "logins": removed})
    return {"removed": removed, "not_members": not_members}


def members(db: Session, group: Group, with_history: bool = False) -> list:
    """(GroupMember, User) rows: current members, or every row ever with `with_history`."""
    query = select(GroupMember, User).join(User, User.id == GroupMember.user_id).where(GroupMember.group_id == group.id)
    if not with_history:
        query = query.where(GroupMember.removed_at.is_(None))
    return list(db.execute(query.order_by(User.login, GroupMember.id)).all())


def groups_query(include_archived: bool = False):
    query = select(Group)
    return query if include_archived else query.where(Group.archived_at.is_(None))


def group_summary(db: Session, group: Group) -> dict:
    people = db.execute(select(func.count()).select_from(GroupMember).where(
        GroupMember.group_id == group.id, GroupMember.removed_at.is_(None))).scalar_one()
    attached = db.execute(select(func.count()).select_from(ContestGroup)
                          .where(ContestGroup.group_id == group.id)).scalar_one()
    return {"id": group.id, "name": group.name, "archived_at": group.archived_at, "created_at": group.created_at,
            "members": people, "contests": attached}


# ----------------------------------------------------------------------------- what contests need
def in_contest_group(db: Session, contest: Contest, user: User) -> bool:
    """access.has_access's `in_group`: the student is now in a group not archived that the contest is attached to."""
    return db.execute(select(exists().where(
        GroupMember.user_id == user.id, GroupMember.removed_at.is_(None),
        GroupMember.group_id == ContestGroup.group_id, ContestGroup.contest_id == contest.id,
        ContestGroup.group_id == Group.id, Group.archived_at.is_(None)))).scalar()


def contest_member_ids(db: Session, contest: Contest) -> set[int]:
    """Ids of the students for whom in_contest_group is true (one query for a whole list of participants)."""
    return set(db.execute(select(GroupMember.user_id).join(ContestGroup, ContestGroup.group_id == GroupMember.group_id)
                          .join(Group, Group.id == GroupMember.group_id)
                          .where(ContestGroup.contest_id == contest.id, GroupMember.removed_at.is_(None),
                                 Group.archived_at.is_(None))).scalars())


def contest_groups(db: Session, contest: Contest) -> list[Group]:
    return list(db.execute(select(Group).join(ContestGroup, ContestGroup.group_id == Group.id)
                           .where(ContestGroup.contest_id == contest.id).order_by(Group.name, Group.id)).scalars())
