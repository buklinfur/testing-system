"""judgectl material ... / section ... and `item create --kind material` (docs/JUDGECTL.md, CLASSROOM.md §11, D-086)
on a real PostgreSQL: every change of the content prints the number of its version, links that are malformed,
unknown or archived save with warnings, the slug never changes, a file of a material is saved like a file of a work
(D-081), refused uploads leave nothing in the blob store or its tmp, titles, texts, names and URLs reach the terminal
without control characters (--json as they are), sections stay two levels deep.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import io
import json
import re
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"%" * 64
HTML = b"<html><script>alert(1)</script></html>"
HOSTILE = "Циклы\x1b[2J\x1b]0;pwned\x07\r\u202eкод"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import blobstore
    from app import groups as g
    from app.auth import audit
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import (Attachment, AuditLog, Blob, LibrarySection, Material, MaterialRef, MaterialSection,
                            MaterialVersion)
    from app.settings import FileSettings, Settings


def controls(text):
    """Raw control characters in judgectl's output (none may get through)."""
    return [hex(ord(ch)) for ch in text if ch not in "\n" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                                or 0x202A <= ord(ch) <= 0x202E)]


class Tty(io.BytesIO):
    def isatty(self):
        return True


@requires_pg
class JudgectlMaterialTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.blob_dir = self.dir / "blobs"
        self.actor = audit.Actor(user_id=self.seed["admin"].id)
        self.group = g.create(self.s, self.actor, "7Б", T0)
        self.old = g.create(self.s, self.actor, "6А", T0)
        g.archive(self.s, self.actor, self.old, T0)
        self.s.commit()
        self.files = FileSettings()
        self.binout = io.BytesIO()

    # ---------------------------------------------------------------- helpers
    def run_cli(self, *argv, stdin=b""):
        out, err = io.StringIO(), io.StringIO()
        settings = Settings(timezone="Asia/Novosibirsk", blob_dir=str(self.blob_dir), files=self.files)
        ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:judge"),
                      out=out, err=err, stdin=io.BytesIO(stdin), binout=self.binout, interactive=lambda: False,
                      clock=lambda: T0)
        code = main([str(a) for a in argv], ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, err)
        self.assertEqual(controls(out + err), [])
        return out, err

    def js(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, "--json", **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def counts(self):
        self.s.expire_all()
        return [self.s.execute(select(func.count()).select_from(t)).scalar()
                for t in (AuditLog, Material, MaterialVersion, Attachment, Blob, MaterialRef, LibrarySection,
                          MaterialSection)]

    def stored(self) -> tuple[list, list]:
        """(blob files in the store, files left in its tmp)."""
        blobs = sorted(p.name for p in self.blob_dir.glob("*/*")
                       if p.parent.name != blobstore.TMP_DIR and blobstore.valid_sha256(p.name))
        tmp = sorted(p.name for p in (self.blob_dir / blobstore.TMP_DIR).glob("*"))
        return blobs, tmp

    def refused(self, code, *argv, **kw):
        """Exit 1, `judgectl: ... (<code>)` on stderr; nothing written to the database or the blob store."""
        before, store = self.counts(), self.stored()
        status, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(status, 1, out + err)
        self.assertTrue(err.startswith("judgectl: "), err)
        if code:
            self.assertIn(f"({code})", err)
        self.assertEqual(controls(out + err), [])
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.stored()[0], store[0])
        self.assertEqual(self.stored()[1], [])
        return err

    def file(self, name, data) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def material(self, slug="cycles", title="Циклы", body="Цикл for"):
        if body:
            self.ok("material", "create", slug, "--title", title, "--body-file", "-", stdin=body.encode())
        else:
            self.ok("material", "create", slug, "--title", title)

    # ---------------------------------------------------------------- material
    def test_create_show_edit_versions(self):
        out, err = self.ok("material", "create", "cycles", "--title", "Циклы", "--body-file", "-",
                           stdin="См. [[памятка:lists]] и [[памятка:Bad Slug]]".encode())
        self.assertIn("material cycles (#", out)
        self.assertIn("created: version 1, library none", out)
        self.assertIn("warning:", err)
        self.assertIn("lists", err)
        self.refused("slug_taken", "material", "create", "cycles", "--title", "Ещё")
        self.refused("slug_invalid", "material", "create", "Циклы", "--title", "Ещё")
        self.refused("title_invalid", "material", "create", "empty-title", "--title", "  ")

        self.material("lists", "Списки", "Список")
        out, err = self.ok("material", "edit", "cycles", "--body-file", "-",
                           stdin="См. [[памятка:lists|списки]]".encode())
        self.assertEqual(out.strip(), "material cycles: version 2")
        self.assertEqual(err, "")
        out, _ = self.ok("material", "edit", "cycles", "--body-file", "-",
                         stdin="См. [[памятка:lists|списки]]".encode())
        self.assertIn("no change, the latest version 2 is the same", out)
        data = self.js("material", "edit", "cycles", "--body-file", self.file("v3.md", "Третья".encode()))
        self.assertEqual((data["version"], data["created"], data["warnings"]), (3, True, []))

        out, _ = self.ok("material", "show", "cycles")
        self.assertIn("version 3 of 3", out)
        self.assertIn("    Третья", out)
        out, _ = self.ok("material", "show", "cycles", "--version", 2)
        self.assertIn("version 2 of 3 (not the latest)", out)
        self.assertIn("[[памятка:lists]] ok «Списки»", out)
        shown = self.js("material", "show", "#%d" % data_id(self, "cycles"), "--version", 1)
        self.assertEqual(shown["version"], 1)
        self.assertEqual([r["status"] for r in shown["links"]], ["ok"])  # resolved when read: lists exists now
        self.refused("version_not_found", "material", "show", "cycles", "--version", 9)
        self.refused("material_not_found", "material", "show", "nothing")
        rows = self.js("material", "versions", "cycles")
        self.assertEqual([r["version"] for r in rows], [3, 2, 1])

        self.ok("material", "title", "cycles", "Циклы и ветвления")
        self.assertEqual(self.js("material", "show", "cycles")["title"], "Циклы и ветвления")
        out, _ = self.ok("material", "title", "cycles", "Циклы и ветвления")
        self.assertIn("nothing changed", out)
        actions = [r.action for r in self.s.execute(select(AuditLog).where(AuditLog.action.like("material.%"))
                                                    .order_by(AuditLog.id)).scalars()]
        self.assertEqual(actions, ["material.create", "material.create", "material.version", "material.version",
                                   "material.update"])

    def test_untrusted_text_is_escaped(self):
        self.refused("title_invalid", "material", "create", "hostile", "--title", HOSTILE)  # the rules refuse it
        _, err = self.ok("material", "create", "hostile", "--title", "Циклы", "--body-file", "-",
                         stdin=(HOSTILE + "\n[[памятка:x\x1b]] [[памятка:x|\u202eкод]]").encode())
        self.assertIn("warning:", err)
        for argv in (("material", "show", "hostile"), ("material", "list"), ("material", "search", "Циклы"),
                     ("material", "versions", "hostile"), ("material", "resolve", "-")):
            self.ok(*argv, stdin=HOSTILE.encode())
        data = self.js("material", "show", "hostile")
        self.assertTrue(data["body_md"].startswith(HOSTILE))  # --json as it is

    def test_library_audience_and_groups(self):
        self.material()
        out, err = self.ok("material", "groups", "cycles", "7Б")
        self.assertIn("library groups 7Б", out)
        self.assertIn("the library audience is none", err)
        self.ok("material", "library", "cycles", "groups")
        data = self.js("material", "show", "cycles")
        self.assertEqual((data["library"], [x["name"] for x in data["groups"]]), ("groups", ["7Б"]))
        self.refused("group_archived", "material", "groups", "cycles", "6А", "--add")
        self.ok("material", "groups", "cycles", "7Б", "--remove")
        self.assertEqual(self.js("material", "show", "cycles")["groups"], [])
        self.refused(None, "material", "groups", "cycles", "--add")
        self.ok("material", "create", "for-all", "--title", "Всем", "--library", "all_students", "--group", "7Б")

    def test_archive_is_idempotent_and_read_only(self):
        self.material()
        self.assertEqual(self.js("material", "archive", "cycles"), {"slug": "cycles", "changed": True})
        out, _ = self.ok("material", "archive", "cycles")
        self.assertIn("already archived", out)
        self.refused("material_archived", "material", "edit", "cycles", "--body-file", "-", stdin=b"new")
        self.refused("material_archived", "material", "attach", "cycles", self.file("a.pdf", PDF))
        self.refused("material_archived", "material", "title", "cycles", "Новое")
        self.assertEqual(self.js("material", "list"), [])
        self.assertEqual(len(self.js("material", "list", "--all")), 1)
        out, _ = self.ok("material", "unarchive", "cycles")
        self.assertIn("back from the archive", out)
        out, _ = self.ok("material", "unarchive", "cycles")
        self.assertIn("not archived", out)

    # ---------------------------------------------------------------- attachments and files
    def test_attachments_make_versions_and_files_are_saved(self):
        self.material()
        out, _ = self.ok("material", "attach", "cycles", self.file("task.pdf", PDF), "--name", "Условие.pdf")
        self.assertIn("material cycles: version 2, attached #", out)
        self.assertIn("file Условие.pdf (73 B)", out)
        pic = self.js("material", "attach", "cycles", "-", "--name", "схема.png", stdin=PNG)
        self.assertEqual((pic["version"], pic["attachment"]["name"]), (3, "схема.png"))
        link = self.js("material", "link", "cycles", " https://example.com/a ", "--title", " Пример ")
        self.assertEqual((link["version"], link["attachment"]["url"]), (4, "https://example.com/a"))
        ids = [a["id"] for a in self.js("material", "show", "cycles")["attachments"]]
        self.assertEqual(len(ids), 3)
        order = self.js("material", "attachment-order", "cycles", ids[2], ids[0], ids[1])
        self.assertEqual((order["version"], order["created"]), (5, True))
        new_ids = [a["id"] for a in order["attachments"]]
        self.refused("attachment_order_invalid", "material", "attachment-order", "cycles", *ids)  # old ids
        out, _ = self.ok("material", "attachment-order", "cycles", *new_ids)
        self.assertIn("no change", out)
        self.refused("duplicate_name", "material", "attach", "cycles", self.file("УСЛОВИЕ.pdf", PDF))
        self.refused("file_type_not_allowed", "material", "attach", "cycles", self.file("x.svg", b"<svg/>"))
        self.refused("file_content_mismatch", "material", "attach", "cycles", self.file("x.png", HTML))
        self.files = FileSettings(attachment_max_bytes=10)
        self.refused("file_too_large", "material", "attach", "cycles", self.file("big.pdf", PDF))
        self.files = FileSettings()

        # a file: a new file (0600), never over an existing one without --force; stdout only into a pipe
        pdf_id = new_ids[1]
        target = self.dir / "out.pdf"
        out, _ = self.ok("material", "file", "cycles", pdf_id, "-o", target)
        self.assertIn("written to", out)
        self.assertEqual(target.read_bytes(), PDF)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.refused(None, "material", "file", "cycles", pdf_id, "-o", target)
        self.ok("material", "file", "cycles", pdf_id, "-o", target, "--force")
        self.ok("material", "file", "cycles", pdf_id, "-o", "-")
        self.assertEqual(self.binout.getvalue(), PDF)
        self.binout = Tty()
        self.refused(None, "material", "file", "cycles", pdf_id, "-o", "-")
        self.binout = io.BytesIO()
        self.refused("attachment_not_found", "material", "file", "cycles", ids[0], "-o", self.dir / "old.pdf")
        v2 = self.js("material", "show", "cycles", "--version", 2)["attachments"][0]["id"]
        self.refused("attachment_not_found", "material", "file", "cycles", v2, "-o", self.dir / "v2.pdf")
        self.ok("material", "file", "cycles", v2, "--version", 2, "-o", self.dir / "v2.pdf")  # an old version
        self.assertEqual((self.dir / "v2.pdf").read_bytes(), PDF)
        link_id = new_ids[0]
        self.refused("attachment_not_found", "material", "file", "cycles", link_id, "-o", self.dir / "link")

        out, _ = self.ok("material", "detach", "cycles", pdf_id)
        self.assertIn("version 6, attachment", out)
        self.refused("attachment_not_found", "material", "detach", "cycles", pdf_id)
        self.assertEqual(len(self.stored()[0]), 2)  # the blobs stay with the old versions
        self.assertEqual([r["attachments"] for r in self.js("material", "versions", "cycles")], [2, 3, 3, 2, 1, 0])

    # ---------------------------------------------------------------- links, items, where
    def test_items_links_where_resolve(self):
        self.material()
        self.material("lists", "Списки", "См. [[памятка:cycles]]")
        made = self.js("item", "create", "--kind", "material", "--material", "cycles")
        self.assertEqual((made["title"], made["material"]["slug"]), ("Циклы", "cycles"))
        out, _ = self.ok("item", "show", made["id"])
        self.assertIn("  material: cycles «Циклы»", out)
        code, _, err = self.run_cli("item", "create", "--kind", "assignment", "--grading", "none")
        self.assertEqual(code, 1)
        self.assertIn("--title is required", err)
        self.refused("material_required", "item", "create", "--kind", "material", "--title", "М")
        self.refused("material_not_found", "item", "create", "--kind", "material", "--material", "nothing")
        out, err = self.ok("item", "create", "--kind", "assignment", "--title", "Задание", "--grading", "none",
                           "--body-file", "-", stdin="Читай [[памятка:cycles]] и [[памятка:later]]".encode())
        self.assertIn("warning:", err)
        self.assertIn("later", err)
        item = int(re.search(r"item #(\d+) created", out).group(1))
        _, err = self.ok("item", "show", item)
        self.assertIn("later", err)
        _, err = self.ok("item", "edit", item, "--body-file", "-", stdin="Только [[памятка:cycles]]".encode())
        self.assertEqual(err, "")
        self.assertEqual([i["id"] for i in self.js("item", "list", "--kind", "material")], [made["id"]])

        where = self.js("material", "where", "cycles")
        self.assertEqual(([i["id"] for i in where["material_items"]], [i["id"] for i in where["linking_items"]],
                          where["materials"]), ([made["id"]], [item], ["lists"]))
        out, _ = self.ok("material", "where", "cycles")
        self.assertIn("materials linking it (latest versions): lists", out)

        self.ok("material", "archive", "lists")
        out, err = self.ok("material", "resolve", "-",
                           stdin="[[памятка:cycles]] [[памятка:lists]] [[памятка:nope]] [[памятка:Bad]]".encode())
        self.assertEqual(out.splitlines(), ["[[памятка:cycles]] ok «Циклы»", "[[памятка:lists]] archived «Списки»",
                                            "[[памятка:nope]] unknown"])
        self.assertIn("warning:", err)
        out, _ = self.ok("material", "resolve", "-", stdin="без ссылок".encode())
        self.assertEqual(out.strip(), "no links")
        self.refused("material_archived", "item", "create", "--kind", "material", "--material", "lists")

    # ---------------------------------------------------------------- sections and search
    def test_sections_and_search(self):
        self.material()
        self.material("lists", "Списки", "")
        self.material("pct", "100% и a_b", "")
        top = self.js("section", "create", "Основы")["id"]
        other = self.js("section", "create", "Ещё")["id"]
        sub = self.js("section", "create", "Циклы", "--parent", top)["id"]
        self.refused("section_too_deep", "section", "create", "Глубже", "--parent", sub)
        self.refused("section_not_found", "section", "create", "Нет", "--parent", 999)
        self.ok("section", "place", sub, "cycles")
        self.ok("section", "place", sub, "lists")
        out, _ = self.ok("section", "place", sub, "lists")
        self.assertIn("is there already", out)
        self.ok("section", "material-order", sub, "lists", "cycles")
        self.refused("order_invalid", "section", "material-order", sub, "lists")
        self.ok("section", "order", other, top)
        self.refused("order_invalid", "section", "order", top)
        out, err = self.ok("section", "list")
        self.assertEqual(out.splitlines(), [f"#{other} Ещё", f"#{top} Основы", f"  #{sub} Циклы",
                                            "    - lists: Списки", "    - cycles: Циклы", "in no section:",
                                            "  - pct: 100% и a_b"])
        self.assertIn("library none", err)
        self.refused("section_too_deep", "section", "move", top, "--parent", other)  # it has a subsection
        self.ok("section", "move", sub, "--top")
        self.refused(None, "section", "move", sub)
        self.ok("section", "move", sub, "--parent", top)
        self.refused("section_not_empty", "section", "delete", top, "--yes")
        self.refused(None, "section", "delete", sub)  # no terminal, no --yes
        out, _ = self.ok("section", "delete", sub, "--yes")
        self.assertIn("its materials stay in the library: lists, cycles", out)
        self.ok("section", "rename", top, "Начало")
        self.ok("section", "unplace", other, "cycles")

        self.assertEqual([m["slug"] for m in self.js("material", "search", "циклы")], ["cycles"])
        self.assertEqual([m["slug"] for m in self.js("material", "search", "ЦИКЛ")], ["cycles"])
        self.assertEqual([m["slug"] for m in self.js("material", "search", "%")], ["pct"])
        self.assertEqual([m["slug"] for m in self.js("material", "search", "_")], ["pct"])


def data_id(case, slug):
    case.s.expire_all()
    return case.s.execute(select(Material.id).where(Material.slug == slug)).scalar_one()
