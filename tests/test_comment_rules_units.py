"""Rules of comment threads (app/classroom/comment_rules.py; docs/CLASSROOM.md §9, D-074): the table of cases
(audience x state x archived group x removed from the group x hidden statement x comments_enabled x kind of item x
author), the order of the codes when several conditions fail, the body, the rate, deletion - and that the module
stays pure. What check_body accepts the database accepts too: tests/pg/test_comment_schema_pg.py."""
import ast
import itertools
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.classroom import access, comment_rules as cr  # noqa: E402

US = timedelta(microseconds=1)
AVAILABLE = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)
DURING = AVAILABLE + timedelta(days=1)
AFTER_DUE = DUE + US
ADMIN = NS(id=1, role="admin")
PUPIL = NS(id=2, role="student")
OTHER = NS(id=3, role="student")
GROUP = 10
BODY = "Можно сдать в тетради?"
RULE_FILE = "backend/app/classroom/comment_rules.py"


def assignment(**kw):
    base = dict(kind="assignment", state="published", available_from=AVAILABLE, due_at=DUE,
                after_due_statement="visible", after_due_submit="late", grading="points", max_points=Decimal(10),
                answer_kind="text", grades_release="manual", comments_enabled=True)
    base.update(kw)
    return NS(**base)


def contest_item(**kw):
    base = dict(kind="contest", state="published", available_from=None, due_at=None, after_due_statement="visible",
                after_due_submit="late", comments_enabled=True)
    base.update(kw)
    return NS(**base)


OPEN_CONTEST = NS(state="published", access_mode="open", timing_mode="fixed", starts_at=AVAILABLE, ends_at=None,
                  personal_duration=None, allow_upsolving=True)

# how the pupil stands to an item addressed to GROUP: the audience boolean is made by access.in_audience, as the
# service makes it (current groups that are not archived)
GROUP_AUDIENCE = [NS(group_id=GROUP, user_id=None)]
SITUATIONS = {
    "in the group": (GROUP_AUDIENCE, {GROUP}),
    "addressed personally": ([NS(group_id=None, user_id=PUPIL.id)], set()),
    "removed from the group": (GROUP_AUDIENCE, set()),
    "the group archived": (GROUP_AUDIENCE, set()),  # an archived group is not among the live ones
    "not addressed": ([NS(group_id=None, user_id=OTHER.id)], {GROUP}),
}


def audience(situation):
    rows, live = SITUATIONS[situation]
    return access.in_audience(PUPIL, rows, live)


def shared(user, item, when=DURING, situation="in the group", body=BODY, **kw):
    kw.setdefault("contest", OPEN_CONTEST if item.kind == "contest" else None)
    return cr.can_post_shared(user, item, when, in_audience=audience(situation), body=body, **kw)


def read_shared(user, item, when=DURING, situation="in the group", **kw):
    kw.setdefault("contest", OPEN_CONTEST if item.kind == "contest" else None)
    return cr.can_read_shared(user, item, when, in_audience=audience(situation), **kw)


def private(user, item, student=PUPIL, when=DURING, situation="in the group", body=BODY, **kw):
    return cr.can_post_private(user, item, student, when, in_audience=audience(situation), body=body, **kw)


def read_private(user, item, student=PUPIL, when=DURING, situation="in the group", thread_exists=False):
    return cr.can_read_private(user, item, student, when, in_audience=audience(situation),
                               thread_exists=thread_exists)


# --------------------------------------------------------------------------- purity
class PurityTests(unittest.TestCase):
    STDLIB = {"__future__", "datetime", "typing", "unicodedata"}
    BACKEND = {("..", "deadline_rules"), ("..deadline_rules", "Decision"), (".", "access")}

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
        script = ("import sys; import app.classroom.comment_rules; "
                  "print(sorted(m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'psycopg', 'fastapi') "
                  "or m in ('app.models', 'app.db', 'app.contests.service', 'app.settings')))")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "backend"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_clock_and_no_date_comparison_of_its_own(self):
        """Dates are compared only in deadline_rules (through access); `is None` on a moment is no comparison."""
        moment = lambda n: (isinstance(n, ast.Name) and n.id in ("now", "due", "when")) or (  # noqa: E731
            isinstance(n, ast.Attribute) and (n.attr.endswith(("_at", "_from")) or n.attr == "at"))
        for node in ast.walk(ast.parse((ROOT / RULE_FILE).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Compare) and not all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
                with self.subTest(line=node.lineno):
                    self.assertFalse(any(moment(n) for n in [node.left, *node.comparators]))
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("now", "utcnow", "today", "time", "monotonic"), node.lineno)

    def test_the_length_is_the_check_of_the_table(self):
        from app import models  # the rules must not import it; the test may
        self.assertEqual(cr.COMMENT_MAX_CHARS, models.COMMENT_MAX_CHARS)


# --------------------------------------------------------------------------- the shared thread: named cases
class SharedThreadTests(unittest.TestCase):
    def test_cases(self):
        hidden = dict(after_due_statement="hidden")
        cases = [  # (name, user, item, kwargs, code of posting, may read)
            ("pupil in the group, comments on", PUPIL, assignment(), {}, "ok", True),
            ("pupil addressed personally", PUPIL, assignment(), dict(situation="addressed personally"), "ok", True),
            ("comments off: reads, cannot write", PUPIL, assignment(comments_enabled=False), {}, "comments_disabled",
             True),
            ("teacher writes with comments off", ADMIN, assignment(comments_enabled=False), {}, "ok", True),
            ("removed from the group", PUPIL, assignment(), dict(situation="removed from the group"), "no_access",
             False),
            ("the group archived", PUPIL, assignment(), dict(situation="the group archived"), "no_access", False),
            ("not addressed", PUPIL, assignment(), dict(situation="not addressed"), "no_access", False),
            ("draft: nobody writes", PUPIL, assignment(state="draft"), {}, "not_published", False),
            ("draft: not the teacher either", ADMIN, assignment(state="draft"), {}, "not_published", True),
            ("archived item", PUPIL, assignment(state="archived"), {}, "item_archived", False),
            ("archived item: the teacher writes", ADMIN, assignment(state="archived"), {}, "ok", True),
            ("before available_from", PUPIL, assignment(), dict(when=AVAILABLE - US), "not_available_yet", False),
            ("at available_from", PUPIL, assignment(), dict(when=AVAILABLE), "ok", True),
            ("statement hidden after due", PUPIL, assignment(**hidden), dict(when=AFTER_DUE), "no_access", False),
            ("statement hidden, extension not over", PUPIL, assignment(**hidden),
             dict(when=AFTER_DUE, extensions=[AFTER_DUE + US]), "ok", True),
            ("statement visible after due", PUPIL, assignment(), dict(when=AFTER_DUE), "ok", True),
            ("statement hidden: the teacher writes", ADMIN, assignment(**hidden), dict(when=AFTER_DUE), "ok", True),
            ("contest item: students never write", PUPIL, contest_item(), {}, "comments_teacher_only", True),
            ("contest item, comments off too", PUPIL, contest_item(comments_enabled=False), {},
             "comments_teacher_only", True),
            ("contest item: the teacher writes", ADMIN, contest_item(), {}, "ok", True),
            ("contest item, contest not visible", PUPIL, contest_item(),
             dict(contest=NS(**{**vars(OPEN_CONTEST), "state": "draft"})), "no_access", False),
            ("contest item, removed from the group", PUPIL, contest_item(),
             dict(situation="removed from the group"), "no_access", False),
            ("a material (from 4v.4) has no thread for students", PUPIL, NS(kind="material", state="published",
                                                                            available_from=None,
                                                                            comments_enabled=True),
             {}, "no_access", False),
        ]
        for name, user, item, kw, code, readable in cases:
            with self.subTest(name):
                self.assertEqual(shared(user, item, **kw).reason, code)
                read_kw = {k: v for k, v in kw.items() if k != "body"}
                self.assertEqual(read_shared(user, item, **read_kw), readable)

    def test_order_when_several_conditions_fail(self):
        """Each line breaks one more condition than the next: the first in the documented order wins."""
        steps = [
            ("not_published", PUPIL, contest_item(state="draft", comments_enabled=False),
             dict(situation="not addressed", body="")),
            ("item_archived", PUPIL, contest_item(state="archived", comments_enabled=False),
             dict(situation="not addressed", body="")),
            ("no_access", PUPIL, contest_item(comments_enabled=False, available_from=DURING + US),
             dict(situation="not addressed", body="")),
            ("not_available_yet", PUPIL, contest_item(comments_enabled=False, available_from=DURING + US),
             dict(body="")),
            ("comments_teacher_only", PUPIL, contest_item(comments_enabled=False), dict(body="", recent=99)),
            ("comments_disabled", PUPIL, assignment(comments_enabled=False), dict(body="", recent=99)),
            ("comment_required", PUPIL, assignment(), dict(body="", recent=99)),
            ("rate_limited", PUPIL, assignment(), dict(recent=99)),
            ("ok", PUPIL, assignment(), dict(recent=9)),
        ]
        for code, user, item, kw in steps:
            with self.subTest(code):
                self.assertEqual(shared(user, item, **kw).reason, code)

    def test_teachers_have_no_rate_limit_but_the_same_body_rules(self):
        self.assertTrue(shared(ADMIN, assignment(), recent=10_000))
        self.assertEqual(shared(ADMIN, assignment(), body=" ").reason, "comment_required")
        self.assertEqual(shared(ADMIN, assignment(), body="я" * 4001).reason, "comment_too_long")


# --------------------------------------------------------------------------- the whole table, by invariants
class SharedMatrixTests(unittest.TestCase):
    """Every combination of audience situation x state x hidden statement x flag x kind x author, checked against
    the rules stated in CLASSROOM.md §9 (not against a copy of the code)."""

    def test_matrix(self):
        product = itertools.product(SITUATIONS, ("draft", "published", "archived"), (False, True), (False, True),
                                    ("assignment", "contest"), ("student", "teacher"))
        n = 0
        for situation, state, hidden, enabled, kind, author in product:
            n += 1
            user = ADMIN if author == "teacher" else PUPIL
            make = assignment if kind == "assignment" else contest_item
            item = make(state=state, comments_enabled=enabled,
                        **({"after_due_statement": "hidden"} if hidden and kind == "assignment" else {}))
            when = AFTER_DUE
            posted = shared(user, item, when=when, situation=situation)
            readable = read_shared(user, item, when=when, situation=situation)
            with self.subTest(situation=situation, state=state, hidden=hidden, enabled=enabled, kind=kind,
                              author=author, code=posted.reason):
                in_audience = situation in ("in the group", "addressed personally")
                if author == "teacher":
                    self.assertTrue(readable)
                    self.assertEqual(bool(posted), state != "draft")
                    continue
                visible = state == "published" and in_audience and not (hidden and kind == "assignment")
                self.assertEqual(readable, visible)
                self.assertEqual(bool(posted), visible and kind == "assignment" and enabled)
                if posted:
                    self.assertTrue(readable, "whoever writes reads")
                if kind == "contest" and visible:
                    self.assertEqual(posted.reason, "comments_teacher_only")
        self.assertEqual(n, 5 * 3 * 2 * 2 * 2 * 2)


# --------------------------------------------------------------------------- the private thread
class PrivateThreadTests(unittest.TestCase):
    def test_student_cases(self):
        hidden = assignment(after_due_statement="hidden", comments_enabled=False)
        cases = [  # (name, item, kwargs, code of posting)
            ("own thread, in the group", assignment(), {}, "ok"),
            ("comments_enabled does not matter", assignment(comments_enabled=False), {}, "ok"),
            ("after the due moment, statement hidden", hidden, dict(when=AFTER_DUE), "ok"),
            ("removed from the group", assignment(), dict(situation="removed from the group"), "no_access"),
            ("the group archived", assignment(), dict(situation="the group archived"), "no_access"),
            ("archived item", assignment(state="archived"), {}, "item_archived"),
            ("draft", assignment(state="draft"), {}, "not_published"),
            ("before available_from", assignment(), dict(when=AVAILABLE - US), "not_available_yet"),
            ("a contest item has no private thread", contest_item(), {}, "not_an_assignment"),
            ("another student's thread", assignment(), dict(student=OTHER), "no_access"),
        ]
        for name, item, kw, code in cases:
            with self.subTest(name):
                self.assertEqual(private(PUPIL, item, **kw).reason, code)

    def test_teacher_cases(self):
        cases = [
            ("addressee", assignment(), {}, "ok"),
            ("archived item", assignment(state="archived"), {}, "ok"),
            ("removed from the group, has work", assignment(), dict(situation="removed from the group",
                                                                    has_work=True), "ok"),
            ("removed from the group, thread exists", assignment(), dict(situation="removed from the group",
                                                                         thread_exists=True), "ok"),
            ("not addressed, no work, no thread", assignment(), dict(situation="not addressed"), "not_addressed"),
            ("the student is an admin", assignment(), dict(student=NS(id=9, role="admin")), "not_a_student"),
            ("draft", assignment(state="draft"), {}, "not_published"),
            ("contest item", contest_item(), {}, "not_an_assignment"),
            ("blank body", assignment(), dict(body="​ \n"), "comment_required"),
            ("no rate limit", assignment(), dict(recent=10_000), "ok"),
        ]
        for name, item, kw, code in cases:
            with self.subTest(name):
                self.assertEqual(private(ADMIN, item, **kw).reason, code)

    def test_order_for_a_student(self):
        steps = [
            ("no_access", assignment(state="draft"), dict(student=OTHER, situation="not addressed", body="")),
            ("not_an_assignment", contest_item(state="draft"), dict(situation="not addressed", body="")),
            ("not_published", assignment(state="draft"), dict(situation="not addressed", body="")),
            ("item_archived", assignment(state="archived"), dict(situation="not addressed", body="")),
            ("no_access", assignment(available_from=DURING + US), dict(situation="not addressed", body="")),
            ("not_available_yet", assignment(available_from=DURING + US), dict(body="", recent=99)),
            ("comment_required", assignment(), dict(body="", recent=99)),
            ("rate_limited", assignment(), dict(recent=10)),
        ]
        for code, item, kw in steps:
            with self.subTest(code):
                self.assertEqual(private(PUPIL, item, **kw).reason, code)

    def test_reading(self):
        cases = [  # (name, user, item, kwargs, expected)
            ("own, in the feed, no thread yet", PUPIL, assignment(), {}, True),
            ("own, removed from the group, thread exists", PUPIL, assignment(),
             dict(situation="removed from the group", thread_exists=True), True),
            ("own, removed from the group, no thread", PUPIL, assignment(),
             dict(situation="removed from the group"), False),
            ("own, the group archived, thread exists", PUPIL, assignment(),
             dict(situation="the group archived", thread_exists=True), True),
            ("own, archived item, thread exists", PUPIL, assignment(state="archived"), dict(thread_exists=True), True),
            ("own, archived item, no thread", PUPIL, assignment(state="archived"), {}, False),
            ("own, statement hidden after due, thread exists", PUPIL, assignment(after_due_statement="hidden"),
             dict(when=AFTER_DUE, thread_exists=True), True),
            ("own, draft", PUPIL, assignment(state="draft"), dict(thread_exists=True), False),
            ("another student's, even existing", PUPIL, assignment(), dict(student=OTHER, thread_exists=True), False),
            ("contest item", PUPIL, contest_item(), dict(thread_exists=True), False),
            ("teacher, any", ADMIN, assignment(state="archived"), dict(student=OTHER, situation="not addressed"),
             True),
        ]
        for name, user, item, kw, expected in cases:
            with self.subTest(name):
                self.assertEqual(read_private(user, item, **kw), expected)


# --------------------------------------------------------------------------- deletion and visibility
class DeleteTests(unittest.TestCase):
    def comment(self, author=PUPIL.id, deleted=False):
        return NS(author_id=author, deleted_at=DURING if deleted else None)

    def test_cases(self):
        cases = [  # (name, user, comment, can_read, code)
            ("own", PUPIL, self.comment(), True, "ok"),
            ("own, already deleted", PUPIL, self.comment(deleted=True), True, "already_deleted"),
            ("someone else's", PUPIL, self.comment(author=OTHER.id), True, "not_your_comment"),
            ("a teacher's", PUPIL, self.comment(author=ADMIN.id), True, "not_your_comment"),
            ("from the console", PUPIL, self.comment(author=None), True, "not_your_comment"),
            ("own, in a thread no longer readable", PUPIL, self.comment(), False, "no_access"),
            ("someone else's, not readable: no_access first", PUPIL, self.comment(author=OTHER.id, deleted=True),
             False, "no_access"),
            ("teacher: anyone's", ADMIN, self.comment(author=OTHER.id), False, "ok"),
            ("teacher: already deleted", ADMIN, self.comment(deleted=True), True, "already_deleted"),
        ]
        for name, user, comment, can_read, code in cases:
            with self.subTest(name):
                self.assertEqual(cr.can_delete(user, comment, can_read=can_read).reason, code)

    def test_deleted_is_gone_for_students_only(self):
        gone = self.comment(deleted=True)
        self.assertFalse(cr.visible_to(PUPIL, gone))
        self.assertFalse(cr.visible_to(OTHER, gone))
        self.assertTrue(cr.visible_to(ADMIN, gone))
        self.assertTrue(cr.visible_to(OTHER, self.comment()))


# --------------------------------------------------------------------------- the body and the rate
class BodyTests(unittest.TestCase):
    def test_codes(self):
        cases = [
            (None, "comment_required"), ("", "comment_required"), (" \t\r\n", "comment_required"),
            ("  ", "comment_required"), ("　", "comment_required"), ("​", "comment_required"),
            ("﻿‎⁦", "comment_required"), ("\x1b\x07", "comment_required"),
            ("  ", "comment_required"),
            ("a\x00b", "comment_invalid"), ("\x00", "comment_invalid"), ("a\ud800", "comment_invalid"),
            ("\udfff", "comment_invalid"), (b"bytes", "comment_invalid"),
            ("я" * 4000, "ok"), ("я" * 4001, "comment_too_long"), ("😀" * 4000, "ok"), ("😀" * 4001, "comment_too_long"),
            ("е́" * 2000, "ok"), (" " * 3999 + "x", "ok"), ("\n" * 5000, "comment_required"),
            ("<script>alert(1)</script>", "ok"), ("`a < b`", "ok"), ("\x1b[31mкрасный", "ok"), (" да", "ok"),
        ]
        for text, code in cases:
            with self.subTest(text=repr(text)[:40]):
                self.assertEqual(cr.check_body(text).reason, code)

    def test_invalid_comes_before_blank_and_long(self):
        self.assertEqual(cr.check_body("\x00" + " " * 5000).reason, "comment_invalid")
        self.assertEqual(cr.check_body(" " * 5000).reason, "comment_required")

    def test_rate(self):
        self.assertTrue(cr.rate_ok(9, 10))
        self.assertEqual(cr.rate_ok(10, 10).reason, "rate_limited")
        self.assertTrue(cr.rate_ok(10_000, None))
        self.assertTrue(cr.rate_ok(10_000, 0))
        self.assertEqual((cr.RATE_WINDOW_S, cr.RATE_PER_WINDOW), (60, 10))


if __name__ == "__main__":
    unittest.main()
