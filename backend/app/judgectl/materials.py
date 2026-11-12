"""judgectl material ... / section ...: the library of materials and «Справочник» (4v.4, D-083..D-086,
docs/CLASSROOM.md §11, docs/JUDGECTL.md).

Materials are named by slug (or #id), sections by id, groups by name or #id. The console reads as a teacher
(classroom.comments.CONSOLE): every material, every version, archived ones too. A change of the content (the text, a
file or a link added or removed, a new order) makes a new version, and the command prints its number; a text with
links [[памятка:slug]] that are malformed, unknown or archived saves anyway and prints warnings. Titles, texts, file
names, URLs and slugs from links are untrusted text: printed through core.terminal_text, --json gives them as they
are. A file of a material is saved like a file of a work (D-081): to a new file, or to a pipe with -o -.
"""
from __future__ import annotations

from sqlalchemy import func, select

from .. import groups as g
from ..classroom import attachments as att
from ..classroom.comments import CONSOLE
from ..classroom.items import FeedError
from ..ids import parse_id
from ..materials import library, refs
from ..materials import service as materials
from ..models import Material
from . import attachments as attachment_cmds
from .core import CliError, Context, confirm, emit_json, int4, read_input, show, terminal_text

BODY_LIMIT = 1024 * 1024  # like the JSON body of the admin API (the service stops a text at 100 000 characters)
LIBRARY_MODES = ("none", "all_students", "groups")


def get_material(db, ref: str) -> Material:
    """A material by slug, or by #id."""
    ref = (ref or "").strip()
    if ref.startswith("#") and ref[1:].isdigit():
        number = parse_id(ref[1:])
        if number is None:  # beyond int4: nothing has it
            raise FeedError(f"no material {ref}", "material_not_found")
        return materials.get_material(db, number)
    return materials.by_slug(db, ref)


def _body(ctx: Context, source: str) -> str:
    return read_input(ctx, source, BODY_LIMIT, "the text").decode("utf-8", errors="replace")


def warn_links(ctx: Context, warnings) -> None:
    """Warnings about the links of a text, to stderr: the text is saved anyway."""
    for w in warnings:
        ctx.note(f"warning: {terminal_text(w)}")


def _lib(m: dict) -> str:
    return getattr(m["library"], "value", m["library"])


def link_line(r: dict) -> str:
    """One resolved link as the console sees it (an admin's view: ok, archived or unknown)."""
    title = f" «{terminal_text(r['title'])}»" if r.get("title") is not None else ""
    return f"[[памятка:{terminal_text(r['slug'])}]] {r['status']}{title}"


def _version_line(slug: str, result) -> str:
    v = result.version.version
    return f"material {terminal_text(slug)}: version {v}" if result.created else \
        f"material {terminal_text(slug)}: no change, the latest version {v} is the same"


# ----------------------------------------------------------------------------- material
def material_create(ctx: Context, args) -> int:
    body = _body(ctx, args.body_file) if args.body_file is not None else ""
    now = ctx.clock()
    with ctx.session() as db:
        result = materials.create_material(db, ctx.actor, now, slug=args.slug, title=args.title, body_md=body,
                                           library=args.library)
        material = materials.get_material(db, result.version.material_id)
        if args.group:
            materials.set_library_groups(db, ctx.actor, material, now, [g.get_group(db, r) for r in args.group])
        data = {**materials.material_summary(db, material), "warnings": result.warnings}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"material {terminal_text(data['slug'])} (#{data['id']}) created: version 1, library {_lib(data)}")
    warn_links(ctx, data["warnings"])
    return 0


def material_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        query = select(Material).order_by(Material.slug)
        if not args.all:
            query = query.where(Material.archived_at.is_(None))
        rows = [materials.material_summary(db, m) for m in db.execute(query).scalars()]
    if ctx.json:
        emit_json(ctx, rows)
        return 0
    show(ctx, [{**r, "library_text": _lib(r) + (f" ({len(r['groups'])} gr)" if _lib(r) == "groups" else ""),
                "archived": r["archived_at"] is not None} for r in rows],
         [("id", "ID"), ("slug", "SLUG"), ("title", "TITLE"), ("library_text", "LIBRARY"), ("versions", "VERSIONS"),
          ("archived", "ARCHIVED"), ("updated_at", "UPDATED")])
    return 0


def material_search(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = library.search(db, CONSOLE, args.query)
    show(ctx, rows, [("id", "ID"), ("slug", "SLUG"), ("title", "TITLE"), ("archived", "ARCHIVED")])
    return 0


def material_show(ctx: Context, args) -> int:
    now = ctx.clock()
    with ctx.session() as db:
        material = get_material(db, args.material)
        data = materials.material_view(db, CONSOLE, material, now)
        if args.version is not None:
            detail = materials.version_detail(db, material, args.version)
            data.update(version=detail["version"], body_md=detail["body_md"], attachments=detail["attachments"],
                        links=materials.resolve(db, CONSOLE, [detail["body_md"]], now),
                        version_created_at=detail["created_at"])
        data["link_warnings"] = refs.warnings(db, {"body": data["body_md"]})
    if ctx.json:
        emit_json(ctx, data)
        return 0
    archived = f" [archived {ctx.time(data['archived_at'])}]" if data["archived_at"] else ""
    ctx.print(f"#{data['id']} {terminal_text(data['slug'])}: {terminal_text(data['title'])}{archived}")
    shown = f"version {data['version']} of {data['versions']}" + ("" if data["version"] == data["versions"]
                                                                   else " (not the latest)")
    ctx.print(f"  {shown}, updated {ctx.time(data['updated_at'])}")
    groups = ", ".join(terminal_text(x["name"]) + (" (archived)" if x["archived"] else "")
                       for x in data["groups"]) or "-"
    ctx.print(f"  library: {_lib(data)}; groups: {groups}")
    sections = ", ".join(f"#{s['id']} {terminal_text(s['title'])}" for s in data["sections"]) or "-"
    ctx.print(f"  sections: {sections}")
    ctx.print("  attachments:")
    attachment_cmds.print_attachments(ctx, data["attachments"], indent="    ")
    if data["links"]:
        ctx.print("  links:")
        for r in data["links"]:
            ctx.print(f"    {link_line(r)}")
    if data["body_md"]:
        ctx.print("  text:")
        for line in terminal_text(data["body_md"], multiline=True).split("\n"):
            ctx.print(f"    {line}")
    warn_links(ctx, data["link_warnings"])
    return 0


def material_edit(ctx: Context, args) -> int:
    body = _body(ctx, args.body_file)
    with ctx.session() as db:
        material = get_material(db, args.material)
        result = materials.new_version(db, ctx.actor, material, ctx.clock(), body)
        data = {"slug": material.slug, "version": result.version.version, "created": result.created,
                "warnings": result.warnings}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(_version_line(data["slug"], result))
    warn_links(ctx, data["warnings"])
    return 0


def _update(ctx: Context, args, **values) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        changed = materials.update_material(db, ctx.actor, material, ctx.clock(), **values)
        data = materials.material_summary(db, material)
    if ctx.json:
        emit_json(ctx, {"material": data, "changed": changed})
        return 0
    ctx.print(f"material {terminal_text(data['slug'])}: " + (f"changed {', '.join(changed)}" if changed
                                                              else "nothing changed"))
    return 0


def material_title(ctx: Context, args) -> int:
    return _update(ctx, args, title=args.title)


def material_library(ctx: Context, args) -> int:
    return _update(ctx, args, library=args.mode)


def material_groups(ctx: Context, args) -> int:
    if (args.add or args.remove) and not args.groups:
        raise CliError("--add and --remove need at least one group")
    with ctx.session() as db:
        material = get_material(db, args.material)
        groups = [g.get_group(db, ref) for ref in args.groups]
        func = materials.add_library_groups if args.add else materials.remove_library_groups if args.remove \
            else materials.set_library_groups
        func(db, ctx.actor, material, ctx.clock(), groups)
        data = materials.material_summary(db, material)
    if ctx.json:
        emit_json(ctx, data)
        return 0
    names = ", ".join(terminal_text(x["name"]) + (" (archived)" if x["archived"] else "")
                      for x in data["groups"]) or "-"
    ctx.print(f"material {terminal_text(data['slug'])}: library groups {names}")
    if _lib(data) != "groups":
        ctx.note(f"note: the library audience is {_lib(data)}; the groups count only with "
                 f"`judgectl material library {data['slug']} groups`")
    return 0


def _state(ctx: Context, args, func, done: str, same: str) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        changed = func(db, ctx.actor, material, ctx.clock())
        slug = material.slug
    if ctx.json:
        emit_json(ctx, {"slug": slug, "changed": changed})
        return 0
    ctx.print(f"material {terminal_text(slug)}: {done if changed else same}")
    return 0


def material_archive(ctx: Context, args) -> int:
    return _state(ctx, args, materials.archive, "archived", "already archived")


def material_unarchive(ctx: Context, args) -> int:
    return _state(ctx, args, materials.unarchive, "back from the archive", "not archived")


def material_versions(ctx: Context, args) -> int:
    with ctx.session() as db:
        rows = materials.versions(db, get_material(db, args.material))
    show(ctx, rows, [("version", "VERSION"), ("created_at", "CREATED"), ("created_by", "BY"),
                     ("body_chars", "CHARS"), ("attachments", "ATTACHMENTS")])
    return 0


def _attached(ctx: Context, slug: str, version, data: dict) -> int:
    if ctx.json:
        emit_json(ctx, {"slug": slug, "version": version.version, "attachment": data})
        return 0
    ctx.print(f"material {terminal_text(slug)}: version {version.version}, attached "
              f"{attachment_cmds.attachment_line(data)}")
    return 0


def material_attach(ctx: Context, args) -> int:
    files = ctx.settings.files
    # one byte over the limit is enough for the service to say file_too_large itself
    upload = attachment_cmds.receive(ctx, args, files.attachment_max_bytes + 1)
    with upload, ctx.session() as db:
        material = get_material(db, args.material)
        version = materials.add_file(db, ctx.actor, material, upload, ctx.clock(),
                                     limits=att.teacher_limits(files))
        data = att.describe(materials.version_attachments(db, version)[-1])
        slug = material.slug
    return _attached(ctx, slug, version, data)


def material_link(ctx: Context, args) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        version = materials.add_link(db, ctx.actor, material, args.url, ctx.clock(), title=args.title,
                                     limits=att.teacher_limits(ctx.settings.files))
        data = att.describe(materials.version_attachments(db, version)[-1])
        slug = material.slug
    return _attached(ctx, slug, version, data)


def material_detach(ctx: Context, args) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        version = materials.remove(db, ctx.actor, material, args.att, ctx.clock())
        slug, number = material.slug, version.version
    if ctx.json:
        emit_json(ctx, {"slug": slug, "version": number, "removed": args.att})
        return 0
    ctx.print(f"material {terminal_text(slug)}: version {number}, attachment #{args.att} removed")
    return 0


def material_attachment_order(ctx: Context, args) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        result = materials.reorder(db, ctx.actor, material, args.ids, ctx.clock())
        rows = [att.describe(r) for r in materials.version_attachments(db, result.version)]
        slug = material.slug
    if ctx.json:
        emit_json(ctx, {"slug": slug, "version": result.version.version, "created": result.created,
                        "attachments": rows})
        return 0
    ctx.print(_version_line(slug, result))
    attachment_cmds.print_attachments(ctx, rows)
    return 0


def material_file(ctx: Context, args) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        row = materials.material_file(db, CONSOLE, material, args.att, ctx.clock(), version=args.version)
        data = {"id": row.id, "kind": row.kind.value, "name": row.name, "size": row.size, "sha256": row.blob_sha256}
    return attachment_cmds.save_file(ctx, data, args.out, args.force)


def material_where(ctx: Context, args) -> int:
    with ctx.session() as db:
        material = get_material(db, args.material)
        data = materials.where_used(db, material)
        slug = material.slug
    if ctx.json:
        emit_json(ctx, data)
        return 0
    ctx.print(f"material {terminal_text(slug)}")
    for key, label in (("material_items", "material items"), ("linking_items", "items linking it")):
        rows = data[key]
        ctx.print(f"  {label}: " + (", ".join(f"#{i['id']} {i['kind']} [{i['state']}] {terminal_text(i['title'])}"
                                             for i in rows) or "-"))
    ctx.print("  problem versions linking it: " + (", ".join(terminal_text(p) for p in data["problems"]) or "-"))
    ctx.print("  materials linking it (latest versions): "
              + (", ".join(terminal_text(m) for m in data["materials"]) or "-"))
    return 0


def material_resolve(ctx: Context, args) -> int:
    """The links of a text (a file or stdin) as they would be saved: warnings and what each slug is now."""
    text = _body(ctx, args.file)
    with ctx.session() as db:
        data = {"warnings": refs.warnings(db, {"text": text}),
                "links": materials.resolve(db, CONSOLE, [text], ctx.clock())}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    if not data["links"]:
        ctx.print("no links")
    for r in data["links"]:
        ctx.print(link_line(r))
    warn_links(ctx, data["warnings"])
    return 0


# ----------------------------------------------------------------------------- section
def _parent(db, args):
    return library.get_section(db, args.parent) if getattr(args, "parent", None) is not None else None


def section_create(ctx: Context, args) -> int:
    with ctx.session() as db:
        section = library.create_section(db, ctx.actor, ctx.clock(), args.title, _parent(db, args))
        data = {"id": section.id, "title": section.title, "parent_id": section.parent_id}
    if ctx.json:
        emit_json(ctx, data)
        return 0
    where = f"in #{data['parent_id']}" if data["parent_id"] is not None else "at the top level"
    ctx.print(f"section #{data['id']} {terminal_text(data['title'])} created {where}")
    return 0


def _tree(ctx: Context, sections, indent: str) -> None:
    for s in sections:
        ctx.print(f"{indent}#{s['id']} {terminal_text(s['title'])}")
        for m in s["materials"]:
            ctx.print(f"{indent}  - {terminal_text(m['slug'])}: {terminal_text(m['title'])}"
                      + (" (archived)" if m["archived"] else ""))
        _tree(ctx, s["sections"], indent + "  ")


def section_list(ctx: Context, args) -> int:
    with ctx.session() as db:
        data = library.library_view(db, CONSOLE, ctx.clock())
        counts = dict(db.execute(select(Material.library, func.count()).group_by(Material.library)).all())
    if ctx.json:
        emit_json(ctx, data)
        return 0
    if not data["sections"]:
        ctx.print("no sections")
    _tree(ctx, data["sections"], "")
    if data["unsorted"]:
        ctx.print("in no section:")
        for m in data["unsorted"]:
            ctx.print(f"  - {terminal_text(m['slug'])}: {terminal_text(m['title'])}"
                      + (" (archived)" if m["archived"] else ""))
    hidden = sum(n for lib, n in counts.items() if getattr(lib, "value", lib) == "none")
    if hidden:
        ctx.note(f"note: {hidden} material(s) with library none: students never see them in the library")
    return 0


def section_rename(ctx: Context, args) -> int:
    with ctx.session() as db:
        changed = library.rename_section(db, ctx.actor, library.get_section(db, args.id), ctx.clock(), args.title)
    ctx.print(f"section #{args.id}: " + ("renamed" if changed else "the same title"))
    return 0


def section_move(ctx: Context, args) -> int:
    if (args.parent is None) == (not args.top):
        raise CliError("give --parent ID or --top")
    with ctx.session() as db:
        changed = library.move_section(db, ctx.actor, library.get_section(db, args.id), ctx.clock(),
                                       _parent(db, args))
    where = f"into #{args.parent}" if args.parent is not None else "to the top level"
    ctx.print(f"section #{args.id}: " + (f"moved {where}" if changed else f"already {where.split(' ', 1)[1]}"))
    return 0


def section_delete(ctx: Context, args) -> int:
    confirm(ctx, str(args.id), f"deleting section #{args.id}", args.yes)
    with ctx.session() as db:
        details = library.delete_section(db, ctx.actor, library.get_section(db, args.id), ctx.clock())
    if ctx.json:
        emit_json(ctx, details)
        return 0
    left = ", ".join(terminal_text(s) for s in details["materials"])
    ctx.print(f"section #{args.id} {terminal_text(details['title'])} deleted"
              + (f"; its materials stay in the library: {left}" if left else ""))
    return 0


def _placement(ctx: Context, args, func, done: str, same: str) -> int:
    with ctx.session() as db:
        section = library.get_section(db, args.id)
        material = get_material(db, args.material)
        changed = func(db, ctx.actor, material, section, ctx.clock())
        slug = material.slug
    ctx.print(f"section #{args.id}: {terminal_text(slug)} {done if changed else same}")
    return 0


def section_place(ctx: Context, args) -> int:
    return _placement(ctx, args, library.place, "placed at the end", "is there already")


def section_unplace(ctx: Context, args) -> int:
    return _placement(ctx, args, library.unplace, "taken out", "was not there")


def section_order(ctx: Context, args) -> int:
    with ctx.session() as db:
        ids = library.reorder_sections(db, ctx.actor, ctx.clock(), args.ids, _parent(db, args))
    where = f"subsections of #{args.parent}" if args.parent is not None else "top-level sections"
    ctx.print(f"{where}: " + " ".join(f"#{i}" for i in ids))
    return 0


def section_material_order(ctx: Context, args) -> int:
    with ctx.session() as db:
        section = library.get_section(db, args.id)
        ids = [get_material(db, ref).id for ref in args.materials]
        library.reorder_materials(db, ctx.actor, section, ctx.clock(), ids)
        slugs = [db.get(Material, i).slug for i in ids]
    ctx.print(f"section #{args.id}: " + " ".join(terminal_text(s) for s in slugs))
    return 0


# ----------------------------------------------------------------------------- the parser
def add_parsers(groups, command) -> None:
    """`material` and `section` in the parser of cli.py (`command` is its helper)."""
    m = groups.add_parser("material", help="the library of materials: versions, files, links, audience").\
        add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    p = command(m, "create", material_create, "a new material with its version 1 (the slug never changes)",
                json=True)
    p.add_argument("slug", help="a-z, 0-9 and -, 2..63 characters; links write [[памятка:slug]]")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", help="the text in Markdown (a file, or - for stdin)")
    p.add_argument("--library", choices=LIBRARY_MODES, default="none",
                   help="who sees it in the library: nobody (default), every student, the groups (--group)")
    p.add_argument("--group", action="append", help="a library group (name or #id); repeat for more")
    p = command(m, "list", material_list, "materials by slug (not archived unless --all)", json=True)
    p.add_argument("--all", action="store_true", help="archived ones too")
    command(m, "search", material_search, "materials whose title contains the text (any case)", json=True) \
        .add_argument("query")
    p = command(m, "show", material_show, "the latest version (or --version N): text, attachments, links",
                json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("--version", type=int4, help="an older version")
    p = command(m, "edit", material_edit, "a new text: a new version (the same text: none)", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("--body-file", required=True, help="the text in Markdown (a file, or - for stdin)")
    p = command(m, "title", material_title, "a new title (in place, not a version)", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("title")
    p = command(m, "library", material_library, "who sees it in the library", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("mode", choices=LIBRARY_MODES)
    p = command(m, "groups", material_groups,
                "the library groups: exactly these (none: no groups), or --add / --remove them", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("groups", nargs="*", metavar="group", help="name or #id")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--add", action="store_true")
    mode.add_argument("--remove", action="store_true")
    for name, func, text in (("archive", material_archive, "read only, out of the library; links and the feed "
                                                           "still open it"),
                             ("unarchive", material_unarchive, "back from the archive")):
        command(m, name, func, text, json=True).add_argument("material", help="slug or #id")
    command(m, "versions", material_versions, "the history of versions, newest first", json=True) \
        .add_argument("material", help="slug or #id")
    p = command(m, "attach", material_attach, "a new version with this file appended", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("file", help="the file, or - for stdin (then --name)")
    p.add_argument("--name", help="the name students see (default: the file's base name)")
    p = command(m, "link", material_link, "a new version with this link appended (the server never opens it)",
                json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("url", help="http:// or https://")
    p.add_argument("--title", help="what students see instead of the address (at most 200 characters)")
    p = command(m, "detach", material_detach, "a new version without this attachment", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("att", type=int4, help="id of an attachment of the latest version (material show)")
    p = command(m, "attachment-order", material_attachment_order,
                "a new version with this order: every attachment id of the latest version once", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("ids", type=int4, nargs="+", metavar="att")
    p = command(m, "file", material_file, "save a file of the material to a new file", json=True)
    p.add_argument("material", help="slug or #id")
    p.add_argument("att", type=int4, help="id of the file (material show [--version N])")
    p.add_argument("--version", type=int4, help="a file of an older version")
    p.add_argument("-o", "--out", required=True, help="a new file, or - for stdout when it is not a terminal")
    p.add_argument("--force", action="store_true", help="replace an existing file")
    command(m, "where", material_where, "feed items, problem versions and materials that give or link it",
            json=True).add_argument("material", help="slug or #id")
    command(m, "resolve", material_resolve, "the links [[памятка:slug]] of a text: warnings and what each is",
            json=True).add_argument("file", help="the text (a file, or - for stdin)")

    s = groups.add_parser("section", help="sections of «Справочник» (two levels)").add_subparsers(
        dest="cmd", required=True, metavar="COMMAND")
    p = command(s, "create", section_create, "a section at the end (with --parent: a subsection)", json=True)
    p.add_argument("title")
    p.add_argument("--parent", type=int4, help="id of a top-level section")
    command(s, "list", section_list, "the library as a tree: sections, their materials, the rest", json=True)
    p = command(s, "rename", section_rename, "a new title")
    p.add_argument("id", type=int4)
    p.add_argument("title")
    p = command(s, "move", section_move, "to the end of another section or of the top level")
    p.add_argument("id", type=int4)
    p.add_argument("--parent", type=int4, help="id of a top-level section")
    p.add_argument("--top", action="store_true", help="to the top level")
    p = command(s, "delete", section_delete, "delete a section without subsections (its materials stay)", json=True)
    p.add_argument("id", type=int4)
    p.add_argument("--yes", action="store_true", help="no confirmation (scripts)")
    for name, func, text in (("place", section_place, "put a material at the end of a section"),
                             ("unplace", section_unplace, "take a material out of a section")):
        p = command(s, name, func, text)
        p.add_argument("id", type=int4)
        p.add_argument("material", help="slug or #id")
    p = command(s, "order", section_order, "the order of the top level (or of --parent): every section id once")
    p.add_argument("ids", type=int4, nargs="+", metavar="id")
    p.add_argument("--parent", type=int4, help="the subsections of this section")
    p = command(s, "material-order", section_material_order,
                "the order of the materials of a section: every one once")
    p.add_argument("id", type=int4)
    p.add_argument("materials", nargs="+", metavar="material", help="slug or #id")
