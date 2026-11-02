"""judgectl extension ...: later deadlines of contest problems for one student or one group (D-023, docs/CONTESTS.md)."""
from __future__ import annotations

from .. import groups as g
from ..auth.users import get_by_login
from ..contests import deadlines, service
from ..timeutil import parse_local
from .core import CliError, Context, emit_json, show


def _due(ctx: Context, text: str):
    try:
        return parse_local(text, ctx.tz)
    except ValueError as exc:
        raise CliError(f"--due {text!r}: not a time like 2026-11-17 23:59:59 (JUDGE_TIMEZONE) or with Z / +07:00") \
            from exc


def extension_add(ctx: Context, args) -> int:
    due = _due(ctx, args.due)
    with ctx.session() as db:
        contest = service.get_contest(db, args.contest)
        user = get_by_login(db, args.user) if args.user else None
        group = g.get_group(db, args.group) if args.group else None
        row, replaced = deadlines.add_extension(db, ctx.actor, contest, args.label, due, ctx.clock(), user=user,
                                                group=group, reason=args.reason)
        ext_id = row.id
        who = f"student {user.login}" if user else f"group {group.name}"
    if ctx.json:
        emit_json(ctx, {"id": ext_id, "due_at": due, "replaced": replaced})
        return 0
    ctx.print(f"{args.contest} {args.label}: {who} until {ctx.time(due)} (#{ext_id})"
              + (f", was {ctx.time(replaced)}" if replaced else ""))
    return 0


def extension_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = deadlines.list_extensions(db, service.get_contest(db, args.contest), args.label)
    show(ctx, rows, [("id", "ID"), ("label", "LABEL"), ("user", "STUDENT"), ("group", "GROUP"), ("due_at", "DUE"),
                     ("base_due_at", "BASE DUE"), ("reason", "REASON")])
    return 0


def extension_remove(ctx: Context, args) -> int:
    with ctx.session() as db:
        details = deadlines.remove_extension(db, ctx.actor, args.id, ctx.clock())
    who = f"student {details['user']}" if "user" in details else f"group {details['group']}"
    ctx.print(f"{details['contest']} {details['label']}: extension of {who} removed")
    return 0
