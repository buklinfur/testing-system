"""Rules of the class feed and manual assignments (app/classroom/access.py, grading.py; docs/CLASSROOM.md §4-§6),
case by case, and that the rules stay pure: no database, no clock, dates only through deadline_rules."""
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

from app.classroom import access, grading  # noqa: E402

US = timedelta(microseconds=1)
AVAILABLE = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)
DURING = AVAILABLE + timedelta(days=1)
ADMIN = NS(id=1, role="admin")
PUPIL = NS(id=2, role="student")
LIVE, ARCHIVED = 10, 11  # group ids: one the pupil is in now (not archived), one archived

RULE_FILES = ("backend/app/classroom/access.py", "backend/app/classroom/grading.py")


def assignment(**kw):
    base = dict(kind="assignment", state="published", available_from=AVAILABLE, due_at=DUE,
                after_due_statement="visible", after_due_submit="late", grading="points", max_points=Decimal(10),
                answer_kind="text", grades_release="manual")
    base.update(kw)
    return NS(**base)


def contest_item(**kw):
    base = dict(kind="contest", state="published", available_from=None, due_at=None, after_due_statement="visible",
                after_due_submit="late")
    base.update(kw)
    return NS(**base)


def contest(**kw):
    base = dict(state="published", access_mode="open", timing_mode="fixed", starts_at=AVAILABLE, ends_at=None,
                personal_duration=None, allow_upsolving=True)
    base.update(kw)
    return NS(**base)


def submit(item=None, **kw):
    args = dict(in_audience=True, text="ответ")
    args.update(kw)
    return access.can_submit_work(PUPIL, item or assignment(), DURING, **args)


# --------------------------------------------------------------------------- purity
class PurityTests(unittest.TestCase):
    STDLIB = {"__future__", "dataclasses", "datetime", "decimal", "typing"}
    # of the backend: deadline_rules and contests.access only (grading uses access of its own package)
    BACKEND = {("..", "deadline_rules"), ("..deadline_rules", "Decision"), ("..contests", "access"), (".", "access")}

    def test_imports_only_deadline_rules_and_contest_access(self):
        for rel in RULE_FILES:
            found = set()
            for node in ast.walk(ast.parse((ROOT / rel).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found |= {(a.name, None) for a in node.names if a.name.split(".")[0] not in self.STDLIB}
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    if node.level or module.split(".")[0] not in self.STDLIB:
                        found |= {(module, a.name) for a in node.names}
            with self.subTest(rel):
                self.assertEqual(sorted(found - self.BACKEND, key=str), [])

    def test_importing_them_loads_no_database_code(self):
        script = ("import sys; import app.classroom.access, app.classroom.grading; "
                  "print(sorted(m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'psycopg', 'fastapi') "
                  "or m in ('app.models', 'app.db', 'app.contests.service')))")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "backend"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_clock_and_no_date_comparison_of_their_own(self):
        """Dates are compared only inside deadline_rules: no comparison here touches `now` or a moment attribute,
        and nothing reads the clock."""
        moment = lambda n: (isinstance(n, ast.Name) and n.id in ("now", "due", "when")) or (  # noqa: E731
            isinstance(n, ast.Attribute) and (n.attr.endswith(("_at", "_from")) or n.attr == "at"))
        for rel in RULE_FILES:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    with self.subTest(rel, line=node.lineno):
                        self.assertFalse(any(moment(n) for n in [node.left, *node.comparators]))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("now", "utcnow", "today", "time", "monotonic"), f"{rel}:{node.lineno}")


# --------------------------------------------------------------------------- audience and the feed
class AudienceTests(unittest.TestCase):
    def test_in_audience(self):
        cases = [
            ("group the pupil is in", [NS(group_id=LIVE, user_id=None)], True),
            ("archived group", [NS(group_id=ARCHIVED, user_id=None)], False),
            ("personally", [NS(group_id=None, user_id=PUPIL.id)], True),
            ("another student", [NS(group_id=None, user_id=99)], False),
            ("a group the pupil is not in", [NS(group_id=12, user_id=None)], False),
            ("nobody", [], False),
            ("group and personally (no doubles)", [NS(group_id=LIVE, user_id=None), NS(group_id=None, user_id=2)],
             True),
        ]
        for name, audience, expected in cases:
            with self.subTest(name):
                self.assertEqual(access.in_audience(PUPIL, audience, [LIVE]), expected)


class FeedMatrixTests(unittest.TestCase):
    AUDIENCE = {"live group": True, "archived group": False, "personal": True, "none": False}
    AUDIENCE_ROWS = {"live group": [NS(group_id=LIVE, user_id=None)], "archived group": [NS(group_id=ARCHIVED,
                                                                                             user_id=None)],
                     "personal": [NS(group_id=None, user_id=PUPIL.id)], "none": []}
    MOMENTS = {"before available_from": AVAILABLE - US, "at available_from": AVAILABLE,
               "after available_from": AVAILABLE + US}

    def test_listed_audience_x_state_x_available(self):
        for (who, rows), state, (when_name, when) in itertools.product(
                self.AUDIENCE_ROWS.items(), ("draft", "published", "archived"), self.MOMENTS.items()):
            item = assignment(state=state)
            listed = access.item_listed(PUPIL, item, when, access.in_audience(PUPIL, rows, [LIVE]))
            expected = self.AUDIENCE[who] and state == "published" and when_name != "before available_from"
            with self.subTest(audience=who, state=state, when=when_name):
                self.assertEqual(listed, expected)
                self.assertTrue(access.item_listed(ADMIN, item, when, False))  # admins see everything

    def test_delayed_publication_without_available_from_is_published_now(self):
        self.assertTrue(access.item_listed(PUPIL, assignment(available_from=None), AVAILABLE - timedelta(days=99),
                                           True))


class ContestItemTests(unittest.TestCase):
    """The contest decides (contests/access.py: can_view_contest or can_see_own_results); the item adds only its
    state, audience and available_from."""

    def listed(self, c, participation=None, in_group=False, in_audience=True, item=None):
        return access.item_listed(PUPIL, item or contest_item(), DURING, in_audience, contest=c,
                                  participation=participation, in_contest_group=in_group)

    def test_cases(self):
        groups = contest(access_mode="groups")
        joined_by_group = NS(joined_via="group", started_at=None)
        cases = [
            ("open contest", dict(c=contest()), True),
            ("draft contest", dict(c=contest(state="draft")), False),
            ("groups contest, pupil in its group", dict(c=groups, in_group=True), True),
            ("groups contest, pupil not in its group", dict(c=groups), False),
            ("removed from the group but took part: own results", dict(c=groups, participation=joined_by_group), True),
            ("archived contest, participant without access", dict(c=contest(state="archived", access_mode="groups"),
                                                                  participation=joined_by_group), True),
            ("not in the item's audience", dict(c=contest(), in_audience=False), False),
            ("draft item", dict(c=contest(), item=contest_item(state="draft")), False),
            ("archived item", dict(c=contest(), item=contest_item(state="archived")), False),
            ("item not available yet", dict(c=contest(), item=contest_item(available_from=DURING + US)), False),
        ]
        for name, kw, expected in cases:
            with self.subTest(name):
                self.assertEqual(self.listed(**kw), expected)

    def test_a_contest_item_needs_its_contest(self):
        with self.assertRaises(ValueError):
            access.item_listed(PUPIL, contest_item(), DURING, True)

    def test_a_contest_item_has_no_statement_and_takes_no_work(self):
        self.assertFalse(access.can_view_assignment(PUPIL, contest_item(), DURING, True))
        self.assertEqual(submit(contest_item()).reason, "not_an_assignment")


# --------------------------------------------------------------------------- statement and own work
class StatementTests(unittest.TestCase):
    def test_hidden_after_due_with_boundaries_and_extension(self):
        hidden = assignment(after_due_statement="hidden")
        for when, ext, expected in ((DUE - US, (), True), (DUE, (), True), (DUE + US, (), False),
                                    (DUE + US, [DUE + timedelta(hours=1)], True),
                                    (DUE + timedelta(hours=1) + US, [DUE + timedelta(hours=1)], False)):
            with self.subTest(when=when, ext=ext):
                self.assertEqual(access.can_view_assignment(PUPIL, hidden, when, True, ext), expected)
        self.assertTrue(access.can_view_assignment(PUPIL, assignment(), DUE + timedelta(days=30), True))
        self.assertTrue(access.can_view_assignment(ADMIN, hidden, DUE + US, False))

    def test_hidden_statement_hides_it_from_a_student_with_work_but_not_the_work(self):
        hidden = assignment(after_due_statement="hidden")
        self.assertFalse(access.can_view_assignment(PUPIL, hidden, DUE + US, True))
        self.assertTrue(access.can_see_own_work(PUPIL, hidden, has_work=True))
        view = grading.student_view(hidden, DUE + US, [("submission", NS(late=False))])
        self.assertEqual((view["status"], view["submissions"]), ("submitted", 1))

    def test_statement_needs_the_feed(self):
        self.assertFalse(access.can_view_assignment(PUPIL, assignment(), DURING, False))
        self.assertFalse(access.can_view_assignment(PUPIL, assignment(), AVAILABLE - US, True))

    def test_archived_item_out_of_the_feed_but_own_work_and_released_grade_visible(self):
        item = assignment(state="archived")
        self.assertFalse(access.item_listed(PUPIL, item, DURING, True))
        self.assertFalse(access.can_view_assignment(PUPIL, item, DURING, True))
        self.assertEqual(submit(item).reason, "item_archived")
        self.assertTrue(access.can_see_own_work(PUPIL, item, has_work=True))
        grade = NS(id=1, action="graded", score=Decimal(7), passed=None, comment="ok", at=DURING)
        view = grading.student_view(item, DURING, [("submission", NS(late=False)), ("review", grade),
                                                   ("review", NS(id=2, action="released", release_of=1))])
        self.assertEqual((view["status"], view["grade"]["score"]), ("graded", Decimal(7)))

    def test_own_work(self):
        cases = [("published, has work", assignment(), True, True), ("archived, has work", assignment(state="archived"),
                                                                       True, True),
                 ("draft", assignment(state="draft"), True, False), ("no work", assignment(), False, False)]
        for name, item, has_work, expected in cases:
            with self.subTest(name):
                self.assertEqual(access.can_see_own_work(PUPIL, item, has_work), expected)
        self.assertTrue(access.can_see_own_work(ADMIN, assignment(state="draft"), False))


# --------------------------------------------------------------------------- submitting
class SubmitTests(unittest.TestCase):
    def test_every_refusal(self):
        cases = [
            ("not_a_student", lambda: access.can_submit_work(ADMIN, assignment(), DURING, in_audience=True,
                                                             text="x")),
            ("not_an_assignment", lambda: submit(contest_item())),
            ("not_published", lambda: submit(assignment(state="draft"))),
            ("item_archived", lambda: submit(assignment(state="archived"))),
            ("no_access", lambda: submit(in_audience=False)),
            ("not_available_yet", lambda: access.can_submit_work(PUPIL, assignment(available_from=DURING + US),
                                                                 DURING, in_audience=True, text="x")),
            ("deadline_passed", lambda: access.can_submit_work(PUPIL, assignment(after_due_submit="no"), DUE + US,
                                                               in_audience=True, text="x")),
            ("already_graded", lambda: submit(work_status="graded")),
            ("answer_required", lambda: submit(text="  \n")),
            ("answer_required", lambda: submit(text=None)),
            ("text_not_expected", lambda: submit(assignment(answer_kind="mark"), text="тетрадь")),
            ("text_too_long", lambda: submit(text="я" * 6, text_limit=11)),  # 12 bytes in UTF-8
        ]
        for code, call in cases:
            with self.subTest(code):
                decision = call()
                self.assertFalse(decision)
                self.assertEqual(decision.reason, code)

    def test_accepted(self):
        for name, decision in (("text", submit()), ("mark", submit(assignment(answer_kind="mark"), text=None)),
                               ("again after a hand-in", submit(work_status="submitted")),
                               ("after a return", submit(work_status="returned")),
                               ("exactly the limit", submit(text="я" * 6, text_limit=12)),
                               ("no deadline", submit(assignment(due_at=None)))):
            with self.subTest(name):
                self.assertTrue(decision)
                self.assertEqual((decision.reason, decision.late), ("ok", False))

    def test_order_when_several_conditions_fail(self):
        """The first failing check of the fixed order wins: not_published, item_archived, no_access,
        not_available_yet, the deadline, already_graded, the answer."""
        everything_wrong = dict(in_audience=False, work_status="graded", text="")
        late_item = dict(available_from=DURING + US, after_due_submit="no", due_at=DURING + timedelta(hours=1))
        steps = [
            ("not_published", assignment(state="draft", **late_item), everything_wrong),
            ("item_archived", assignment(state="archived", **late_item), everything_wrong),
            ("no_access", assignment(**late_item), everything_wrong),
            ("not_available_yet", assignment(**late_item), dict(everything_wrong, in_audience=True)),
        ]
        for code, item, kw in steps:
            with self.subTest(code):
                self.assertEqual(access.can_submit_work(PUPIL, item, DURING, **kw).reason, code)
        after = DUE + US
        self.assertEqual(access.can_submit_work(PUPIL, assignment(after_due_submit="no"), after, in_audience=True,
                                                work_status="graded", text="").reason, "deadline_passed")
        self.assertEqual(access.can_submit_work(PUPIL, assignment(), after, in_audience=True, work_status="graded",
                                                text="").reason, "already_graded")
        self.assertEqual(access.can_submit_work(PUPIL, assignment(), after, in_audience=True, text="").reason,
                         "answer_required")

    def test_deadline_policies_at_the_boundary_with_extensions(self):
        ext = DUE + timedelta(hours=2)
        for policy, past in (("no", (False, "deadline_passed", False)), ("late", (True, "late", True)),
                             ("yes", (True, "ok", False))):
            for when, extensions, expected in (
                    (DUE - US, (), (True, "ok", False)), (DUE, (), (True, "ok", False)), (DUE + US, (), past),
                    (ext, [ext], (True, "ok", False)), (ext + US, [ext], past)):
                with self.subTest(policy=policy, when=when, extensions=extensions):
                    d = access.can_submit_work(PUPIL, assignment(after_due_submit=policy), when, in_audience=True,
                                               extensions=extensions, text="x")
                    self.assertEqual((d.allowed, d.reason, d.late), expected)

    def test_available_from_boundary(self):
        self.assertEqual(access.can_submit_work(PUPIL, assignment(), AVAILABLE - US, in_audience=True,
                                                text="x").reason, "not_available_yet")
        self.assertTrue(access.can_submit_work(PUPIL, assignment(), AVAILABLE, in_audience=True, text="x"))


# --------------------------------------------------------------------------- statuses
class StatusTests(unittest.TestCase):
    EXPECTED = {  # every (status, event): the new status, or the refusal code
        (None, "submit"): "submitted", (None, "grade"): "graded", (None, "return"): "!nothing_to_return",
        (None, "release"): "!nothing_to_release",
        ("submitted", "submit"): "submitted", ("submitted", "grade"): "graded", ("submitted", "return"): "returned",
        ("submitted", "release"): "!nothing_to_release",
        ("graded", "submit"): "!already_graded", ("graded", "grade"): "graded", ("graded", "return"): "returned",
        ("graded", "release"): "graded",
        ("returned", "submit"): "submitted", ("returned", "grade"): "graded", ("returned", "return"): "!already_returned",
        ("returned", "release"): "!nothing_to_release",
    }

    def test_every_pair(self):
        pairs = list(itertools.product(grading.STATUSES, grading.EVENTS))
        self.assertEqual(sorted(pairs, key=str), sorted(self.EXPECTED, key=str), "the table covers every pair")
        for (status, event), expected in self.EXPECTED.items():
            with self.subTest(status=status, event=event):
                t = grading.next_status(status, event)
                if expected.startswith("!"):
                    self.assertFalse(t)
                    self.assertEqual((t.reason, t.status), (expected[1:], None))
                else:
                    self.assertTrue(t)
                    self.assertEqual(t.status, expected)

    def test_enum_members_and_unknown_events(self):
        self.assertEqual(grading.next_status(NS(value="returned"), "submit").status, "submitted")
        with self.assertRaises(ValueError):
            grading.next_status("submitted", "withdraw")  # a student cannot take a hand-in back


class GradeCheckTests(unittest.TestCase):
    def test_scales(self):
        points, pass_fail, none = assignment(), assignment(grading="pass_fail", max_points=None), \
            assignment(grading="none", max_points=None)
        cases = [
            (points, Decimal(0), None, "ok"), (points, Decimal(10), None, "ok"), (points, Decimal("9.5"), None, "ok"),
            (points, Decimal(-1), None, "score_out_of_range"), (points, Decimal("10.01"), None, "score_out_of_range"),
            (points, None, None, "score_required"), (points, Decimal(5), True, "grade_kind_mismatch"),
            (points, None, False, "grade_kind_mismatch"),
            (pass_fail, None, True, "ok"), (pass_fail, None, False, "ok"), (pass_fail, None, None, "passed_required"),
            (pass_fail, Decimal(1), True, "grade_kind_mismatch"),
            (none, None, None, "ok"), (none, Decimal(1), None, "grade_not_expected"),
            (none, None, True, "grade_not_expected"),
        ]
        for item, score, passed, code in cases:
            with self.subTest(grading=item.grading, score=score, passed=passed):
                d = grading.check_grade(item, score, passed)
                self.assertEqual((bool(d), d.reason), (code == "ok", code))


# --------------------------------------------------------------------------- what a student sees
def graded(id, score=None, passed=None, comment=None):
    return ("review", NS(id=id, action="graded", score=None if score is None else Decimal(score), passed=passed,
                         comment=comment, at=DURING))


def released(id, of):
    return ("review", NS(id=id, action="released", release_of=of, score=None, passed=None, comment=None, at=DURING))


def returned(id, comment="переделай"):
    return ("review", NS(id=id, action="returned", score=None, passed=None, comment=comment, at=DURING))


def handed(late=False):
    return ("submission", NS(late=late))


def reviews(events):
    return [row for kind, row in events if kind == "review"]


class ReleaseTests(unittest.TestCase):
    def test_release_target(self):
        manual = assignment()
        self.assertEqual(grading.release_target(assignment(grades_release="immediate"), reviews([graded(1, 5)])),
                         (None, "release_not_needed"))
        self.assertEqual(grading.release_target(manual, []), (None, "nothing_to_release"))
        row, code = grading.release_target(manual, reviews([graded(1, 5), released(2, 1), graded(3, 6)]))
        self.assertEqual((row.id, code), (3, None))  # the regrade
        self.assertEqual(grading.release_target(manual, reviews([graded(1, 5), released(2, 1)])),
                         (None, "already_released"))


class StudentViewTests(unittest.TestCase):
    def view(self, events, item=None, now=DURING, extensions=()):
        return grading.student_view(item or assignment(), now, events, extensions)

    def test_manual_rule_1_sees_the_last_released_grade(self):
        v = self.view([handed(), graded(1, 7, comment="хорошо")])
        self.assertEqual((v["status"], v["grade"]), ("submitted", None))  # not released yet: still «сдано»
        v = self.view([handed(), graded(1, 7, comment="хорошо"), released(2, 1)])
        self.assertEqual((v["status"], v["grade"]["score"], v["grade"]["comment"]), ("graded", Decimal(7), "хорошо"))

    def test_manual_rule_2_regrade_after_release_hidden_until_released(self):
        events = [handed(), graded(1, 7), released(2, 1), graded(3, 9)]
        v = self.view(events)
        self.assertEqual((v["status"], v["grade"]["score"]), ("graded", Decimal(7)))
        v = self.view(events + [released(4, 3)])
        self.assertEqual((v["status"], v["grade"]["score"]), ("graded", Decimal(9)))

    def test_manual_rule_3_return_seen_at_once_grades_stay(self):
        v = self.view([handed(), returned(1, "добавь вывод")])
        self.assertEqual((v["status"], v["returned_comment"], v["grade"]), ("returned", "добавь вывод", None))
        # a return after a released grade: the return is seen, the released grade stays
        v = self.view([handed(), graded(1, 4), released(2, 1), returned(3, "переделай")])
        self.assertEqual((v["status"], v["returned_comment"], v["grade"]["score"]),
                         ("returned", "переделай", Decimal(4)))
        # an unreleased grade after the return is not seen: the student still sees the return
        v = self.view([handed(), graded(1, 4), released(2, 1), returned(3), graded(4, 6)])
        self.assertEqual((v["status"], v["grade"]["score"]), ("returned", Decimal(4)))
        # handed in again after the return: «сдано», the comment of the return is gone
        v = self.view([handed(), returned(1), handed()])
        self.assertEqual((v["status"], v["returned_comment"]), ("submitted", None))

    def test_immediate_sees_every_grade_at_once(self):
        item = assignment(grades_release="immediate")
        v = self.view([handed(), graded(1, 7)], item)
        self.assertEqual((v["status"], v["grade"]["score"]), ("graded", Decimal(7)))
        v = self.view([handed(), graded(1, 7), graded(2, 8)], item)
        self.assertEqual(v["grade"]["score"], Decimal(8))
        v = self.view([handed(), graded(1, 7), returned(2)], item)
        self.assertEqual((v["status"], v["grade"]["score"]), ("returned", Decimal(7)))

    def test_graded_without_a_hand_in(self):
        v = self.view([graded(1, 0, comment="не сдано")], assignment(grades_release="immediate"))
        self.assertEqual((v["status"], v["submissions"], v["grade"]["score"]), ("graded", 0, Decimal(0)))
        v = self.view([graded(1, 0)])  # manual, not released: nothing to see yet
        self.assertEqual((v["status"], v["grade"]), (None, None))

    def test_pass_fail_and_none_grades(self):
        v = self.view([handed(), graded(1, passed=True)], assignment(grading="pass_fail", max_points=None,
                                                                     grades_release="immediate"))
        self.assertEqual((v["grade"]["passed"], v["grade"]["score"]), (True, None))
        v = self.view([handed(), graded(1)], assignment(grading="none", max_points=None, grades_release="immediate"))
        self.assertEqual((v["status"], v["grade"]["score"], v["grade"]["passed"]), ("graded", None, None))

    def test_grading_none_is_seen_at_once_whatever_grades_release(self):
        """«Принято» has nothing to hold back: no release needed, even with grades_release = manual."""
        item = assignment(grading="none", max_points=None, grades_release="manual")
        v = self.view([handed(), graded(1, comment="принято")], item)
        self.assertEqual((v["status"], v["grade"]["comment"]), ("graded", "принято"))
        self.assertEqual(grading.release_target(item, reviews([graded(1)])), (None, "release_not_needed"))

    def test_late_and_missing_are_computed_flags(self):
        self.assertEqual(self.view([handed(late=False), handed(late=True)])["late"], True)
        self.assertEqual(self.view([handed(late=True), handed(late=False)])["late"], False)  # the latest counts
        self.assertEqual(self.view([])["late"], False)
        for now, extensions, has, expected in ((DUE, (), False, False), (DUE + US, (), False, True),
                                               (DUE + US, (), True, False),
                                               (DUE + US, [DUE + timedelta(hours=1)], False, False),
                                               (DUE + timedelta(days=9), (), False, True)):
            with self.subTest(now=now, extensions=extensions, has=has):
                v = self.view([handed()] if has else [], now=now, extensions=extensions)
                self.assertEqual(v["missing"], expected)
        self.assertFalse(self.view([], assignment(due_at=None), now=DUE + timedelta(days=99))["missing"])


if __name__ == "__main__":
    unittest.main()
