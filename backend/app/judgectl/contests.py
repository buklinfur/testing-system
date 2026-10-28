"""judgectl contest ...: contest.yaml in, states, pins, invite codes, participants (docs/CONTESTS.md)."""
from __future__ import annotations

from sqlalchemy import func, select

from ..contests import service
from ..contests.spec import parse
from ..models import Contest, ContestProblem, ContestState, Participation, Problem, ProblemVersion
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
        query = select(Contest).order_by(Contest.starts_at.desc())
        if not args.all:
            query = query.where(Contest.state != ContestState.archived)
        rows = []
        for c in db.execute(query).scalars():
            problems = db.execute(select(func.count()).select_from(ContestProblem).where(
                ContestProblem.contest_id == c.id, ContestProblem.hidden.is_(False))).scalar_one()
            people = db.execute(select(func.count()).select_from(Participation)
                                .where(Participation.contest_id == c.id)).scalar_one()
            rows.append({"slug": c.slug, "title": c.title, "state": c.state, "timing": c.timing_mode,
                         "starts_at": c.starts_at, "ends_at": c.ends_at, "problems": problems, "participants": people})
    show(ctx, rows, [("slug", "SLUG"), ("title", "TITLE"), ("state", "STATE"), ("timing", "TIMING"),
                     ("starts_at", "STARTS"), ("ends_at", "ENDS"), ("problems", "PROBLEMS"), ("participants", "PEOPLE")])
    return 0


def contest_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        c = service.get_contest(db, args.slug)
        rows = db.execute(select(ContestProblem, Problem.slug, ProblemVersion.version, ProblemVersion.status)
                          .join(Problem, Problem.id == ContestProblem.problem_id)
                          .join(ProblemVersion, ProblemVersion.id == ContestProblem.problem_version_id)
                          .where(ContestProblem.contest_id == c.id)
                          .order_by(ContestProblem.hidden, ContestProblem.position, ContestProblem.id)).all()
        problems = [{"label": cp.label, "problem": slug, "version": version, "status": status,
                     "points": None if cp.points is None else str(cp.points), "hidden": cp.hidden}
                    for cp, slug, version, status in rows]
        people = db.execute(select(func.count()).select_from(Participation)
                            .where(Participation.contest_id == c.id)).scalar_one()
        data = {"slug": c.slug, "title": c.title, "state": c.state, "timing_mode": c.timing_mode,
                "starts_at": c.starts_at, "ends_at": c.ends_at, "personal_duration":
                    None if c.personal_duration is None else int(c.personal_duration.total_seconds() // 60),
                "freeze_at": c.freeze_at, "scoring_mode": c.scoring_mode, "leaderboard": c.leaderboard,
                "join_policy": c.join_policy, "invite_code": c.invite_code if c.join_policy.value == "code" else None,
                "allow_upsolving": c.allow_upsolving, "languages": c.languages,
                "submit_cooldown_sec": c.submit_cooldown_sec,
                "max_submissions_per_problem": c.max_submissions_per_problem, "tests_after_end": c.tests_after_end,
                "participants": people, "problems": problems}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"{data['slug']}  {data['title']}  [{data['state'].value}]")
    window = f"{ctx.time(data['starts_at'])} .. {ctx.time(data['ends_at']) if data['ends_at'] else 'no end'}"
    if data["timing_mode"].value == "personal":
        window += f", personal timer {data['personal_duration']} min"
    ctx.print(f"  time ({ctx.settings.timezone}): {window}"
              + (f", freeze {ctx.time(data['freeze_at'])}" if data["freeze_at"] else ""))
    ctx.print(f"  scoring {data['scoring_mode'].value}, leaderboard {data['leaderboard'].value}, "
              f"upsolving {'yes' if data['allow_upsolving'] else 'no'}, "
              f"languages {', '.join(data['languages']) if data['languages'] else 'all'}")
    ctx.print(f"  join: {data['join_policy'].value}" + (f", invite code {data['invite_code']}" if data["invite_code"] else "")
              + f"; participants: {data['participants']}")
    show(ctx, data["problems"], [("label", "LABEL"), ("problem", "PROBLEM"), ("version", "V"), ("status", "STATUS"),
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
        rows = [{"login": u.login, "display_name": u.display_name, "joined_at": p.joined_at, "started_at": p.started_at}
                for p, u in service.participants(db, c)]
    show(ctx, rows, [("login", "LOGIN"), ("display_name", "NAME"), ("joined_at", "JOINED"), ("started_at", "STARTED")])
    return 0

