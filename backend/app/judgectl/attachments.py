"""judgectl item attach | link | attachments | detach | attachment-order, work file: a teacher's files and links of a
feed item and the files of students' work (D-078..D-081, docs/CLASSROOM.md §10, docs/JUDGECTL.md).

The console changes attachments as a teacher (classroom/attachments.py: the same rules, limits and audit as the admin
API) and reads every work. A file goes into the blob store through blobstore.Incoming: written into <blob root>/tmp,
taken into the store only when the service accepted it, removed otherwise. Names, URLs and titles are untrusted text:
printed through core.terminal_text, --json gives them as they are. A student's file is never printed to a terminal:
`work file` writes it to a new file (-o PATH, an existing one only with --force) or to a pipe (-o -).
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from ..auth.users import get_by_login
from ..blobstore import BlobStore, TooLarge
from ..classroom import attachments, items, works
from ..classroom.comments import CONSOLE
from ..classroom.items import FeedError
from .core import CliError, Context, emit_json, int4, show, terminal_text


def size_text(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def attachment_line(a: dict) -> str:
    """One attachment as judgectl prints it (item show, item attachments)."""
    if a["kind"] == "file":
        return f"#{a['id']} file {terminal_text(a['name'])} ({size_text(a['size'])})"
    title = f" «{terminal_text(a['title'])}»" if a["title"] else ""
    return f"#{a['id']} link {terminal_text(a['url'])}{title}"


def describe_all(db, item) -> list[dict]:
    return [attachments.describe(row) for row in attachments.list_attachments(db, item)]


def print_attachments(ctx: Context, rows: list[dict], indent: str = "  ") -> None:
    if not rows:
        ctx.print(f"{indent}no attachments")
    for a in rows:
        ctx.print(indent + attachment_line(a))


# ----------------------------------------------------------------------------- item
def receive(ctx: Context, args, limit: int):
    """The file argument (or stdin for `-`, which needs --name) as a finished Incoming of at most `limit` bytes;
    the caller closes it."""
    if args.file == "-" and args.name is None:
        raise CliError("a file from stdin needs --name (with its extension, e.g. --name task.pdf)")
    name = args.name if args.name is not None else os.path.basename(args.file)
    try:
        source = contextlib.nullcontext(ctx.stdin) if args.file == "-" else open(args.file, "rb")
    except OSError as exc:
        raise CliError(f"cannot read {args.file}: {exc.strerror} (judgectl runs as judge: give the file on stdin "
                       "with `-` and --name)") from exc
    upload = BlobStore(ctx.settings.blob_dir).incoming(name, limit)
    try:
        with source as stream:
            upload.copy_from(stream)
    except TooLarge as exc:
        upload.close()
        raise FeedError(f"{args.file}: larger than {(limit - 1) // (1024 * 1024)} MB (JUDGE_ATTACHMENT_MAX_MB)",
                        "file_too_large") from exc
    except BaseException:
        upload.close()
        raise
    return upload


def item_attach(ctx: Context, args) -> int:
    files = ctx.settings.files
    # one byte over the limit is enough for the service to say file_too_large itself
    upload = receive(ctx, args, files.attachment_max_bytes + 1)
    with upload, ctx.session() as db:
        item = items.get_item(db, args.id)
        row = attachments.add_file(db, ctx.actor, item, upload, ctx.clock(),
                                   limits=attachments.teacher_limits(files))
        data = attachments.describe(row)
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"item #{args.id}: attached {attachment_line(data)}")
    return 0


def item_link(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        row = attachments.add_link(db, ctx.actor, item, args.url, ctx.clock(), title=args.title,
                                   limits=attachments.teacher_limits(ctx.settings.files))
        data = attachments.describe(row)
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"item #{args.id}: attached {attachment_line(data)}")
    return 0


def item_attachments(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = describe_all(db, items.get_item(db, args.id))
    show(ctx, [{**a, "what": a.get("name") or a.get("url"), "size": a.get("size"), "title": a.get("title")}
               for a in rows],
         [("id", "ID"), ("kind", "KIND"), ("what", "NAME / URL"), ("size", "SIZE"), ("title", "TITLE"),
          ("created_at", "ADDED")])
    return 0


def item_detach(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        details = attachments.remove(db, ctx.actor, item, args.att, ctx.clock())
    if ctx.json:
        emit_json(ctx, details)
        return 0
    ctx.print(f"item #{args.id}: removed {attachment_line({**details, 'id': details['attachment']})}")
    return 0


def item_attachment_order(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        rows = [attachments.describe(r) for r in attachments.reorder(db, ctx.actor, item, args.ids, ctx.clock())]
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    ctx.print(f"item #{args.id}: the new order")
    print_attachments(ctx, rows)
    return 0


# ----------------------------------------------------------------------------- work
def _write_new(path: Path, store: BlobStore, sha256: str, force: bool) -> int:
    """Copy the blob into `path` through a temporary file next to it (0600: students' files are personal): an
    existing path is replaced only with `force` (os.link refuses an existing name, so there is no window)."""
    source = store.open(sha256)
    if source is None:
        raise CliError("the file is recorded but missing from the blob store (judgectl status --details)")
    directory = path.parent if str(path.parent) else Path(".")
    try:
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".judgectl-")
    except OSError as exc:
        source.close()
        raise CliError(f"cannot write into {directory}: {exc.strerror} (judgectl runs as judge: use -o - and a "
                       "redirection)") from exc
    try:
        total = 0
        with source, os.fdopen(fd, "wb") as out:
            while chunk := source.read(1 << 20):
                out.write(chunk)
                total += len(chunk)
        if force:
            os.replace(tmp, path)
        else:
            try:
                os.link(tmp, path)
            except FileExistsError as exc:
                raise CliError(f"{path} already exists: refusing to overwrite it without --force") from exc
            except OSError as exc:
                raise CliError(f"cannot create {path}: {exc.strerror}") from exc
        return total
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def save_file(ctx: Context, data: dict, out: str, force: bool) -> int:
    """Write a stored file (`data`: id, name, size, sha256) where -o says (D-081): a new file (0600; an existing one
    only with `force`) or stdout for `-` when it is not a terminal. Used by `work file` and `material file`."""
    store = BlobStore(ctx.settings.blob_dir)
    if out == "-":
        if ctx.json:
            raise CliError("--json and -o - both use stdout")
        if ctx.binout.isatty():
            raise CliError("-o - writes the file to stdout: redirect it to a file or a pipe, not a terminal")
        source = store.open(data["sha256"])
        if source is None:
            raise CliError("the file is recorded but missing from the blob store (judgectl status --details)")
        with source:
            while chunk := source.read(1 << 20):
                ctx.binout.write(chunk)
        ctx.binout.flush()
        ctx.note(f"file #{data['id']} {terminal_text(data['name'])} ({size_text(data['size'])}) written to stdout")
        return 0
    _write_new(Path(out), store, data["sha256"], force)
    if ctx.json:
        emit_json(ctx, {**data, "written_to": out})
        return 0
    ctx.print(f"file #{data['id']} {terminal_text(data['name'])} ({size_text(data['size'])}) written to "
              f"{terminal_text(out)}")
    return 0


def work_file(ctx: Context, args) -> int:
    with ctx.session() as db:
        item = items.get_item(db, args.id)
        student = get_by_login(db, args.login)
        row = works.work_file(db, CONSOLE, item, student, args.file_id)
        data = {"id": row.id, "kind": row.kind.value, "name": row.path, "size": row.size, "sha256": row.blob_sha256}
    return save_file(ctx, data, args.out, args.force)


# ----------------------------------------------------------------------------- the parser
def add_parsers(item, work, command) -> None:
    """The attachment commands in the `item` and `work` groups of feed.add_parsers (`command`: cli.py's helper)."""
    p = command(item, "attach", item_attach, "attach a file (a teacher's file of the item)", json=True)
    p.add_argument("id", type=int4)
    p.add_argument("file", help="the file, or - for stdin (then --name)")
    p.add_argument("--name", help="the name students see (default: the file's base name)")
    p = command(item, "link", item_link, "attach a link (stored as given; the server never opens it)", json=True)
    p.add_argument("id", type=int4)
    p.add_argument("url", help="http:// or https://")
    p.add_argument("--title", help="what students see instead of the address (at most 200 characters)")
    command(item, "attachments", item_attachments, "files and links of an item, in their order", json=True) \
        .add_argument("id", type=int4)
    p = command(item, "detach", item_detach, "remove a file or link from the item (the audit keeps what it was)",
                json=True)
    p.add_argument("id", type=int4)
    p.add_argument("att", type=int4, help="id of the attachment (item attachments)")
    p = command(item, "attachment-order", item_attachment_order, "the new order: every attachment id once",
                json=True)
    p.add_argument("id", type=int4)
    p.add_argument("ids", type=int4, nargs="+", metavar="att")

    p = command(work, "file", work_file, "save a file of a work (answer.md or an upload) to a new file", json=True)
    p.add_argument("id", type=int4)
    p.add_argument("login")
    p.add_argument("file_id", type=int4, help="id of the file (work show)")
    p.add_argument("-o", "--out", required=True, help="a new file, or - for stdout when it is not a terminal")
    p.add_argument("--force", action="store_true", help="replace an existing file")
