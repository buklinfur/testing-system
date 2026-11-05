"""judgectl group ...: classes and their students (docs/JUDGECTL.md, D-020)."""
from __future__ import annotations

from .. import groups as g
from ..auth.users import get_by_login
from ..models import Group
from .core import Context, emit_json, show


def _users(db, logins):
    return [get_by_login(db, login) for login in logins]


def group_create(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.create(db, ctx.actor, args.name, ctx.clock())
        name, group_id = group.name, group.id
    ctx.print(f"group {name} created (#{group_id})")
    return 0


def group_rename(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        old = group.name
        changed = g.rename(db, ctx.actor, group, args.name, ctx.clock())
        new = group.name
    ctx.print(f"group {old} -> {new}" if changed else f"group {old}: the same name")
    return 0


def group_archive(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        was_archived = group.archived_at is not None
        attached = g.archive(db, ctx.actor, group, ctx.clock())
        name = group.name
    ctx.print(f"group {name}: archived" + (" already" if was_archived else ""))
    if attached:
        ctx.note(f"it is attached to these contests and gives no access to them any more: {', '.join(attached)} "
                 "(drop it from access.groups of their contest.yaml if it should go)")
    return 0


def group_unarchive(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        changed = g.unarchive(db, ctx.actor, group, ctx.clock())
        name = group.name
    ctx.print(f"group {name}: " + ("not archived any more" if changed else "not archived"))
    return 0


def group_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = [g.group_summary(db, group)
                for group in db.execute(g.groups_query(args.all).order_by(Group.name, Group.id)).scalars()]
    show(ctx, rows, [("id", "ID"), ("name", "NAME"), ("members", "STUDENTS"), ("contests", "CONTESTS"),
                     ("archived_at", "ARCHIVED")])
    return 0


def group_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        data = g.group_summary(db, group)
        data["contests"] = [c.slug for c in g.contests_of(db, group)]
        data["students"] = [{"login": u.login, "display_name": u.display_name, "added_at": m.added_at,
                             "removed_at": m.removed_at} for m, u in g.members(db, group, args.history)]
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"#{data['id']} {data['name']}" + (f"  [archived {ctx.time(data['archived_at'])}]"
                                                 if data["archived_at"] else ""))
    ctx.print(f"  contests: {', '.join(data['contests']) or '-'}")
    columns = [("login", "LOGIN"), ("display_name", "NAME"), ("added_at", "ADDED")]
    show(ctx, data["students"], columns + ([("removed_at", "REMOVED")] if args.history else []))
    return 0


def group_add(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        result = g.add_members(db, ctx.actor, group, _users(db, args.logins), ctx.clock())
        name = group.name
    if ctx.json:
        emit_json(ctx, {"group": name, **result})
        return 0
    ctx.print(f"group {name}: added {len(result['added'])}" + (f": {', '.join(result['added'])}" if result["added"] else ""))
    if result["already"]:
        ctx.note(f"already in the group: {', '.join(result['already'])}")
    return 0


def group_remove(ctx: Context, args) -> int:
    with ctx.session() as db:
        group = g.get_group(db, args.group)
        result = g.remove_members(db, ctx.actor, group, _users(db, args.logins), ctx.clock())
        name = group.name
    if ctx.json:
        emit_json(ctx, {"group": name, **result})
        return 0
    ctx.print(f"group {name}: removed {len(result['removed'])}"
              + (f": {', '.join(result['removed'])}" if result["removed"] else ""))
    if result["not_members"]:
        ctx.note(f"not in the group: {', '.join(result['not_members'])}")
    return 0
