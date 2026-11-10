"""Rules of files and links (app/classroom/attachment_rules.py and the answer kinds of access.can_submit_work;
docs/CLASSROOM.md §10, D-078): names, types by signature, links, answer_kind x text x files x links, limits and the
quota at their boundaries, the order of the codes when several conditions fail, who sees the teacher's files - and
that the module stays pure, with signatures in step with content_files.sniff."""
import ast
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app.classroom import access, attachment_rules as ar  # noqa: E402

US = timedelta(microseconds=1)
AVAILABLE = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)
DURING = AVAILABLE + timedelta(days=1)
ADMIN = NS(id=1, role="admin")
PUPIL = NS(id=2, role="student")
RULE_FILE = "backend/app/classroom/attachment_rules.py"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 16
ZIP = b"PK\x03\x04" + b"\x00" * 26
EMPTY_ZIP = b"PK\x05\x06" + b"\x00" * 18
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><script>alert(1)</script></html>"
CP1251 = "print('Привет')\n".encode("cp1251")
MIB = 1024 * 1024


def assignment(**kw):
    base = dict(kind="assignment", state="published", available_from=AVAILABLE, due_at=DUE,
                after_due_statement="visible", after_due_submit="late", grading="points", max_points=Decimal(10),
                answer_kind="files", grades_release="manual")
    base.update(kw)
    return NS(**base)


def contest_item(**kw):
    base = dict(kind="contest", state="published", available_from=None, due_at=None, after_due_statement="visible",
                after_due_submit="late")
    base.update(kw)
    return NS(**base)


OPEN_CONTEST = NS(state="published", access_mode="open", timing_mode="fixed", starts_at=AVAILABLE, ends_at=None,
                  personal_duration=None, allow_upsolving=True)


def f(name, head=b"x = 1\n", size=None):
    return NS(name=name, head=head, size=len(head) if size is None else size)


def link(url="https://scratch.mit.edu/projects/1", title=None):
    return NS(url=url, title=title)


def code(decision):
    return "ok" if decision else decision.reason if hasattr(decision, "reason") else decision.code


# --------------------------------------------------------------------------- purity
class PurityTests(unittest.TestCase):
    STDLIB = {"__future__", "dataclasses", "datetime", "typing", "unicodedata", "urllib"}
    BACKEND = {("..deadline_rules", "Decision"), (".", "access")}

    def test_imports_only_deadline_rules_and_access(self):
        found = set()
        for node in ast.walk(ast.parse((ROOT / RULE_FILE).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {(a.name, None) for a in node.names if a.name.split(".")[0] not in self.STDLIB}
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                if node.level or module.split(".")[0] not in self.STDLIB:
                    found |= {(module, a.name) for a in node.names}
        self.assertEqual(sorted(found - self.BACKEND, key=str), [])

    def test_importing_it_loads_no_database_code(self):
        script = ("import sys; import app.classroom.attachment_rules; "
                  "print(sorted(m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'psycopg', 'fastapi') "
                  "or m in ('app.models', 'app.db', 'app.contests.service', 'app.settings', 'app.content_files')))")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "backend"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_clock_and_no_date_comparison_of_its_own(self):
        moment = lambda n: (isinstance(n, ast.Name) and n.id in ("now", "due", "when")) or (  # noqa: E731
            isinstance(n, ast.Attribute) and (n.attr.endswith(("_at", "_from")) or n.attr == "at"))
        for node in ast.walk(ast.parse((ROOT / RULE_FILE).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare) and not all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
                with self.subTest(line=node.lineno):
                    self.assertFalse(any(moment(n) for n in [node.left, *node.comparators]))
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("now", "utcnow", "today", "time", "monotonic"), node.lineno)


class InStepTests(unittest.TestCase):
    """The signatures and sizes the rules use are the ones the server serves by (docs/FILES.md, D-070)."""

    def test_the_checks_of_the_tables_hold_the_same_limits(self):
        """The CHECKs of attachments and assignment_submission_links (models.py) keep these numbers; the rules refuse
        more, the database only makes sure nothing longer is ever stored."""
        try:
            from app import models
        except ImportError:  # pragma: no cover - sqlalchemy is not installed
            self.skipTest("needs sqlalchemy")
        self.assertEqual(models.ATTACHMENT_NAME_MAX_CHARS, ar.NAME_MAX)
        self.assertEqual(models.LINK_URL_MAX_CHARS, ar.LINK_URL_MAX)
        self.assertEqual(models.LINK_TITLE_MAX_CHARS, ar.LINK_TITLE_MAX)
        self.assertLessEqual(ar.NAME_MAX, models.AssignmentSubmissionFile.__table__.c.path.type.length)

    def test_signatures_agree_with_content_files_and_the_package_check(self):
        try:
            from app import content_files
        except ImportError:  # pragma: no cover - fastapi is not installed
            self.skipTest("needs fastapi")
        from protocol.problem_package import picture_kind
        heads = [PNG, JPEG, WEBP, ZIP, EMPTY_ZIP, PDF, SVG, HTML, CP1251, b"", b"RIFF\x00\x00\x00\x00WAVE",
                 b"\x89PNG", b"\xff\xd8", b"PK\x03"]
        for head in heads:
            with self.subTest(head=head[:12]):
                ours = ar.signature(head)
                served = content_files.sniff(head)
                self.assertEqual(ours if ours in ("png", "jpeg", "webp", "zip") else None, served)
                self.assertEqual(ours if ours in ("png", "jpeg", "webp") else None, picture_kind(head))
        self.assertEqual(ar.HEAD_BYTES, content_files.HEAD_BYTES)
        self.assertEqual(ar.NAME_MAX, content_files.NAME_MAX)

    def test_every_picture_suffix_is_one_content_files_shows(self):
        try:
            from app import content_files
        except ImportError:  # pragma: no cover
            self.skipTest("needs fastapi")
        pictures = {s for s, kind in ar.ALLOWED.items() if kind in ("png", "jpeg", "webp")}
        self.assertEqual(pictures, set(content_files.IMAGE_SUFFIXES))


# --------------------------------------------------------------------------- names
class NameTests(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("Задача 1.py", "ok"),
            ("отчёт.v2.pdf", "ok"),
            ("ПРОЕКТ.SB3", "ok"),  # the suffix is compared in lower case
            ("Main.CPP", "ok"),
            (".hidden.py", "ok"),
            ("a" * 147 + ".py", "ok"),  # 150 characters
            ("a" * 148 + ".py", "file_name_invalid"),  # 151
            ("README", "file_type_not_allowed"),  # no suffix
            ("notes.", "file_type_not_allowed"),
            (".py", "file_type_not_allowed"),  # a name that is only a suffix
            ("run.py.exe", "file_type_not_allowed"),
            ("pic.svg", "file_type_not_allowed"),
            ("page.html", "file_type_not_allowed"),
            ("pic.gif", "file_type_not_allowed"),
            ("setup.exe", "file_type_not_allowed"),
            ("script.sh", "file_type_not_allowed"),
            ("photo.heic", "file_type_not_allowed"),
            ("", "file_name_invalid"),
            ("   ", "file_name_invalid"),
            ("...", "file_name_invalid"),
            (" a.py", "file_name_invalid"),
            ("a.py ", "file_name_invalid"),
            ("dir/a.py", "file_name_invalid"),
            ("dir\\a.py", "file_name_invalid"),
            ("a\x00.py", "file_name_invalid"),
            ("a\nb.py", "file_name_invalid"),
            ("evil\u202eyp.exe", "file_name_invalid"),  # a direction mark
            ("a\ud800.py", "file_name_invalid"),  # a lone surrogate
            (None, "file_name_invalid"),
            (b"a.py", "file_name_invalid"),
        ]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(code(ar.check_name(name)), expected)

    def test_duplicates_ignore_case(self):
        for files, expected in (
                ([f("a.py"), f("A.PY")], ("duplicate_name", 1)),
                ([f("Отчёт.txt"), f("отчёт.TXT")], ("duplicate_name", 1)),
                ([f("a.py"), f("b.py"), f("a.py")], ("duplicate_name", 2)),
                ([f("a.py"), f("a.py.txt")], "ok"),
                ([f("a.py"), f("a.c")], "ok")):
            with self.subTest(files=[x.name for x in files]):
                result = ar.check_hand_in(files)
                self.assertEqual("ok" if result else (result.code, result.index), expected)

    def test_a_teachers_new_file_against_the_items_names(self):
        result = ar.check_new_attachment(["Условие.pdf"], 1, file=f("условие.PDF", PDF))
        self.assertEqual((result.code, result.index), ("duplicate_name", 0))
        self.assertTrue(ar.check_new_attachment(["Условие.pdf"], 1, file=f("Условие-2.pdf", PDF)))
        self.assertTrue(ar.check_new_attachment(["a.py"], 1, link=link()))  # links have no names to clash


# --------------------------------------------------------------------------- types by content
class ContentTests(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("pic.png", PNG, "ok"),
            ("pic.jpg", JPEG, "ok"),
            ("pic.JPEG", JPEG, "ok"),
            ("pic.webp", WEBP, "ok"),
            ("pic.png", SVG, "file_content_mismatch"),
            ("pic.png", HTML, "file_content_mismatch"),  # HTML under .png
            ("pic.jpg", PNG, "file_content_mismatch"),  # PNG under .jpg: stricter than serving (D-070)
            ("pic.png", JPEG, "file_content_mismatch"),
            ("pic.webp", PNG, "file_content_mismatch"),
            ("pic.png", b"", "file_empty"),
            ("main.py", CP1251, "ok"),  # a cp1251 source from Windows
            ("notes.txt", b"\x00\x01\x02", "ok"),  # text kinds are not checked
            ("data.json", SVG, "ok"),
            ("empty.py", b"", "file_empty"),  # an empty file of any type: it would satisfy files_required
            ("empty.txt", b"", "file_empty"),
            ("empty.zip", b"", "file_empty"),
            ("one.txt", b"\n", "ok"),
            ("project.zip", ZIP, "ok"),
            ("project.zip", EMPTY_ZIP, "file_content_mismatch"),
            ("report.docx", ZIP, "ok"),
            ("report.docx", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "file_content_mismatch"),  # an old .doc renamed
            ("report.docx", HTML, "file_content_mismatch"),
            ("table.xlsx", ZIP, "ok"),
            ("slides.pptx", ZIP, "ok"),
            ("text.odt", ZIP, "ok"),
            ("calc.ods", ZIP, "ok"),
            ("show.odp", ZIP, "ok"),
            ("game.sb3", ZIP, "ok"),
            ("game.sb3", PNG, "file_content_mismatch"),
            ("paper.pdf", PDF, "ok"),
            ("paper.pdf", b"%PDF", "file_content_mismatch"),  # no dash: not the header
            ("paper.pdf", HTML, "file_content_mismatch"),
            ("paper.pdf", b"\n%PDF-1.4", "file_content_mismatch"),
        ]
        for name, head, expected in cases:
            with self.subTest(name=name, head=head[:8]):
                self.assertEqual(code(ar.check_file(name, len(head), head, ar.SUBMISSION_FILE_MAX_BYTES)), expected)

    def test_file_order_name_type_size_content(self):
        big = 21 * MIB
        self.assertEqual(code(ar.check_file("a/b.png", big, SVG, 20 * MIB)), "file_name_invalid")
        self.assertEqual(code(ar.check_file("b.svg", big, SVG, 20 * MIB)), "file_type_not_allowed")
        self.assertEqual(code(ar.check_file("b.svg", 0, b"", 20 * MIB)), "file_type_not_allowed")
        self.assertEqual(code(ar.check_file("b.png", 0, b"", 20 * MIB)), "file_empty")
        self.assertEqual(code(ar.check_file("b.png", big, SVG, 20 * MIB)), "file_too_large")
        self.assertEqual(code(ar.check_file("b.png", 10, SVG, 20 * MIB)), "file_content_mismatch")

    def test_an_empty_file_does_not_hand_anything_in(self):
        """A hand-in of only an empty .txt is refused before files_required could be satisfied by it."""
        self.assertEqual(ar.check_hand_in([f("a.py"), f("empty.txt", b"")], [], ar.Limits(10, MIB, None)),
                         ar.Refusal("file_empty", 1))

    def test_whitelist_is_the_agreed_one(self):
        self.assertEqual(set(ar.ALLOWED), {
            ".py", ".c", ".cpp", ".h", ".hpp", ".txt", ".md", ".csv", ".json", ".ipynb",
            ".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".sb3", ".pdf",
            ".png", ".jpg", ".jpeg", ".webp"})


# --------------------------------------------------------------------------- links
class LinkTests(unittest.TestCase):
    def test_cases(self):
        host = "https://example.ru/"
        cases = [
            ("https://scratch.mit.edu/projects/123456/", "ok"),
            ("http://example.ru", "ok"),
            ("HTTPS://EXAMPLE.RU/Path", "ok"),
            ("hTTp://example.ru", "ok"),
            ("  https://example.ru/a  ", "ok"),  # whitespace around it is stripped
            ("https://www.youtube.com/@channel", "ok"),  # @ in the path
            ("https://example.ru/?q=a@b#x@y", "ok"),
            ("https://пример.рф/страница", "ok"),
            ("https://example.ru:8443/a", "ok"),
            ("https://[2001:db8::1]/a", "ok"),
            (host + "a" * (2000 - len(host)), "ok"),  # 2000 characters
            (host + "a" * (2001 - len(host)), "link_invalid"),  # 2001
            ("javascript:alert(1)", "link_invalid"),
            ("JavaScript:alert(1)", "link_invalid"),
            ("data:text/html,<script>alert(1)</script>", "link_invalid"),
            ("file:///etc/passwd", "link_invalid"),
            ("ftp://example.ru/a", "link_invalid"),
            ("//example.ru/a", "link_invalid"),  # no scheme
            ("example.ru", "link_invalid"),
            ("https://", "link_invalid"),  # no host
            ("https:///path", "link_invalid"),
            ("https:example.ru", "link_invalid"),
            ("https:/example.ru", "link_invalid"),
            ("https://:80/", "link_invalid"),
            ("https://user:pass@example.ru/", "link_invalid"),
            ("https://good.ru@evil.example/", "link_invalid"),
            ("https://good.ru\\@evil.example/", "link_invalid"),
            ("https://example.ru:port/", "link_invalid"),
            ("https://exa mple.ru/", "link_invalid"),
            ("https://example.ru/a b", "link_invalid"),
            ("https://example.ru/a\tb", "link_invalid"),
            ("https://example.ru/a\u00a0b", "link_invalid"),  # NBSP inside (around it, it is stripped)
            ("\u00a0https://example.ru/\u3000", "ok"),
            ("https://example.ru/a\nb", "link_invalid"),
            ("https://example.ru/a\x00", "link_invalid"),
            ("https://example.ru/\x1b[31m", "link_invalid"),
            ("https://example.ru/\u202egnp.exe", "link_invalid"),  # a direction mark
            ("https://example.ru/\u200b", "link_invalid"),  # zero width space
            ("", "link_invalid"),
            ("   ", "link_invalid"),
            (None, "link_invalid"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(code(ar.check_link(url)), expected)

    def test_titles(self):
        for title, expected in ((None, "ok"), ("", "ok"), ("Проект в Scratch", "ok"), ("я" * 200, "ok"),
                                ("я" * 201, "link_title_invalid"), ("a\nb", "link_title_invalid"),
                                ("a\x00", "link_title_invalid"), ("\u202eabc", "link_title_invalid"),
                                ("a\ud800", "link_title_invalid"), ("👨\u200d💻 код", "ok"), (5, "link_title_invalid")):
            with self.subTest(title=title):
                self.assertEqual(code(ar.check_link("https://example.ru", title)), expected)

    def test_a_bad_url_is_reported_before_its_title(self):
        self.assertEqual(code(ar.check_link("javascript:x", "a\n")), "link_invalid")


# --------------------------------------------------------------------------- answer_kind x text x files x links
class AnswerKindTests(unittest.TestCase):
    # (answer_kind, a text is sent, files or links are sent) -> the code
    EXPECTED = {
        ("mark", False, False): "ok",
        ("mark", True, False): "text_not_expected",
        ("mark", False, True): "files_not_expected",
        ("mark", True, True): "text_not_expected",
        ("text", False, False): "answer_required",
        ("text", True, False): "ok",
        ("text", False, True): "answer_required",
        ("text", True, True): "files_not_expected",
        ("files", False, False): "files_required",
        ("files", True, False): "text_not_expected",
        ("files", False, True): "ok",
        ("files", True, True): "text_not_expected",
        ("text_or_files", False, False): "answer_required",
        ("text_or_files", True, False): "ok",
        ("text_or_files", False, True): "ok",
        ("text_or_files", True, True): "ok",
    }

    def test_matrix(self):
        for answer_kind in ("mark", "text", "files", "text_or_files"):
            for text in (None, "", " \n\t", "ответ"):
                for files in (0, 1, 3):
                    for links in (0, 1):
                        sent_text, sent_any = bool(text and text.strip()), bool(files + links)
                        expected = self.EXPECTED[(answer_kind, sent_text, sent_any)]
                        with self.subTest(answer_kind=answer_kind, text=text, files=files, links=links):
                            d = access.can_submit_work(PUPIL, assignment(answer_kind=answer_kind), DURING,
                                                       in_audience=True, text=text, attachments=files + links)
                            self.assertEqual(code(d), expected)

    def test_old_calls_without_attachments_keep_their_codes(self):
        """The calls of 4v.1 do not pass attachments: text and mark behave as before."""
        self.assertEqual(code(access.can_submit_work(PUPIL, assignment(answer_kind="text"), DURING, in_audience=True,
                                                     text="")), "answer_required")
        self.assertEqual(code(access.can_submit_work(PUPIL, assignment(answer_kind="mark"), DURING, in_audience=True,
                                                     text="x")), "text_not_expected")
        self.assertEqual(code(access.can_submit_work(PUPIL, assignment(answer_kind="mark"), DURING,
                                                     in_audience=True)), "ok")

    def test_order_when_several_conditions_fail(self):
        """After already_graded: answer_required, text_not_expected, files_not_expected, files_required,
        text_too_long - and every check of the 4v.1 order still comes first."""
        long = "я" * 10  # 20 bytes
        submit = lambda kind, **kw: code(access.can_submit_work(  # noqa: E731
            PUPIL, assignment(answer_kind=kind), DURING, in_audience=True, text_limit=4, **kw))
        self.assertEqual(submit("files", text=long, attachments=0), "text_not_expected")  # not files_required
        self.assertEqual(submit("text", text=long, attachments=2), "files_not_expected")  # not text_too_long
        self.assertEqual(submit("text_or_files", text=long, attachments=2), "text_too_long")
        self.assertEqual(submit("text_or_files", text="", attachments=0), "answer_required")
        self.assertEqual(submit("mark", text=long, attachments=2), "text_not_expected")
        graded = code(access.can_submit_work(PUPIL, assignment(answer_kind="files"), DURING, in_audience=True,
                                             work_status="graded", text="x", attachments=0))
        self.assertEqual(graded, "already_graded")
        draft = code(access.can_submit_work(PUPIL, assignment(answer_kind="files", state="draft"), DURING,
                                            in_audience=False, text="x"))
        self.assertEqual(draft, "not_published")


# --------------------------------------------------------------------------- limits and the quota
class LimitTests(unittest.TestCase):
    LIMITS = ar.Limits(files_max=3, file_max_bytes=100, total_max_bytes=250)

    def check(self, files, links=()):
        result = ar.check_hand_in(files, links, self.LIMITS)
        return "ok" if result else (result.code, result.index)

    def test_count_of_files_and_links(self):
        self.assertEqual(self.check([f("a.py"), f("b.py")], [link()]), "ok")  # 3
        self.assertEqual(self.check([f("a.py"), f("b.py")], [link(), link()]), ("too_many_files", None))  # 4
        self.assertEqual(self.check([], [link()] * 3), "ok")
        self.assertEqual(self.check([], [link()] * 4), ("too_many_files", None))

    def test_size_of_one_file(self):
        self.assertEqual(self.check([f("a.py", size=99)]), "ok")
        self.assertEqual(self.check([f("a.py", size=100)]), "ok")
        self.assertEqual(self.check([f("a.py", size=101)]), ("file_too_large", 0))

    def test_sum_of_a_hand_in(self):
        self.assertEqual(self.check([f("a.py", size=100), f("b.py", size=100), f("c.py", size=50)]), "ok")  # 250
        self.assertEqual(self.check([f("a.py", size=100), f("b.py", size=100), f("c.py", size=51)]),
                         ("submission_too_large", None))  # 251

    def test_defaults(self):
        self.assertEqual((ar.SUBMISSION_FILES_MAX, ar.SUBMISSION_FILE_MAX_BYTES, ar.SUBMISSION_TOTAL_MAX_BYTES,
                          ar.STUDENT_QUOTA_BYTES, ar.ITEM_ATTACHMENTS_MAX, ar.ATTACHMENT_MAX_BYTES),
                         (10, 20 * MIB, 50 * MIB, 200 * MIB, 20, 50 * MIB))
        files = [f(f"{i}.py", size=ar.SUBMISSION_FILE_MAX_BYTES // 4) for i in range(10)]
        self.assertTrue(ar.check_hand_in(files))  # 10 files of 5 MB: exactly 50 MB
        self.assertEqual(ar.check_hand_in(files + [f("x.py")]).code, "too_many_files")

    def test_order_of_a_hand_in(self):
        """too_many_files, then each file in order (its name, type, size, content), duplicate_name,
        submission_too_large, then each link."""
        bad_link = link("javascript:alert(1)")
        self.assertEqual(self.check([f("a.svg"), f("a.py"), f("a.py")], [bad_link]), ("too_many_files", None))
        self.assertEqual(self.check([f("a.py", size=101), f("a.svg")], [bad_link]), ("file_too_large", 0))
        self.assertEqual(self.check([f("a.py"), f("b.png", HTML)], [bad_link]), ("file_content_mismatch", 1))
        self.assertEqual(self.check([f("a.py", size=100), f("A.py", size=100), f("c.py", size=100)]),
                         ("duplicate_name", 1))  # before the sum (300)
        self.assertEqual(self.check([f("a.py", size=100), f("b.py", size=100), f("c.py", size=100)]),
                         ("submission_too_large", None))
        self.assertEqual(self.check([f("a.py")], [link(), bad_link]), ("link_invalid", 1))

    def test_teacher_attachments(self):
        lim = ar.Limits(files_max=2, file_max_bytes=100, total_max_bytes=None)
        add = lambda count, **kw: ar.check_new_attachment([], count, limits=lim, **kw)  # noqa: E731
        self.assertTrue(add(1, file=f("a.py")))
        self.assertEqual(add(2, file=f("a.py")).code, "too_many_attachments")
        self.assertEqual(add(2, link=link()).code, "too_many_attachments")
        self.assertTrue(add(1, file=f("a.py", size=100)))
        self.assertEqual(add(1, file=f("a.py", size=101)).code, "file_too_large")
        self.assertEqual(add(2, file=f("a.svg")).code, "too_many_attachments")  # the count first
        self.assertEqual(add(0, link=link("file:///etc/passwd")).code, "link_invalid")
        with self.assertRaises(ValueError):
            ar.check_new_attachment([], 0, file=f("a.py"), link=link())
        self.assertEqual((ar.TEACHER_LIMITS.files_max, ar.TEACHER_LIMITS.file_max_bytes), (20, 50 * MIB))

    def test_quota_at_the_boundary(self):
        self.assertTrue(ar.quota_ok(150, 50, 200))  # exactly the quota
        self.assertEqual(code(ar.quota_ok(150, 51, 200)), "quota_exceeded")
        self.assertEqual(code(ar.quota_ok(201, 0, 200)), "ok")  # nothing new: a hand-in of known files passes
        self.assertTrue(ar.quota_ok(10 ** 12, 10 ** 12, None))
        self.assertTrue(ar.quota_ok(10 ** 12, 10 ** 12, 0))
        self.assertTrue(ar.quota_ok(ar.STUDENT_QUOTA_BYTES - 1, 1))
        self.assertFalse(ar.quota_ok(ar.STUDENT_QUOTA_BYTES, 1))


# --------------------------------------------------------------------------- who sees and who changes them
class VisibilityTests(unittest.TestCase):
    def see(self, item, when=DURING, in_audience=True, **kw):
        return ar.can_view_item_files(PUPIL, item, when, in_audience=in_audience, **kw)

    def test_cases(self):
        hidden = assignment(after_due_statement="hidden")
        cases = [
            ("assignment in the feed", lambda: self.see(assignment()), True),
            ("not addressed", lambda: self.see(assignment(), in_audience=False), False),
            ("draft", lambda: self.see(assignment(state="draft")), False),
            ("archived", lambda: self.see(assignment(state="archived")), False),
            ("before available_from", lambda: self.see(assignment(), when=AVAILABLE - US), False),
            ("at available_from", lambda: self.see(assignment(), when=AVAILABLE), True),
            ("hidden statement, at the due moment", lambda: self.see(hidden, when=DUE), True),
            ("hidden statement, after the due moment", lambda: self.see(hidden, when=DUE + US), False),
            ("hidden statement, extended", lambda: self.see(hidden, when=DUE + US, extensions=[DUE + timedelta(1)]),
             True),
            ("visible statement after the due moment", lambda: self.see(assignment(), when=DUE + US), True),
            ("contest item in the feed", lambda: self.see(contest_item(), contest=OPEN_CONTEST), True),
            ("contest item not addressed", lambda: self.see(contest_item(), in_audience=False, contest=OPEN_CONTEST),
             False),
            ("a material (4v.4)", lambda: self.see(NS(kind="material", state="published", available_from=None)),
             False),
        ]
        for name, call, expected in cases:
            with self.subTest(name):
                self.assertEqual(call(), expected)

    def test_teachers_see_everything(self):
        for item in (assignment(state="draft"), assignment(after_due_statement="hidden"), contest_item()):
            self.assertTrue(ar.can_view_item_files(ADMIN, item, DUE + timedelta(days=30), in_audience=False))

    def test_changes(self):
        self.assertTrue(ar.can_change_attachments(assignment(state="draft")))
        self.assertTrue(ar.can_change_attachments(assignment(state="published")))
        self.assertEqual(code(ar.can_change_attachments(assignment(state="archived"))), "item_archived")


if __name__ == "__main__":
    unittest.main()
