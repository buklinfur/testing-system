"""judgectl contest ...: contest.yaml in, states, pins, invite codes, participants (docs/CONTESTS.md)."""
from __future__ import annotations

from .. import groups as g
from ..contests import service
from ..contests.spec import parse
from ..models import Contest, ContestState
from .core import CliError, Context, emit_json, read_input, show

MAX_YAML = 1024 * 1024


def contest_apply(ctx: Context, args) -> int:
    """Make the contest what the file says; --dry-run shows the changes and rolls everything back."""
    try:
        text = read_input(ctx, args.file, MAX_YAML, "a contest.yaml").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(f"{args.file}: not UTF-8 text") from exc
    spec = parse(text, ctx.tz)
    with ctx.session() as db:
        step = db.begin_nested()
        result = service.apply(db, ctx.actor, spec, ctx.clock())
        slug, created = result.contest.slug, result.created
        if args.dry_run:
            step.rollback()
        else:
            step.commit()
    for warning in result.warnings:
        ctx.note(f"warning: {warning}")
    if ctx.json:
        emit_json(ctx, {"slug": slug, "created": created, "changes": result.changes, "warnings": result.warnings,
                        "dry_run": args.dry_run})
        return 0
    head = f"{slug}: " + ("would change" if args.dry_run else "changed")
    if not result.changes:
        ctx.print(f"{slug}: no changes")
    else:
        ctx.print(head + (" (dry run: nothing was written)" if args.dry_run else ""))
        for line in result.changes:
            ctx.print(f"  {line}")
    if created and not args.dry_run:
        ctx.note(f"the contest is a draft: check it (judgectl contest show {slug}), then judgectl contest publish {slug}")
    return 0


def contest_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        query = service.contests_query(include_archived=args.all).order_by(Contest.starts_at.desc())
        rows = [service.contest_summary(db, c) for c in db.execute(query).scalars()]
    show(ctx, rows, [("slug", "SLUG"), ("title", "TITLE"), ("state", "STATE"), ("timing_mode", "TIMING"),
                     ("starts_at", "STARTS"), ("ends_at", "ENDS"), ("problems", "PROBLEMS"), ("participants", "PEOPLE")])
    return 0


def contest_show(ctx: Context, args) -> int:
    """The same data as GET /api/admin/contests/<slug>: `spec` (contest.yaml) plus state, code and every row."""
    with ctx.session() as db:
        data = service.contest_detail(db, service.get_contest(db, args.slug))
    if ctx.json:
        emit_json(ctx, data)
        return 0
    spec = data["spec"]
    ctx.print(f"{spec['slug']}  {spec['title']}  [{data['state'].value}]")
    window = f"{ctx.time(spec['starts_at'])} .. {ctx.time(spec['ends_at']) if spec['ends_at'] else 'no end'}"
    if spec["timing_mode"] == "personal":
        window += f", personal timer {spec['personal_duration']}"
    ctx.print(f"  time ({ctx.settings.timezone}): {window}"
              + (f", freeze {ctx.time(spec['freeze_at'])}" if spec.get("freeze_at") else ""))
    ctx.print(f"  scoring {spec['scoring_mode']}, leaderboard {spec['leaderboard']}, "
              f"upsolving {'yes' if spec['allow_upsolving'] else 'no'}, "
              f"languages {', '.join(spec['languages']) if spec.get('languages') else 'all'}")
    ctx.print(f"  access: {spec['access']['mode']}"
              + (f", groups {', '.join(spec['access']['groups'])}" if spec["access"].get("groups") else "")
              + (f", invite code {data['invite_code']}" if data["invite_code"] else "")
              + f"; participants: {data['participants']}")
    show(ctx, data["rows"], [("label", "LABEL"), ("problem", "PROBLEM"), ("version", "V"), ("status", "STATUS"),
                             ("points", "POINTS"), ("hidden", "HIDDEN")])
    return 0


def _state(ctx: Context, args, state: ContestState) -> int:
    with ctx.session() as db:
        c = service.get_contest(db, args.slug)
        changed = service.set_state(db, ctx.actor, c, state, ctx.clock())
    ctx.print(f"{args.slug}: {state.value}" + ("" if changed else " already"))
    return 0


def contest_publish(ctx: Context, args) -> int:
    return _state(ctx, args, ContestState.published)


def contest_archive(ctx: Context, args) -> int:
    return _state(ctx, args, ContestState.archived)


def contest_pin(ctx: Context, args) -> int:
    with ctx.session() as db:
        c = service.get_contest(db, args.contest)
        result = service.pin(db, ctx.actor, c, args.label, args.version, args.rejudge, ctx.clock())
    if ctx.json:
        emit_json(ctx, result)
    else:
        ctx.print(f"{args.contest} {args.label}: v{result['from']} -> v{result['to']}"
                  + (f"; rejudge: {result['rejudge']}" if result["rejudge"] is not None else "; no rejudge"))
    return 0


def contest_code_regenerate(ctx: Context, args) -> int:
    with ctx.session() as db:
        code = service.regenerate_code(db, ctx.actor, service.get_contest(db, args.slug), ctx.clock())
    if ctx.json:
        emit_json(ctx, {"slug": args.slug, "invite_code": code})
    else:
        ctx.print(f"{args.slug}: new invite code {code} (the old one no longer works)")
    return 0


def contest_participants(ctx: Context, args) -> int:
    with ctx.session() as db:
        c = service.get_contest(db, args.slug)
        members = g.contest_member_ids(db, c)
        rows = [service.participant_out(c, p, u, members) for p, u in service.participants(db, c)]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("joined_at", "JOINED"), ("started_at", "STARTED"),
                     ("joined_via", "VIA"), ("access_now", "ACCESS")])
    return 0

