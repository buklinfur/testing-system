"""judgectl submission ...: lists, the admin view of one submission, and rejudges (R33; docs/QUEUE.md for what a
rejudge is). The same services as /api/admin/submissions (app/submissions/)."""
from __future__ import annotations

from .. import groups as g
from ..auth.users import get_by_login
from ..blobstore import BlobStore
from ..models import Submission
from ..submissions import rejudge, view
from ..submissions.view import Filters, ViewError
from .core import CliError, Context, emit_json, show

SELECTORS = ("id", "contest", "contest_problem_id", "problem_version")


def _people(db, args) -> dict:
    out = {}
    if getattr(args, "user", None):
        out["user_id"] = get_by_login(db, args.user).id
    if getattr(args, "group", None):
        out["group_id"] = g.get_group(db, args.group).id
    return out


def _verdicts(text) -> list:
    return [v.strip().upper() for v in (text or "").split(",") if v.strip()]


def submission_list(ctx: Context, args) -> int:
    """Newest first, the same filters as GET /api/admin/submissions."""
    with ctx.session() as db:
        f = Filters(contest=args.contest, label=args.label, problem=args.problem, verdicts=_verdicts(args.verdicts),
                    status=args.status, late=args.late, **_people(db, args))
        try:
            query = view.submissions_query(db, f).order_by(Submission.id.desc()).limit(args.last)
        except ViewError as exc:
            raise CliError(str(exc)) from exc
        rows = []
        for row in db.execute(query).all():
            item = view.summary(row)
            j = item.pop("judgement") or {}
            rows.append({**item, "user": item["user"]["login"], "where": f"{item['contest']} {item['label']}",
                         "status": j.get("status"), "verdict": j.get("verdict"), "score": j.get("score")})
    show(ctx, rows, [("id", "ID"), ("created_at", "AT"), ("user", "USER"), ("where", "PROBLEM"),
                     ("language", "LANG"), ("status", "STATUS"), ("verdict", "VERDICT"), ("score", "SCORE"),
                     ("late", "LATE")])
    return 0


def submission_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        try:
            data = view.detail(db, BlobStore(ctx.settings.blob_dir), args.id)
        except ViewError as exc:
            raise CliError(str(exc)) from exc
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"submission {data['id']}: {data['user']['login']}, {data['contest']} {data['label']} ({data['problem']}), "
              f"{data['language'] or 'answers'}{' archive' if data['archive'] else ''}, {ctx.time(data['created_at'])}"
              + (", late" if data["late"] else ""))
    show(ctx, data["judgements"], [("id", "JUDGEMENT"), ("reason", "REASON"), ("status", "STATUS"),
                                   ("verdict", "VERDICT"), ("score", "SCORE"), ("version", "V"), ("current", "CURRENT"),
                                   ("attempt", "ATT"), ("worker", "WORKER"), ("error", "ERROR")])
    if data["tests"]:
        ctx.print("tests of the current judgement (admin view: hidden tests included):")
        rows = [{**t, "checker_message": (t["checker_message"] or "")[:60] or None} for t in data["tests"]]
        show(ctx, rows, [("test", "TEST"), ("group", "GROUP"), ("verdict", "VERDICT"), ("time_ms", "MS"),
                         ("memory_kb", "KB"), ("tl_kind", "TL"), ("visible_to_student", "OPEN"),
                         ("checker_message", "CHECKER")])
    return 0


def submission_rejudge(ctx: Context, args) -> int:
    """One submission (--id), one problem of a contest (--contest + --problem, or --contest-problem-id), or every
    contest problem pinned to a version (--problem-version <slug>@<v>: visible and hidden ones). --dry-run counts."""
    given = [n for n in SELECTORS if getattr(args, n, None) is not None]
    if len(given) != 1 or (args.contest is None) != (args.problem is None):
        raise CliError("give one of: --id N | --contest <slug> --problem <label> | --contest-problem-id N | "
                       "--problem-version <slug>@<version>")
    verdicts = _verdicts(args.verdicts)
    if verdicts and args.id is not None:
        raise CliError("--verdicts selects among many submissions: not with --id")
    with ctx.session() as db:
        try:
            if args.id is not None:
                if args.dry_run:
                    raise CliError("--dry-run counts many submissions: not with --id")
                result = rejudge.one(db, ctx.actor, args.id, ctx.clock())
            else:
                f = Filters(contest=args.contest, label=args.problem, contest_problem_id=args.contest_problem_id,
                            problem_version=args.problem_version, verdicts=verdicts)
                result = ({"affected": rejudge.count(db, f), "dry_run": True} if args.dry_run
                          else rejudge.run(db, ctx.actor, f, ctx.clock()))
        except ViewError as exc:
            raise CliError(str(exc)) from exc
    if ctx.json:
        emit_json(ctx, result)
    elif args.id is not None:
        ctx.print(f"submission {args.id}: {result['kind']} (judgement {result['judgement']})")
    elif args.dry_run:
        ctx.print(f"{result['affected']} submission(s) would be rejudged (dry run: nothing was queued)")
    else:
        ctx.print(f"{result['affected']} submission(s) of {len(result['contest_problems'])} contest problem(s): "
                  f"{result['created']} rejudges queued, {result['retargeted']} queued ones retargeted, "
                  f"{result['already_running']} already running")
    return 0
