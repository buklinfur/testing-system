"""judgectl item ... / work ...: the class feed and manual assignments (D-059, docs/CLASSROOM.md, docs/JUDGECTL.md).

Items are named by id, students by login, groups by name or #id; times without an offset are in JUDGE_TIMEZONE.
Everything a student or a teacher typed (titles, statements, answers, comments, names) is printed through
core.terminal_text: control characters never reach the terminal as such. --json gives the data as it is.
"""
from __future__ import annotations

from .. import groups as g
from ..auth.users import get_by_login
from ..blobstore import BlobStore
from ..classroom import items, works
from ..contests import service as contests
from ..models import DeadlineExtension
from ..timeutil import parse_local
from .core import CliError, Context, confirm, emit_json, read_input, show, terminal_text

BODY_LIMIT = 1024 * 1024  # like the JSON body of the admin API


def _time(ctx: Context, text: str, flag: str):
    try:
        return parse_local(text, ctx.tz)
    except ValueError as exc:
        raise CliError(f"{flag} {text!r}: not a time like 2026-11-17 23:59:59 (JUDGE_TIMEZONE) or with Z / +07:00") \
            from exc


def _fields(ctx: Context, args, editing: bool = False) -> dict:
    """The item fields the command line gives (only those given: an edit changes nothing else)."""
    out = {}
    if args.title is not None:
        out["title"] = args.title
    if args.body_file is not None:
        out["body_md"] = read_input(ctx, args.body_file, BODY_LIMIT, "the text").decode("utf-8", errors="replace")
    if args.available_from is not None:
        out["available_from"] = _time(ctx, args.available_from, "--available-from")
    if args.due is not None:
        out["due_at"] = _time(ctx, args.due, "--due")
    if editing:
        for flag, name in (("no_due", "due_at"), ("no_available_from", "available_from")):
            if getattr(args, flag):
                if name in out:
                    raise CliError(f"--{flag.replace('_', '-')} and a new {name} at once")
                out[name] = None
    for arg, name in (("after_due_statement", "after_due_statement"), ("after_due_submit", "after_due_submit"),
                      ("grading", "grading"), ("max_points", "max_points"), ("answer", "answer_kind"),
                      ("release", "grades_release")):
        if getattr(args, arg) is not None:
            out[name] = getattr(args, arg)
    if editing and out.get("grading") not in (None, "points") and "max_points" not in out:
        out["max_points"] = None  # another scale drops max_points
    return out


def _audience(db, args) -> tuple[list, list]:
    groups = [g.get_group(db, ref) for ref in args.group or []]
    users = [get_by_login(db, login) for login in args.student or []]
    return groups, users


def _works_text(summary: dict) -> str:
    return ", ".join(f"{status} {n}" for status, n in sorted(summary["works"].items())) or "-"


def _row(summary: dict) -> dict:
    aud = summary["audience"]
    return {**summary, "works_text": _works_text(summary),
            "audience_text": f"{len(aud['groups'])} gr, {len(aud['users'])} st"}


def _warn(ctx: Context, data: dict) -> None:
    if data.get("warning"):
        ctx.note(f"warning: {terminal_text(data['warning'])}")


def _print_item(ctx: Context, d: dict) -> None:
    ctx.print(f"#{d['id']} {d['kind'].value} [{d['state'].value}] {terminal_text(d['title'])}")
    if d["contest"]:
        ctx.print(f"  contest: {d['contest']['slug']} ({d['contest']['state'].value})")
    ctx.print(f"  available from: {ctx.time(d['available_from'])}  due: {ctx.time(d['due_at'])}  "
              f"published: {ctx.time(d['published_at'])}")
    if d["grading"] is not None:
        scale = d["grading"].value + (f" (max {d['max_points']})" if d["max_points"] is not None else "")
        ctx.print(f"  after due: statement {d['after_due_statement'].value}, submit {d['after_due_submit'].value}")
        ctx.print(f"  grading: {scale}, answer: {d['answer_kind'].value}, grades: {d['grades_release'].value}")
    groups = ", ".join(terminal_text(x["name"]) + (" (archived)" if x["archived"] else "")
                       for x in d["audience"]["groups"]) or "-"
    users = ", ".join(x["login"] for x in d["audience"]["users"]) or "-"
    ctx.print(f"  audience: groups {groups}; students {users}")
    ctx.print(f"  extensions: {d['extensions']}  works: {_works_text(d)}")
    if d["body_md"]:
        ctx.print("  text:")
        for line in terminal_text(d["body_md"], multiline=True).split("\n"):
            ctx.print(f"    {line}")


# ----------------------------------------------------------------------------- item
def item_create(ctx: Context, args) -> int:
    fields = _fields(ctx, args)
    title = fields.pop("title")
    body = fields.pop("body_md", "")
    now = ctx.clock()
    with ctx.session() as db:
        contest = contests.get_contest(db, args.contest) if args.contest else None
        item = items.create_item(db, ctx.actor, now, kind=args.kind, title=title, body_md=body, contest=contest,
                                 **fields)
        groups, users = _audience(db, args)
        if groups or users:
            items.set_audience(db, ctx.actor, item, now, groups=groups, users=users)
        data = items.item_detail(db, item)
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"item #{data['id']} created: {data['kind'].value}, draft (publish: judgectl item publish {data['id']})")
    _warn(ctx, data)
    return 0


def item_edit(ctx: Context, args) -> int:
    fields = _fields(ctx, args, editing=True)
    if not fields:
        raise CliError("nothing to change: give at least one field")
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        changed = items.update_item(db, ctx.actor, item, ctx.clock(), **fields)
        data = items.item_detail(db, item)
    if ctx.json:
        emit_json(ctx, {"item": data, "changed": changed})
        return 0
    ctx.print(f"item #{args.id}: " + (f"changed {', '.join(changed)}" if changed else "nothing changed"))
    return 0


def _state(ctx: Context, args, func, done: str, same: str) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        changed = func(db, ctx.actor, item, ctx.clock())
        data = items.item_detail(db, item)
    ctx.print(f"item #{args.id}: {done if changed else same}")
    _warn(ctx, data)
    return 0


def item_publish(ctx: Context, args) -> int:
    return _state(ctx, args, items.publish, "published", "already published")


def item_archive(ctx: Context, args) -> int:
    return _state(ctx, args, items.archive, "archived", "already archived")


def item_unarchive(ctx: Context, args) -> int:
    return _state(ctx, args, items.unarchive, "published again", "not archived")


def item_delete(ctx: Context, args) -> int:
    confirm(ctx, str(args.id), f"deleting item #{args.id}", args.yes)
    with ctx.session() as db:
        details = items.delete_item(db, ctx.actor, items.get_item(db, args.id), ctx.clock())
    ctx.print(f"item #{args.id} deleted ({details['kind']}: {terminal_text(details['title'])})")
    return 0


def item_list(ctx: Context, args) -> int:
    states = [args.state] if args.state else None if args.all else ["draft", "published"]
    with ctx.session() as db:
        group = g.get_group(db, args.group) if args.group else None
        query = items.items_query(group=group, kind=args.kind, states=states)
        rows = [items.item_summary(db, item) for item in db.execute(query.order_by(*items.feed_order())).scalars()]
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    show(ctx, [_row(r) for r in rows], [("id", "ID"), ("kind", "KIND"), ("state", "STATE"), ("title", "TITLE"),
                                        ("available_from", "FROM"), ("due_at", "DUE"), ("audience_text", "AUDIENCE"),
                                        ("works_text", "WORKS")])
    return 0


def item_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        data = items.item_detail(db, items.get_item(db, args.id))
    if ctx.json:
        emit_json(ctx, data)
        return 0
    _print_item(ctx, data)
    _warn(ctx, data)
    return 0


def item_audience(ctx: Context, args) -> int:
    if not args.group and not args.student:
        raise CliError("name at least one --group or --student")
    func = items.add_audience if args.action == "add" else items.remove_audience
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        groups, users = _audience(db, args)
        result = func(db, ctx.actor, item, ctx.clock(), groups=groups, users=users)
        data = items.item_detail(db, item)
    if ctx.json:
        emit_json(ctx, {**result, "item": data})
        return 0
    parts = [f"{k} {', '.join(terminal_text(x) for x in v)}" for k, v in result.items() if v]
    ctx.print(f"item #{args.id}: " + ("; ".join(parts) or "audience unchanged"))
    _warn(ctx, data)
    return 0


def item_extension_add(ctx: Context, args) -> int:
    due = _time(ctx, args.due, "--due")
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        user = get_by_login(db, args.user) if args.user else None
        group = g.get_group(db, args.group) if args.group else None
        row, replaced = items.add_extension(db, ctx.actor, item, due, ctx.clock(), user=user, group=group,
                                            reason=args.reason)
        ext_id = row.id
        who = f"student {user.login}" if user else f"group {terminal_text(group.name)}"
    if ctx.json:
        emit_json(ctx, {"id": ext_id, "item": args.id, "due_at": due, "replaced": replaced})
        return 0
    ctx.print(f"item #{args.id}: {who} until {ctx.time(due)} (#{ext_id})"
              + (f", was {ctx.time(replaced)}" if replaced else ""))
    return 0


def item_extension_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = items.list_extensions(db, items.get_item(db, args.id))
    show(ctx, rows, [("id", "ID"), ("user", "STUDENT"), ("group", "GROUP"), ("due_at", "DUE"),
                     ("base_due_at", "BASE DUE"), ("reason", "REASON")])
    return 0


def item_extension_remove(ctx: Context, args) -> int:
    with ctx.session() as db:
        row = db.get(DeadlineExtension, args.eid)
        if row is not None and row.feed_item_id is None:
            raise CliError(f"extension {args.eid} is of a contest problem: judgectl extension remove {args.eid}")
        if row is None:
            raise CliError(f"no extension {args.eid}")
        item = items.get_item(db, row.feed_item_id)
        details = items.remove_extension(db, ctx.actor, item, args.eid, ctx.clock())
    who = f"student {details['user']}" if "user" in details else f"group {terminal_text(details['group'])}"
    ctx.print(f"item #{item.id}: extension of {who} removed")
    return 0


# ----------------------------------------------------------------------------- work
def _grade_text(grade) -> str:
    if grade is None:
        return "-"
    if grade.get("score") is not None:
        return str(grade["score"])
    if grade.get("passed") is not None:
        return "pass" if grade["passed"] else "fail"
    return "accepted"


def work_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.item) if args.item is not None else None
        group = g.get_group(db, args.group) if args.group else None
        page = works.review_queue(db, ctx.clock(), item=item, group=group, status=args.status, late=args.late,
                                  missing=True if args.missing else None, limit=args.last)
    rows = page["items"]
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    show(ctx, [{**r, "grade_text": _grade_text(r["grade"]),
                "released": None if r["grade"] is None else r["grade"]["released"]} for r in rows],
         [("item", "ITEM"), ("login", "LOGIN"), ("status", "STATUS"), ("late", "LATE"), ("missing", "MISSING"),
          ("submissions", "HAND-INS"), ("last_submitted_at", "LAST"), ("grade_text", "GRADE"),
          ("released", "RELEASED")])
    if page["next_cursor"] is not None:
        ctx.note(f"only the newest {args.last}: --last N shows more")
    return 0


def _card(ctx: Context, db, item_id: int, login: str):
    item = items.get_item(db, item_id)
    student = get_by_login(db, login)
    card = works.work_card(db, BlobStore(ctx.settings.blob_dir), item, student, ctx.clock())
    if card is None:
        raise CliError(f"{student.login} is not addressed by item {item.id} and has no work in it (work_not_found)")
    return item, student, card


def work_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        _item, _student, card = _card(ctx, db, args.id, args.login)
    if ctx.json:
        emit_json(ctx, card)
        return 0
    sees = card["student_sees"]
    ctx.print(f"item #{card['item']} {card['login']}: {card['status']}" + (", missing" if card["missing"] else "")
              + (f" (since {ctx.time(card['status_at'])})" if card["status_at"] else ""))
    ctx.print(f"  the student sees: {sees['status'] or 'assigned'}, grade {_grade_text(sees['grade'])}"
              + (", late" if sees["late"] else ""))
    for e in card["events"]:
        if e["kind"] == "submission":
            ctx.print(f"  #{e['id']} {ctx.time(e['at'])} hand-in" + (" (late)" if e["late"] else ""))
            if e["text"] is not None:
                for line in terminal_text(e["text"], multiline=True).split("\n"):
                    ctx.print(f"    | {line}")
        else:
            what = e["action"]
            if what == "graded":
                what += " " + _grade_text(e)
            elif what == "released":
                what += f" #{e['release_of']}"
            by = f"admin #{e['reviewer_id']}" if e["reviewer_id"] else "console"
            ctx.print(f"  #{e['id']} {ctx.time(e['at'])} {what} ({by})")
            if e["comment"]:
                for line in terminal_text(e["comment"], multiline=True).split("\n"):
                    ctx.print(f"    > {line}")
    return 0


def work_grade(ctx: Context, args) -> int:
    passed = True if args.passed == "pass" else False if args.passed == "fail" else None
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        student = get_by_login(db, args.login)  # without work and not addressed: refused by the service
        result = works.grade_work(db, ctx.actor, item, student, ctx.clock(), score=args.score, passed=passed,
                                  comment=args.comment, release=args.release)
    if ctx.json:
        emit_json(ctx, result)
        return 0
    shown = args.score if args.score is not None else {"pass": "pass", "fail": "fail"}.get(args.passed, "accepted")
    ctx.print(f"item #{item.id} {student.login}: graded {shown}"
              + (", released" if result["released"] else ", seen at once" if result["seen_at_once"] else ""))
    if not result["released"] and not result["seen_at_once"]:
        ctx.note(f"the student does not see it yet: judgectl work release {item.id} {student.login}")
    return 0


def work_return(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        student = get_by_login(db, args.login)
        result = works.return_work(db, ctx.actor, item, student, ctx.clock(), args.comment)
    if ctx.json:
        emit_json(ctx, result)
        return 0
    ctx.print(f"item #{item.id} {student.login}: returned for rework")
    return 0


def work_release(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        students = [get_by_login(db, login) for login in args.logins] if args.logins else None
        results = works.release_grades(db, ctx.actor, item, ctx.clock(), students)
    if ctx.json:
        emit_json(ctx, results)
        return 0
    show(ctx, results, [("login", "LOGIN"), ("result", "RESULT")])
    released = sum(r["result"] == "released" for r in results)
    ctx.note(f"item #{args.id}: released {released} of {len(results)}")
    return 0


# ----------------------------------------------------------------------------- the parser
def add_parsers(groups, command) -> None:
    """`item` and `work` in the parser of cli.py (`command` is its helper)."""
    item = groups.add_parser("item", help="the class feed: assignments and links to contests").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")

    def fields(p, editing=False):
        p.add_argument("--title", required=not editing)
        p.add_argument("--body-file", help="the text in Markdown (a file, or - for stdin)")
        p.add_argument("--available-from", help="before it students do not see the item (JUDGE_TIMEZONE or offset)")
        p.add_argument("--due", help="the deadline of an assignment, e.g. '2026-11-17 23:59:59'")
        p.add_argument("--after-due-statement", choices=["visible", "hidden"])
        p.add_argument("--after-due-submit", choices=["no", "late", "yes"])
        p.add_argument("--grading", choices=["points", "pass_fail", "none"])
        p.add_argument("--max-points", help="with --grading points, e.g. 5 or 10.5")
        p.add_argument("--answer", choices=["mark", "text"], help="mark: just «handed in»; text: a text answer")
        p.add_argument("--release", choices=["immediate", "manual"], help="when students see grades")
        if editing:
            p.add_argument("--no-due", action="store_true", help="drop the deadline")
            p.add_argument("--no-available-from", action="store_true", help="available at once")

    p = command(item, "create", item_create, "a new item (a draft): an assignment or a link to a contest", json=True)
    p.add_argument("--kind", required=True, choices=["assignment", "contest"])
    p.add_argument("--contest", help="slug of the contest (kind contest)")
    p.add_argument("--group", action="append", help="addressed group (name or #id); repeat for more")
    p.add_argument("--student", action="append", help="addressed student (login); repeat for more")
    fields(p)
    p = command(item, "edit", item_edit, "change the fields given; the others stay", json=True)
    p.add_argument("id", type=int)
    fields(p, editing=True)
    command(item, "publish", item_publish, "publish (with a later --available-from: shown from then)") \
        .add_argument("id", type=int)
    command(item, "archive", item_archive, "gone from the feed; students keep their work and grades") \
        .add_argument("id", type=int)
    command(item, "unarchive", item_unarchive, "back to the feed").add_argument("id", type=int)
    p = command(item, "delete", item_delete, "delete a draft without work and extensions")
    p.add_argument("id", type=int)
    p.add_argument("--yes", action="store_true", help="no confirmation (scripts)")
    p = command(item, "list", item_list, "items, newest first (drafts and published unless --all / --state)",
                json=True)
    p.add_argument("--group", help="addressed to this group (name or #id)")
    p.add_argument("--kind", choices=["assignment", "contest"])
    p.add_argument("--state", choices=["draft", "published", "archived"])
    p.add_argument("--all", action="store_true", help="archived ones too")
    command(item, "show", item_show, "settings, audience, text, works", json=True).add_argument("id", type=int)

    audience = item.add_parser("audience", help="who an item is addressed to").add_subparsers(
        dest="aud_cmd", required=True, metavar="COMMAND")
    for name, text in (("add", "address groups or students"), ("remove", "stop addressing them")):
        p = command(audience, name, item_audience, text, json=True)
        p.set_defaults(action=name)
        p.add_argument("id", type=int)
        p.add_argument("--group", action="append", help="name or #id; repeat for more")
        p.add_argument("--student", action="append", help="login; repeat for more")

    extension = item.add_parser("extension", help="later deadlines of an assignment").add_subparsers(
        dest="ext_cmd", required=True, metavar="COMMAND")
    p = command(extension, "add", item_extension_add,
                "a later due moment for one student or group (again for the same one: replaces it)", json=True)
    p.add_argument("id", type=int)
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="login of the student")
    who.add_argument("--group", help="the group (name or #id)")
    p.add_argument("--due", required=True, help="e.g. '2026-11-17 23:59:59' (JUDGE_TIMEZONE) or with Z / +07:00")
    p.add_argument("--reason", help="a short note (at most 200 characters)")
    command(extension, "list", item_extension_list, "extensions of an item", json=True).add_argument("id", type=int)
    command(extension, "remove", item_extension_remove, "remove an extension by id").add_argument("eid", type=int)

    work = groups.add_parser("work", help="students' work on assignments: review, grades").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    p = command(work, "list", work_list, "the review queue, newest first (with --item: every addressee)", json=True)
    p.add_argument("--item", type=int, help="one assignment: every addressee, those without work too")
    p.add_argument("--group", help="current members of the group (name or #id)")
    p.add_argument("--status", choices=list(works.QUEUE_STATUSES))
    late = p.add_mutually_exclusive_group()
    late.add_argument("--late", dest="late", action="store_true", default=None, help="latest hand-in late")
    late.add_argument("--not-late", dest="late", action="store_false", help="latest hand-in on time")
    p.add_argument("--missing", action="store_true", help="«не сдано»: nothing handed in, deadline passed (--item)")
    p.add_argument("--last", type=int, default=50)
    p = command(work, "show", work_show, "every hand-in with its text, every review, what the student sees",
                json=True)
    p.add_argument("id", type=int)
    p.add_argument("login")
    p = command(work, "grade", work_grade, "grade (also without a hand-in); --release: the student sees it now",
                json=True)
    p.add_argument("id", type=int)
    p.add_argument("login")
    scale = p.add_mutually_exclusive_group(required=True)
    scale.add_argument("--score", help="points, e.g. 8 or 4.5 (grading points)")
    scale.add_argument("--pass", dest="passed", action="store_const", const="pass", help="grading pass_fail")
    scale.add_argument("--fail", dest="passed", action="store_const", const="fail", help="grading pass_fail")
    scale.add_argument("--accept", dest="passed", action="store_const", const="accept", help="grading none")
    p.add_argument("--comment")
    p.add_argument("--release", action="store_true", help="release it at once (grades_release manual)")
    p = command(work, "return", work_return, "back for rework with a comment (seen at once)", json=True)
    p.add_argument("id", type=int)
    p.add_argument("login")
    p.add_argument("--comment", required=True)
    p = command(work, "release", work_release, "release the current grades (of every work, or of these students)",
                json=True)
    p.add_argument("id", type=int)
    p.add_argument("logins", nargs="*", metavar="login")
