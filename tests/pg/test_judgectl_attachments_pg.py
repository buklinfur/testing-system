"""judgectl item attach | link | attachments | detach | attachment-order, item show, work show | file and
dev_seed --item --file / --link (docs/JUDGECTL.md, CLASSROOM.md §10, D-081) on a real PostgreSQL: the same rules,
limits and audit as the services, the limits of the settings (env) in every channel, refused uploads leave nothing
in the blob store or its tmp, names and URLs printed without control characters (--json as they are), a student's
file never printed to a terminal and never written over an existing file without --force.

Skipped unless JUDGE_TEST_DATABASE_URL is set.
"""
import contextlib
import io
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

T0 = datetime(2026, 11, 12, 3, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"%" * 64
HTML = b"<html><script>alert(1)</script></html>"
HOSTILE = "отчёт\x1b[2J\x1b]0;pwned\x07\r\u202egnp.exe"

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import blobstore, db, dev_seed
    from app import groups as g
    from app.auth import audit
    from app.classroom import items
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import (AssignmentSubmissionFile, AssignmentSubmissionLink, Attachment, AttachmentKind, AuditLog,
                            Blob, FeedItem, Role, User)
    from app.settings import FileSettings, Settings


def controls(text):
    """Raw control characters in judgectl's output (none may get through)."""
    return [hex(ord(ch)) for ch in text if ch not in "\n" and (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                                                or 0x202A <= ord(ch) <= 0x202E)]


class Tty(io.BytesIO):
    def isatty(self):
        return True


@requires_pg
class JudgectlAttachmentTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.blob_dir = self.dir / "blobs"
        self.actor = audit.Actor(user_id=self.seed["admin"].id)
        self.dev = User(login="dev-student-1", password_hash="!", display_name="Dev 1", role=Role.student)
        self.other = User(login="dev-student-2", password_hash="!", display_name="Dev 2", role=Role.student)
        self.s.add_all([self.dev, self.other])
        self.s.flush()
        self.group = g.create(self.s, self.actor, "7Б", T0)
        g.add_members(self.s, self.actor, self.group, [self.dev, self.other], T0)
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
                for t in (AuditLog, Attachment, Blob, AssignmentSubmissionFile, AssignmentSubmissionLink)]

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
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.stored()[0], store[0])
        self.assertEqual(self.stored()[1], [])
        return err

    def file(self, name, data) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    def assignment(self, answer_kind="text_or_files", publish=True):
        item = items.create_item(self.s, self.actor, T0, kind="assignment", title="Проект", body_md="Сделай",
                                 grading="pass_fail", due_at=DUE, answer_kind=answer_kind)
        items.set_audience(self.s, self.actor, item, T0, groups=[self.group])
        if publish:
            items.publish(self.s, self.actor, item, T0)
        self.s.commit()
        return item.id

    def hand_in(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        full = {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": str(self.blob_dir), **(env or {})}
        with mock.patch.dict(os.environ, full), mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dev_seed.main([str(a) for a in argv])
        return code, out.getvalue(), err.getvalue()

    # ---------------------------------------------------------------- a teacher's attachments
    def test_attach_link_list_order_detach(self):
        item = self.assignment()
        made = self.js("item", "attach", item, self.file("task.pdf", PDF), "--name", "Условие.pdf")
        self.assertEqual((made["kind"], made["name"], made["size"]), ("file", "Условие.pdf", len(PDF)))
        out, _ = self.ok("item", "attach", item, "-", "--name", "схема.png", stdin=PNG)
        self.assertIn("file схема.png (72 B)", out)
        link = self.js("item", "link", item, " https://scratch.mit.edu/projects/1 ", "--title", "  Пример  ")
        self.assertEqual((link["url"], link["title"]), ("https://scratch.mit.edu/projects/1", "Пример"))
        rows = self.js("item", "attachments", item)
        self.assertEqual([r["kind"] for r in rows], ["file", "file", "link"])
        out, _ = self.ok("item", "attachments", item)
        self.assertIn("NAME / URL", out)
        out, _ = self.ok("item", "show", item)
        self.assertIn(f"    #{made['id']} file Условие.pdf", out)
        self.assertIn("link https://scratch.mit.edu/projects/1 «Пример»", out)
        self.assertEqual(len(self.js("item", "show", item)["attachments"]), 3)

        ids = [r["id"] for r in rows]
        order = self.js("item", "attachment-order", item, ids[2], ids[0], ids[1])
        self.assertEqual([r["id"] for r in order], [ids[2], ids[0], ids[1]])
        self.refused("attachment_order_invalid", "item", "attachment-order", item, ids[0], ids[0], ids[1])
        out, _ = self.ok("item", "detach", item, ids[0])
        self.assertIn(f"removed #{ids[0]} file Условие.pdf", out)
        self.refused("attachment_not_found", "item", "detach", item, ids[0])
        self.assertEqual(len(self.stored()[0]), 2)  # the blob stays
        self.s.expire_all()
        actions = [r.action for r in self.s.execute(select(AuditLog).where(AuditLog.action.like("item.attachment%"))
                                                    .order_by(AuditLog.id)).scalars()]
        self.assertEqual(actions, ["item.attachment_add"] * 3 + ["item.attachment_order", "item.attachment_remove"])

    def test_refused_files_leave_nothing(self):
        item = self.assignment()
        self.refused("file_type_not_allowed", "item", "attach", item, self.file("run.py.exe", b"MZ" + b"\x00" * 9))
        self.refused("file_type_not_allowed", "item", "attach", item, self.file("pic.svg", b"<svg/>"))
        self.refused("file_content_mismatch", "item", "attach", item, self.file("pic.png", HTML))
        self.refused("file_empty", "item", "attach", item, self.file("empty.txt", b""))
        self.refused("link_invalid", "item", "link", item, "javascript:alert(1)")
        self.refused("link_invalid", "item", "link", item, "https://good.ru@evil.example/")
        self.refused("link_title_invalid", "item", "link", item, "https://a.ru", "--title", "я" * 201)
        err = self.refused(None, "item", "attach", item, "-", stdin=PDF)
        self.assertIn("needs --name", err)
        self.refused(None, "item", "attach", item, self.dir / "missing.pdf")
        self.ok("item", "attach", item, self.file("a.pdf", PDF))
        self.refused("duplicate_name", "item", "attach", item, self.file("A.PDF", PDF + b"2"))
        self.refused("item_not_found", "item", "attach", 999999, self.file("b.pdf", PDF))
        items.archive(self.s, self.actor, self.s.get(FeedItem, item), T0)
        self.s.commit()
        self.refused("item_archived", "item", "attach", item, self.file("c.pdf", PDF))
        self.refused("item_archived", "item", "link", item, "https://a.ru")

    def test_the_limits_of_the_settings(self):
        item = self.assignment()
        self.files = FileSettings(attachment_max_bytes=100, item_attachments_max=2)
        self.ok("item", "attach", item, self.file("exact.pdf", PDF + b"x" * (100 - len(PDF))))
        self.refused("file_too_large", "item", "attach", item, self.file("big.pdf", PDF + b"x" * (101 - len(PDF))))
        self.refused("file_too_large", "item", "attach", item, "-", "--name", "huge.pdf", stdin=PDF + b"x" * 5000)
        self.ok("item", "link", item, "https://a.ru")
        self.refused("too_many_attachments", "item", "link", item, "https://b.ru")

    def test_untrusted_names_are_escaped_and_json_is_as_is(self):
        item = self.assignment()
        sha = "ab" * 32
        self.s.add(Blob(sha256=sha, size_bytes=3))
        self.s.flush()
        # rows the rules would refuse (an older version, a hand edit): the console must still be safe
        self.s.add(Attachment(item_id=item, kind=AttachmentKind.file, name=HOSTILE + ".pdf", blob_sha256=sha, size=3,
                              position=0, created_at=T0))
        self.s.add(Attachment(item_id=item, kind=AttachmentKind.link, url="https://a.ru/\x1b[31m", title=HOSTILE,
                              position=1, created_at=T0))
        self.s.commit()
        for argv in (("item", "show", item), ("item", "attachments", item)):
            out, _ = self.ok(*argv)
            self.assertIn("\\x1b[2J", out)
            self.assertIn("\\u202e", out)
        rows = self.js("item", "attachments", item)
        self.assertEqual((rows[0]["name"], rows[1]["title"]), (HOSTILE + ".pdf", HOSTILE))

    # ---------------------------------------------------------------- hand-ins and work files
    def test_dev_seed_hands_in_files_and_links_and_work_file_saves_them(self):
        item = self.assignment()
        code, out, err = self.hand_in("--item", item, "--file", self.file("main.py", b"print(1)\n"),
                                   "--file", self.file("отчёт.pdf", PDF), "--link", "https://scratch.mit.edu/p/1",
                                   "Проект", "--link", "https://github.com/x/y")
        self.assertEqual(code, 0, err)
        made = json.loads(out)
        self.assertEqual([(f["kind"], f["name"]) for f in made["files"]], [("file", "main.py"), ("file", "отчёт.pdf")])
        self.assertEqual([(x["url"], x["title"]) for x in made["links"]],
                         [("https://scratch.mit.edu/p/1", "Проект"), ("https://github.com/x/y", None)])
        self.assertEqual(self.stored()[1], [])
        text = self.file("answer.txt", "Готово".encode())
        code, out, err = self.hand_in("--item", item, "--text-file", text, "--file", self.file("main.py", b"print(1)\n"))
        self.assertEqual(code, 0, err)
        second = json.loads(out)
        answer = next(f for f in second["files"] if f["kind"] == "text")

        out, _ = self.ok("work", "show", item, "dev-student-1")
        self.assertIn(f"file #{made['files'][1]['id']} отчёт.pdf", out)
        self.assertIn("link https://scratch.mit.edu/p/1 «Проект»", out)
        self.assertIn(f"file #{answer['id']} answer.md", out)
        self.assertIn("the text answer", out)

        dest = self.dir / "out" / "r.pdf"
        dest.parent.mkdir()
        out, _ = self.ok("work", "file", item, "dev-student-1", made["files"][1]["id"], "-o", dest)
        self.assertIn("written to", out)
        self.assertEqual(dest.read_bytes(), PDF)
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        err = self.refused(None, "work", "file", item, "dev-student-1", made["files"][0]["id"], "-o", dest)
        self.assertIn("without --force", err)
        self.assertEqual(dest.read_bytes(), PDF)
        self.assertEqual(sorted(p.name for p in dest.parent.iterdir()), ["r.pdf"])  # no temporary file left
        self.ok("work", "file", item, "dev-student-1", made["files"][0]["id"], "-o", dest, "--force")
        self.assertEqual(dest.read_bytes(), b"print(1)\n")
        self.ok("work", "file", item, "dev-student-1", answer["id"], "-o", self.dir / "a.md")
        self.assertEqual((self.dir / "a.md").read_text(encoding="utf-8"), "Готово")

        _out, err = self.ok("work", "file", item, "dev-student-1", made["files"][1]["id"], "-o", "-")
        self.assertEqual(self.binout.getvalue(), PDF)
        self.assertIn("written to stdout", err)
        self.binout = Tty()
        err = self.refused(None, "work", "file", item, "dev-student-1", made["files"][1]["id"], "-o", "-")
        self.assertIn("not a terminal", err)
        self.assertEqual(self.binout.getvalue(), b"")

        self.refused("file_not_found", "work", "file", item, "dev-student-2", made["files"][1]["id"], "-o",
                     self.dir / "x")
        other = self.assignment()
        self.refused("file_not_found", "work", "file", other, "dev-student-1", made["files"][1]["id"], "-o",
                     self.dir / "x")
        self.assertFalse((self.dir / "x").exists())

    def test_dev_seed_follows_the_rules_and_the_env_limits(self):
        item = self.assignment()
        files_only = self.assignment(answer_kind="files")
        text = self.file("t.txt", b"text")
        cases = [
            (("--item", files_only, "--text-file", text, "--file", self.file("a.py", b"1")), {}, "text_not_expected"),
            (("--item", self.assignment(answer_kind="text"), "--text-file", text, "--file", self.file("a.py", b"1")), {},
             "files_not_expected"),
            (("--item", files_only), {}, "files_required"),
            (("--item", item, "--file", self.file("x.exe", b"MZ")), {}, "file_type_not_allowed"),
            (("--item", item, "--file", self.file("p.png", HTML)), {}, "file_content_mismatch"),
            (("--item", item, "--link", "ftp://a.ru/x"), {}, "link_invalid"),
            (("--item", item, "--file", self.file("a.py", b"1"), "--file", self.file("b.py", b"2")),
             {"JUDGE_SUBMISSION_FILES_MAX": "1"}, "too_many_files"),
            (("--item", item, "--file", self.file("big.txt", b"x" * (1024 * 1024 + 1))),
             {"JUDGE_SUBMISSION_FILE_MAX_MB": "1"}, "file_too_large"),
            (("--item", item, "--file", self.file("q.txt", b"y" * (1024 * 1024 + 1))),
             {"JUDGE_STUDENT_QUOTA_MB": "1"}, "quota_exceeded"),
        ]
        for argv, env, code in cases:
            with self.subTest(code=code):
                before, store = self.counts(), self.stored()
                status, out, err = self.hand_in(*argv, env=env)
                self.assertEqual(status, 1, out + err)
                self.assertIn(f"refused: {code}", err)
                self.assertEqual(self.counts(), before)
                self.assertEqual(self.stored(), (store[0], []))
        status, _out, err = self.hand_in("--item", item, "--file", self.file("q.txt", b"y" * (1024 * 1024)),
                                      env={"JUDGE_STUDENT_QUOTA_MB": "1"})
        self.assertEqual(status, 0, err)
        status, _out, err = self.hand_in("--item", item, "--file", self.file("q.txt", b"y" * (1024 * 1024)),
                                      env={"JUDGE_STUDENT_QUOTA_MB": "1"})
        self.assertEqual(status, 0, err)  # the same blob again costs nothing

    def test_dev_seed_argument_checks(self):
        for argv in (("--file", "a.py"), ("--item", 1, "--comment", "x", "--file", "a.py"),
                     ("--item", 1, "--link", "https://a.ru", "t", "extra")):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    dev_seed.main([str(a) for a in argv])
                self.assertEqual(raised.exception.code, 2)
