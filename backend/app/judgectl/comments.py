"""judgectl comment ...: comment threads of the class feed, as the console (D-072..D-076, docs/CLASSROOM.md §9,
docs/JUDGECTL.md).

The console reads like a teacher (classroom.comments.CONSOLE) and has no read marks: the inbox shows «awaiting
reply», never unread. It writes as the console (author null, the console user's name in the audit log). Items are
named by id, students by login, groups by name or #id. Comments are untrusted text: printed through
core.terminal_text, --json gives them as they are.
"""
from __future__ import annotations

from .. import groups as g
from ..auth.users import get_by_login
from ..classroom import comments, items
from .core import CliError, Context, emit_json, int4, read_input, show, terminal_text

FILE_LIMIT = 64 * 1024  # bytes of --file: room for 4000 characters of any script, so the service says too long


def _who(person) -> str:
    """An author or a deleter in the teacher view: the console, or the login (and the display name of an author)."""
    if person is None or person.get("console"):
        return "console"
    name = person.get("display_name")
    return person["login"] + (f" «{terminal_text(name)}»" if name else "")


def _whole_thread(db, item, student, now) -> dict:
    """The thread with every comment (the service gives pages of at most PAGE_MAX)."""
    def page(after=None):
        if student is None:
            return comments.shared_thread(db, comments.CONSOLE, item, now, after=after)
        return comments.private_thread(db, comments.CONSOLE, item, student, now, after=after)

    view = page()
    while view["next_after"] is not None:
        more = page(view["next_after"])
        view["comments"] += more["comments"]
        view["next_after"] = more["next_after"]
    return view


def _body(ctx: Context, args) -> str:
    if args.text is not None:
        return args.text
    data = read_input(ctx, args.file, FILE_LIMIT, "the comment")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError(f"{args.file}: not UTF-8 text (byte {exc.start})") from exc


def _where(card: dict) -> str:
    out = f"the {card['kind']} thread of item #{card['item']}"
    return out + (f" ({card['student']['login']})" if card["student"] else "")


# ----------------------------------------------------------------------------- commands
def comment_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.item) if args.item is not None else None
        group = g.get_group(db, args.group) if args.group else None
        page = comments.threads_query(db, comments.CONSOLE, ctx.clock(), item=item, group=group,
                                      awaiting=args.awaiting, limit=args.last)
    rows = page["items"]
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    show(ctx, [{"thread": r["thread_id"], "item": r["item"]["id"], "title": r["item"]["title"], "kind": r["kind"],
                "student": r["student"]["login"] if r["student"] else None, "comments": r["comments"],
                "deleted": r["deleted"], "last_at": r["last"]["at"] if r["last"] else None,
                "awaiting": r["awaiting_reply"]} for r in rows],
         [("thread", "THREAD"), ("item", "ITEM"), ("title", "TITLE"), ("kind", "KIND"), ("student", "STUDENT"),
          ("comments", "COMMENTS"), ("deleted", "DELETED"), ("last_at", "LAST"), ("awaiting", "AWAITING")])
    if page["next_cursor"] is not None:
        ctx.note(f"only the newest {len(rows)}: --last N shows more (at most {comments.PAGE_MAX})")
    return 0


def comment_show(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.item)
        student = get_by_login(db, args.student) if args.student else None
        view = _whole_thread(db, item, student, ctx.clock())
        title, kind, state = item.title, item.kind.value, item.state.value
    if ctx.json:
        emit_json(ctx, view)
        return 0
    whose = "shared thread" if student is None else (
        f"private thread of {view['student']['login']} «{terminal_text(view['student']['display_name'])}»")
    ctx.print(f"item #{args.item} {kind} [{state}] {terminal_text(title)}: {whose}")
    ctx.print(f"  comments of students: {'on' if view['comments_enabled'] else 'off'}; the console "
              + ("can write" if view["can_post"] else f"cannot write ({view['cannot_post']})"))
    if not view["comments"]:
        ctx.print("  no comments yet")
    for c in view["comments"]:
        ctx.print(f"#{c['id']} {ctx.time(c['at'])} {_who(c['author'])}"
                  + (" (teacher)" if c["author"]["teacher"] and not c["author"]["console"] else ""))
        if c["deleted"]:
            reason = f": {terminal_text(c['delete_reason'])}" if c["delete_reason"] else ""
            ctx.print(f"  [deleted {ctx.time(c['deleted_at'])} by {_who(c['deleted_by'])}{reason}]")
        for line in terminal_text(c["body"], multiline=True).split("\n"):
            ctx.print(f"    | {line}")
    return 0


def comment_post(ctx: Context, args) -> int:
    body = _body(ctx, args)
    with ctx.session() as db:
        item = items.get_item(db, args.item)
        student = get_by_login(db, args.student) if args.student else None
        comment = comments.teacher_post(db, ctx.actor, item, ctx.clock(), body, student=student)
        card = comments.comment_card(db, comments.CONSOLE, comment)
    if ctx.json:
        emit_json(ctx, card)
        return 0
    ctx.print(f"comment #{card['id']} in {_where(card)}")
    return 0


def comment_delete(ctx: Context, args) -> int:
    with ctx.session() as db:
        comment = comments.delete_comment(db, ctx.actor, args.id, ctx.clock(), reason=args.reason)
        card = comments.comment_card(db, comments.CONSOLE, comment)
    if ctx.json:
        emit_json(ctx, card)
        return 0
    ctx.print(f"comment #{card['id']} of {_who(card['author'])} deleted from {_where(card)}; teachers still see "
              "its text")
    return 0


# ----------------------------------------------------------------------------- the parser
def add_parsers(groups, command) -> None:
    """`comment` in the parser of cli.py (`command` is its helper)."""
    comment = groups.add_parser("comment", help="comment threads of the class feed").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    p = command(comment, "list", comment_list, "threads, the one with the newest comment first", json=True)
    p.add_argument("--item", type=int4, help="threads of one item")
    p.add_argument("--group", help="private threads of its current members, shared threads of items addressed to it "
                                   "(name or #id)")
    waits = p.add_mutually_exclusive_group()
    waits.add_argument("--awaiting", dest="awaiting", action="store_true", default=None,
                       help="the last comment (not deleted) is a student's")
    waits.add_argument("--answered", dest="awaiting", action="store_false", help="the last comment is a teacher's")
    p.add_argument("--last", type=int4, default=50, help=f"at most this many threads (up to {comments.PAGE_MAX})")
    p = command(comment, "show", comment_show, "a whole thread, deleted comments marked", json=True)
    p.add_argument("item", type=int4)
    p.add_argument("--student", help="login: the private thread of this student (assignments)")
    p = command(comment, "post", comment_post, "write as the console: shared thread, or --student's private one",
                json=True)
    p.add_argument("item", type=int4)
    p.add_argument("--student", help="login: the private thread of this student (assignments)")
    text = p.add_mutually_exclusive_group(required=True)
    text.add_argument("--text", help="the comment (Markdown)")
    text.add_argument("--file", help="the comment from a UTF-8 file, or - for stdin")
    p = command(comment, "delete", comment_delete, "delete a comment (soft: teachers still see the text)",
                json=True)
    p.add_argument("id", type=int4)
    p.add_argument("--reason", help="why, at most 200 characters (teachers see it)")
