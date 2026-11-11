"""Rules of the library of materials (app/materials/rules.py, links.py; docs/CLASSROOM.md §11, D-083): the grammar of
the links [[памятка:slug]], what a teacher types (slug, title, body, sections, order), who opens a material - the
matrix of bases x state of the item or the problem x deadline and hidden statement x archived material - and that the
modules stay pure."""
import ast
import itertools
import json
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contests import access as contest_access  # noqa: E402
from app.materials import links, rules  # noqa: E402
from app.materials.rules import ItemBasis, ProblemBasis  # noqa: E402

US = timedelta(microseconds=1)
AVAILABLE = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)
DURING = AVAILABLE + timedelta(days=1)
AFTER_DUE = DUE + timedelta(days=3)
ADMIN = NS(id=1, role="admin")
PUPIL = NS(id=2, role="student")
GROUP, OTHER_GROUP = 10, 11
RULE_FILES = ("backend/app/materials/rules.py", "backend/app/materials/links.py")


def material(**kw):
    base = dict(id=7, slug="cycles", title="Циклы", library="none", archived_at=None)
    base.update(kw)
    return NS(**base)


ARCHIVED = datetime(2026, 11, 1, tzinfo=timezone.utc)


def item(kind="material", **kw):
    base = dict(kind=kind, state="published", available_from=AVAILABLE, due_at=None, after_due_statement="visible",
                after_due_submit="late")
    if kind == "assignment":
        base.update(due_at=DUE)
    base.update(kw)
    return NS(**base)


def contest(**kw):
    base = dict(state="published", timing_mode="fixed", starts_at=AVAILABLE + timedelta(days=2), ends_at=AFTER_DUE,
                personal_duration=None, allow_upsolving=True, access_mode="groups", invite_code="ABC")
    base.update(kw)
    return NS(**base)


def contest_problem(**kw):
    base = dict(hidden=False, available_from=AVAILABLE, due_at=DUE, after_due_statement="visible",
                after_due_submit="late")
    base.update(kw)
    return NS(**base)


def basis(user=PUPIL, m=None, when=DURING, **kw):
    return rules.open_basis(user, m or material(), when, **kw)


# --------------------------------------------------------------------------- purity
class PurityTests(unittest.TestCase):
    STDLIB = {"__future__", "datetime", "typing", "unicodedata", "re", "dataclasses"}
    BACKEND = {
        "backend/app/materials/rules.py": {("..classroom", "access"), ("..contests", "access"),
                                           ("..deadline_rules", "Decision")},
        "backend/app/materials/links.py": set(),
    }

    def test_imports_only_the_access_rules(self):
        for rule_file in RULE_FILES:
            found = set()
            for node in ast.walk(ast.parse((ROOT / rule_file).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found |= {(a.name, None) for a in node.names if a.name.split(".")[0] not in self.STDLIB}
                elif isinstance(node, ast.ImportFrom):
                    module = "." * node.level + (node.module or "")
                    if node.level or module.split(".")[0] not in self.STDLIB:
                        found |= {(module, a.name) for a in node.names}
            with self.subTest(rule_file):
                self.assertEqual(sorted(found - self.BACKEND[rule_file], key=str), [])

    def test_importing_them_loads_no_database_code(self):
        script = ("import sys; import app.materials.rules, app.materials.links; "
                  "print(sorted(m for m in sys.modules if m.split('.')[0] in ('sqlalchemy', 'psycopg', 'fastapi') "
                  "or m in ('app.models', 'app.db', 'app.contests.service', 'app.settings')))")
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                           env={"PYTHONPATH": str(ROOT / "backend"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "[]")

    def test_no_clock_and_no_date_comparison_of_their_own(self):
        moment = lambda n: (isinstance(n, ast.Name) and n.id in ("now", "due", "when")) or (  # noqa: E731
            isinstance(n, ast.Attribute) and (n.attr.endswith(("_at", "_from")) or n.attr == "at"))
        for rule_file in RULE_FILES:
            for node in ast.walk(ast.parse((ROOT / rule_file).read_text(encoding="utf-8"))):
                if isinstance(node, ast.Compare) and not all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
                    with self.subTest(file=rule_file, line=node.lineno):
                        self.assertFalse(any(moment(n) for n in [node.left, *node.comparators]))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, ("now", "utcnow", "today", "time", "monotonic"), node.lineno)

    def test_the_slug_is_the_slug_of_a_problem(self):
        schema = json.loads((ROOT / "protocol/schemas/problem.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["slug"]["pattern"], "^" + rules.SLUG_RE.pattern + "$")
        self.assertEqual(links.SLUG_PATTERN, rules.SLUG_RE.pattern)


# --------------------------------------------------------------------------- the grammar of links
class LinkGrammarTests(unittest.TestCase):
    def refs(self, text):
        return [(r.slug, r.label) for r in links.scan(text)[0]]

    def bad(self, text):
        return [m.text for m in links.scan(text)[1]]

    LINKS = [
        ("См. [[памятка:cycles]].", [("cycles", None)]),
        ("[[памятка:cycles|циклы for]]", [("cycles", "циклы for")]),
        ("[[Памятка:cycles]] и [[ПАМЯТКА:io-fast]]", [("cycles", None), ("io-fast", None)]),
        ("[[памятка:a1|  с пробелами по краям  ]]", [("a1", "с пробелами по краям")]),
        ("[[[памятка:cycles]]]", [("cycles", None)]),  # the outer brackets are text
        ("`[[памятка:cycles]]` in code counts too", [("cycles", None)]),
        ("[[памятка:cycles]][[памятка:cycles|ещё раз]]", [("cycles", None), ("cycles", "ещё раз")]),
        ("[[memo:cycles]] [[pamyatka:cycles]] [[cycles]]", []),  # not the prefix: plain text, no warning
        ("no links at all", []),
        ("", []),
    ]

    def test_links(self):
        for text, want in self.LINKS:
            with self.subTest(text):
                self.assertEqual(self.refs(text), want)
                self.assertEqual(self.bad(text), [])

    NOT_LINKS = [
        "[[памятка:Cycles]]",  # upper case slug
        "[[памятка:]]",
        "[[памятка:a]]",  # one character
        "[[памятка:-cycles]]",
        "[[памятка:cy cles]]",
        "[[ памятка:cycles]]",  # spaces inside the brackets
        "[[памятка :cycles]]",
        "[[памятка: cycles]]",
        "[[памятка:cycles|]]",  # an empty text
        "[[памятка:cycles|   ]]",
        "[[памятка:cycles|a|b]]",
        "[[памятка:cycles|a[b]]",
        "[[памятка:cycles|" + "я" * 201 + "]]",
        "[[памятка:cycles|zero\u200bwidth]]",
        "[[памятка:cycles|bell\x07]]",
        "[[памятка:" + "a" * 64 + "]]",
    ]

    def test_what_looks_like_a_link_but_is_not(self):
        for text in self.NOT_LINKS:
            with self.subTest(text):
                self.assertEqual(self.refs(text), [])
                self.assertEqual(self.bad(text), [text])

    def test_a_line_break_ends_a_candidate(self):
        self.assertEqual(links.scan("[[памятка:cycles\n]]"), ([], []))

    def test_the_longest_text_and_slug(self):
        self.assertEqual(self.refs("[[памятка:cycles|" + "я" * 200 + "]]"), [("cycles", "я" * 200)])
        self.assertEqual(self.refs("[[памятка:" + "a" * 63 + "]]"), [("a" * 63, None)])

    def test_places(self):
        text = "ab [[памятка:cycles]] cd"
        ref = links.scan(text)[0][0]
        self.assertEqual(text[ref.start:ref.end], "[[памятка:cycles]]")

    def test_slugs_in_order_without_repeats(self):
        self.assertEqual(links.slugs(["[[памятка:b-1]] [[памятка:a-1]]", "[[памятка:b-1]] [[памятка:c-1]]"]),
                         ["b-1", "a-1", "c-1"])

    def test_warnings_never_refuse(self):
        texts = {"statement/ru.md": "[[памятка:cycles]] [[памятка:old-io]] [[памятка:nope]] [[памятка:Bad]]",
                 "body": "nothing"}
        out = links.warnings(texts, {"cycles": False, "old-io": True})
        self.assertEqual(len(out), 3)
        self.assertTrue(out[0].startswith("statement/ru.md: [[памятка:Bad]] is not a link"))
        self.assertIn("[[памятка:old-io]]: the material old-io is archived", out[1])
        self.assertIn("there is no material nope", out[2])
        self.assertEqual(links.warnings({"body": "[[памятка:cycles]] [[памятка:cycles]]"}, {"cycles": False}), [])
        self.assertEqual(len(links.warnings({"body": "[[памятка:nope]] [[памятка:nope]]"}, {})), 1)


class InStepTests(unittest.TestCase):
    """The copies that must not drift: the grammar in the migration of 4v.4 (it fills material_refs for the texts
    before it) and the limits of the CHECKs in models.py."""
    MIGRATION = ROOT / "backend/migrations/versions/2026_11_12_0213-b8e0cbcc4cda_materials_library_module_4v_4.py"

    def migration(self):
        import importlib.util
        try:
            import alembic  # noqa: F401
        except ImportError:  # pragma: no cover - alembic is not installed
            self.skipTest("needs alembic")
        spec = importlib.util.spec_from_file_location("materials_migration", self.MIGRATION)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_grammar_of_the_migration_is_links_py(self):
        m = self.migration()
        self.assertEqual((m.PREFIX, m.LABEL_MAX, m.SLUG_PATTERN, m.BAD_LABEL_CATEGORIES),
                         (links.PREFIX, links.LABEL_MAX, links.SLUG_PATTERN, links.BAD_LABEL_CATEGORIES))
        self.assertEqual((m.LINK_RE.pattern, m.LINK_RE.flags), (links.LINK_RE.pattern, links.LINK_RE.flags))
        self.assertEqual((m.CANDIDATE_RE.pattern, m.CANDIDATE_RE.flags),
                         (links.CANDIDATE_RE.pattern, links.CANDIDATE_RE.flags))
        texts = [text for text, _ in LinkGrammarTests.LINKS] + LinkGrammarTests.NOT_LINKS + [
            "[[памятка:cycles\n]]", "[[памятка:b-1]] x [[ПАМЯТКА:a-1|т]] [[памятка:b-1]]",
            "[[памятка:cycles|" + "я" * 200 + "]]", "[[памятка:cycles|" + "я" * 201 + "]]"]
        for text in texts:
            with self.subTest(text):
                self.assertEqual(m.link_slugs(text), links.slugs([text]))

    def test_the_checks_of_the_tables_are_the_rules(self):
        try:
            from app import models  # the rules must not import it; the test may
        except ImportError:  # pragma: no cover - sqlalchemy is not installed
            self.skipTest("needs sqlalchemy")
        self.assertEqual(models.MATERIAL_SLUG_PATTERN, "^" + rules.SLUG_RE.pattern + "$")
        self.assertEqual(models.MATERIAL_TITLE_MAX_CHARS, rules.TITLE_MAX)
        self.assertEqual(models.MATERIAL_TITLE_MAX_CHARS, rules.SECTION_TITLE_MAX)
        self.assertEqual(models.MATERIAL_BODY_MAX_CHARS, rules.BODY_MAX_CHARS)
        self.assertEqual(tuple(v.value for v in models.MaterialLibrary), rules.LIBRARY)


# --------------------------------------------------------------------------- what a teacher types
class InputTests(unittest.TestCase):
    def test_slug(self):
        for slug in ("ab", "cycles", "io-fast", "a1", "1a", "a" * 63):
            self.assertTrue(rules.check_slug(slug), slug)
        for slug in ("a", "Cycles", "-ab", "a_b", "a b", " ab", "ab ", "а-кириллица", "a" * 64, None, 12, ""):
            with self.subTest(slug):
                self.assertEqual(rules.check_slug(slug).reason, "slug_invalid")

    def test_title(self):
        self.assertEqual(rules.clean_title("  Циклы \n  for  "), "Циклы for")
        self.assertEqual(rules.clean_title(None), "")
        self.assertTrue(rules.check_title("Циклы"))
        self.assertTrue(rules.check_title("я" * 200))
        for title in ("", "я" * 201, "a\x1bb", "a\u202eb", "a\u200bb", None):
            with self.subTest(repr(title)):
                self.assertEqual(rules.check_title(title).reason, "title_invalid")
        self.assertEqual(rules.check_section_title("").reason, "section_title_invalid")

    def test_body(self):
        self.assertTrue(rules.check_body(""))
        self.assertTrue(rules.check_body("x" * rules.BODY_MAX_CHARS))
        self.assertTrue(rules.check_body("<b>html</b>\x1b and anything visible or not"))
        self.assertEqual(rules.check_body("x" * (rules.BODY_MAX_CHARS + 1)).reason, "body_too_long")
        self.assertEqual(rules.check_body("a\x00b").reason, "body_invalid")
        self.assertEqual(rules.check_body("a\ud800b").reason, "body_invalid")
        self.assertEqual(rules.check_body(None).reason, "body_invalid")

    def test_library(self):
        for value in ("none", "all_students", "groups"):
            self.assertTrue(rules.check_library(value))
        self.assertEqual(rules.check_library("everyone").reason, "library_invalid")

    def test_sections_are_two_levels(self):
        top, sub = NS(id=1, parent_id=None), NS(id=2, parent_id=1)
        self.assertTrue(rules.check_section_place(None, None, has_children=True))  # anything may go to the top
        self.assertTrue(rules.check_section_place(None, top, has_children=False))  # a new subsection
        self.assertTrue(rules.check_section_place(3, top, has_children=False))
        self.assertEqual(rules.check_section_place(None, sub, has_children=False).reason, "section_too_deep")
        self.assertEqual(rules.check_section_place(3, top, has_children=True).reason, "section_too_deep")
        self.assertEqual(rules.check_section_place(1, top, has_children=False).reason, "section_invalid")

    def test_order(self):
        self.assertTrue(rules.check_order([1, 2, 3], [3, 1, 2]))
        self.assertTrue(rules.check_order([], []))
        for wanted in ([1, 2], [1, 2, 3, 4], [1, 2, 2, 3], [1, 1, 2]):
            with self.subTest(wanted):
                self.assertEqual(rules.check_order([1, 2, 3], wanted).reason, "order_invalid")
        self.assertEqual(rules.order_problems([1, 2, 3], [1, 1, 4]), {"missing": [2, 3], "extra": [4],
                                                                       "repeated": [1]})


# --------------------------------------------------------------------------- who opens a material
class OpenTests(unittest.TestCase):
    def test_admins_open_everything(self):
        self.assertEqual(basis(ADMIN, material(archived_at=ARCHIVED)), "admin")
        self.assertTrue(rules.in_library(ADMIN, material(archived_at=ARCHIVED), (), ()))

    def test_no_basis(self):
        self.assertIsNone(basis())
        self.assertFalse(rules.can_open(PUPIL, material(), DURING))

    def test_library(self):
        cases = [  # (library, groups of the library, the pupil's live groups, archived, opens)
            ("all_students", [], [], False, True),
            ("all_students", [], [], True, False),
            ("groups", [GROUP], [GROUP], False, True),
            ("groups", [GROUP], [GROUP], True, False),
            ("groups", [GROUP], [OTHER_GROUP], False, False),
            ("groups", [GROUP], [], False, False),  # removed from the group, or the group archived (not live)
            ("none", [GROUP], [GROUP], False, False),  # groups left over from before mean nothing
            ("groups", [], [GROUP], False, False),
        ]
        for library, lib_groups, live, archived, want in cases:
            m = material(library=library, archived_at=ARCHIVED if archived else None)
            with self.subTest(library=library, lib_groups=lib_groups, live=live, archived=archived):
                self.assertEqual(rules.in_library(PUPIL, m, lib_groups, live), want)
                self.assertEqual(basis(m=m, library_group_ids=lib_groups, live_group_ids=live),
                                 "library" if want else None)

    def test_feed_and_item_links(self):
        hidden = dict(after_due_statement="hidden")
        cases = [  # (name, the basis item, when, the basis expected)
            ("material item in the feed", ItemBasis(item(), True), DURING, "feed"),
            ("material item at available_from", ItemBasis(item(), True), AVAILABLE, "feed"),
            ("material item before available_from", ItemBasis(item(), True), AVAILABLE - US, None),
            ("material item not addressed", ItemBasis(item(), False), DURING, None),
            ("material item draft", ItemBasis(item(state="draft"), True), DURING, None),
            ("material item archived", ItemBasis(item(state="archived"), True), DURING, None),
            ("not a material item, without a link", ItemBasis(item("assignment"), True), DURING, None),
            ("assignment linking it", ItemBasis(item("assignment"), True, links=True), DURING, "item_link"),
            ("assignment linking it, after the deadline", ItemBasis(item("assignment"), True, links=True), AFTER_DUE,
             "item_link"),
            ("assignment linking it, statement hidden after the deadline",
             ItemBasis(item("assignment", **hidden), True, links=True), AFTER_DUE, "item_link"),
            ("assignment linking it, not yet available", ItemBasis(item("assignment"), True, links=True),
             AVAILABLE - US, None),
            ("assignment linking it, draft", ItemBasis(item("assignment", state="draft"), True, links=True), DURING,
             None),
            ("assignment linking it, archived", ItemBasis(item("assignment", state="archived"), True, links=True),
             DURING, None),
            ("assignment linking it, not addressed", ItemBasis(item("assignment"), False, links=True), DURING, None),
            ("contest item linking it in a note", ItemBasis(item("contest"), True, links=True, contest=contest(),
                                                            in_contest_group=True), DURING, "item_link"),
            ("contest item linking it, contest not visible",
             ItemBasis(item("contest"), True, links=True, contest=contest(), in_contest_group=False), DURING, None),
        ]
        for name, b, when, want in cases:
            for archived in (False, True):  # an archived material stays open by every basis but the library
                with self.subTest(name, archived=archived):
                    m = material(archived_at=ARCHIVED if archived else None)
                    self.assertEqual(basis(m=m, when=when, items=[b]), want)

    def test_problem_links(self):
        participation = NS(started_at=None, joined_via="group")
        cases = [  # (name, contest, contest problem, participation, in a group of the contest, when, opens)
            ("group member, no participation yet (owner's case)", contest(), contest_problem(), None, True, DURING,
             True),
            ("group member, joined", contest(), contest_problem(), participation, True, DURING, True),
            ("before the contest window: the problem is already listed", contest(), contest_problem(), None, True,
             AVAILABLE, True),
            ("before the problem's available_from", contest(), contest_problem(), None, True, AVAILABLE - US, False),
            ("after the due moment, statement hidden", contest(), contest_problem(after_due_statement="hidden"),
             None, True, AFTER_DUE, True),
            ("after the contest", contest(), contest_problem(), None, True, AFTER_DUE + timedelta(days=1), True),
            ("hidden contest problem", contest(), contest_problem(hidden=True), None, True, DURING, False),
            ("not in a group of the contest", contest(), contest_problem(), None, False, DURING, False),
            ("removed from the group, still a participant", contest(), contest_problem(), participation, False,
             DURING, False),
            ("draft contest", contest(state="draft"), contest_problem(), None, True, DURING, False),
            ("archived contest, participant", contest(state="archived"), contest_problem(), participation, True,
             DURING, True),
            ("archived contest, no participation", contest(state="archived"), contest_problem(), None, True, DURING,
             False),
            ("open contest, anyone", contest(access_mode="open"), contest_problem(), None, False, DURING, True),
            ("code contest without the code", contest(access_mode="code"), contest_problem(), None, False, DURING,
             False),
            ("code contest, joined with the code", contest(access_mode="code"), contest_problem(),
             NS(started_at=None, joined_via="code"), False, DURING, True),
            ("code_or_groups through the group", contest(access_mode="code_or_groups"), contest_problem(), None,
             True, DURING, True),
        ]
        for name, c, cp, part, in_group, when, want in cases:
            for archived in (False, True):
                with self.subTest(name, archived=archived):
                    m = material(archived_at=ARCHIVED if archived else None)
                    got = basis(m=m, when=when, problems=[ProblemBasis(c, cp, part, in_group)])
                    self.assertEqual(got, "problem_link" if want else None)
                    self.assertEqual(contest_access.problem_visible(PUPIL, c, cp, part, when, in_group), want)

    def test_the_order_of_bases(self):
        m = material(library="all_students")
        feed = ItemBasis(item(), True)
        link = ItemBasis(item("assignment"), True, links=True)
        problem = ProblemBasis(contest(), contest_problem(), None, True)
        self.assertEqual(basis(m=m, items=[link, feed], problems=[problem]), "library")
        self.assertEqual(basis(items=[link, feed], problems=[problem]), "feed")
        self.assertEqual(basis(items=[link], problems=[problem]), "item_link")
        self.assertEqual(basis(problems=[problem]), "problem_link")

    def test_matrix(self):
        """Every combination: library (none / all / group / removed from the group) x a material item (none / listed /
        not yet / archived) x a linking assignment (none / listed / hidden after due / draft) x a linking problem (none
        / visible / hidden) x archived material. Opens exactly when one basis holds."""
        libraries = {"none": ("none", [], []), "all": ("all_students", [], []),
                     "group": ("groups", [GROUP], [GROUP]), "removed": ("groups", [GROUP], [])}
        feed_items = {"none": None, "listed": item(), "not yet": item(available_from=AFTER_DUE),
                      "archived": item(state="archived")}
        link_items = {"none": None, "listed": item("assignment"),
                      "hidden after due": item("assignment", after_due_statement="hidden", due_at=AVAILABLE + US),
                      "draft": item("assignment", state="draft")}
        problems = {"none": None, "visible": contest_problem(), "hidden": contest_problem(hidden=True)}
        for (lib, feed, link, prob, archived) in itertools.product(libraries, feed_items, link_items, problems,
                                                                  (False, True)):
            library, lib_groups, live = libraries[lib]
            m = material(library=library, archived_at=ARCHIVED if archived else None)
            items = []
            if feed_items[feed] is not None:
                items.append(ItemBasis(feed_items[feed], True))
            if link_items[link] is not None:
                items.append(ItemBasis(link_items[link], True, links=True))
            probs = [ProblemBasis(contest(), problems[prob], None, True)] if problems[prob] is not None else []
            expected = ((lib in ("all", "group") and not archived) or feed == "listed"
                        or link in ("listed", "hidden after due") or prob == "visible")
            with self.subTest(library=lib, feed=feed, link=link, problem=prob, archived=archived):
                self.assertEqual(rules.can_open(PUPIL, m, DURING, library_group_ids=lib_groups, live_group_ids=live,
                                                items=items, problems=probs), expected)


# --------------------------------------------------------------------------- links for rendering
class ResolveTests(unittest.TestCase):
    def test_a_student_cannot_tell_missing_from_closed(self):
        open_m, closed_m, archived_m = material(id=1, slug="aa"), material(id=2, slug="bb"), \
            material(id=3, slug="cc", archived_at=ARCHIVED)
        found = {"aa": (open_m, True), "bb": (closed_m, False), "cc": (archived_m, False)}
        out = rules.resolve(PUPIL, ["aa", "bb", "cc", "dd", "aa"], found)
        self.assertEqual(out, [{"slug": "aa", "available": True, "id": 1, "title": "Циклы"},
                               {"slug": "bb", "available": False}, {"slug": "cc", "available": False},
                               {"slug": "dd", "available": False}])

    def test_an_admin_sees_the_status(self):
        found = {"aa": (material(id=1, slug="aa"), True), "cc": (material(id=3, slug="cc", archived_at=ARCHIVED),
                                                                 True)}
        self.assertEqual(rules.resolve(ADMIN, ["aa", "cc", "dd"], found),
                         [{"slug": "aa", "status": "ok", "id": 1, "title": "Циклы"},
                          {"slug": "cc", "status": "archived", "id": 3, "title": "Циклы"},
                          {"slug": "dd", "status": "unknown"}])


if __name__ == "__main__":
    unittest.main()
