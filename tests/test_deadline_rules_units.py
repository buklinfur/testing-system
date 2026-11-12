"""Deadline rules shared by contest problems and class feed items (app/deadline_rules.py, D-032, D-059): the
boundaries one microsecond before, at and after, the effective due moment with extensions, the after-due policies,
and that the module stays independent of the rest of the backend."""
import ast
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app import deadline_rules as rules  # noqa: E402
from app.contests import access  # noqa: E402

US = timedelta(microseconds=1)
AVAILABLE = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)
DUE = datetime(2026, 11, 17, 23, 59, 59, tzinfo=timezone.utc)


def holder(**kw):
    base = dict(available_from=AVAILABLE, due_at=DUE, after_due_statement="visible", after_due_submit="late")
    base.update(kw)
    return NS(**base)


class AvailableTests(unittest.TestCase):
    def test_available_from_is_inclusive(self):
        self.assertFalse(rules.available(holder(), AVAILABLE - US))
        self.assertTrue(rules.available(holder(), AVAILABLE))
        self.assertTrue(rules.available(holder(), AVAILABLE + US))

    def test_without_available_from_always(self):
        self.assertTrue(rules.available(holder(available_from=None), datetime(2000, 1, 1, tzinfo=timezone.utc)))


class DueTests(unittest.TestCase):
    def test_effective_due_is_the_latest(self):
        mine, group = DUE + timedelta(hours=1), DUE + timedelta(hours=3)
        self.assertEqual(rules.effective_due(holder()), DUE)
        self.assertEqual(rules.effective_due(holder(), [mine]), mine)
        self.assertEqual(rules.effective_due(holder(), [mine, group]), group)
        self.assertEqual(rules.effective_due(holder(), [DUE - timedelta(hours=1)]), DUE)

    def test_no_due_at_no_deadline_whatever_the_extensions(self):
        self.assertIsNone(rules.effective_due(holder(due_at=None), [DUE]))
        self.assertFalse(rules.past_due(holder(due_at=None), DUE + timedelta(days=365), [DUE]))

    def test_due_moment_is_inclusive(self):
        self.assertFalse(rules.past_due(holder(), DUE - US))
        self.assertFalse(rules.past_due(holder(), DUE))
        self.assertTrue(rules.past_due(holder(), DUE + US))

    def test_extension_moves_the_boundary(self):
        later = DUE + timedelta(hours=2)
        self.assertFalse(rules.past_due(holder(), later, [later]))
        self.assertTrue(rules.past_due(holder(), later + US, [later]))


class PolicyTests(unittest.TestCase):
    def test_after_due_submit_at_the_boundary(self):
        for policy, after in (("no", (False, "deadline_passed", False)), ("late", (True, "late", True)),
                              ("yes", (True, "ok", False))):
            h = holder(after_due_submit=policy)
            for when, expected in ((DUE - US, (True, "ok", False)), (DUE, (True, "ok", False)), (DUE + US, after)):
                with self.subTest(policy=policy, when=when):
                    d = rules.after_due_submit(h, when)
                    self.assertEqual((d.allowed, d.reason, d.late), expected)

    def test_after_due_submit_with_an_extension(self):
        later = DUE + timedelta(hours=1)
        d = rules.after_due_submit(holder(after_due_submit="no"), later, [later])
        self.assertEqual((d.allowed, d.late), (True, False))
        self.assertFalse(rules.after_due_submit(holder(after_due_submit="no"), later + US, [later]))

    def test_statement_hidden_only_past_due_with_hidden(self):
        hidden, visible = holder(after_due_statement="hidden"), holder()
        self.assertFalse(rules.statement_hidden_after_due(hidden, DUE))
        self.assertTrue(rules.statement_hidden_after_due(hidden, DUE + US))
        self.assertFalse(rules.statement_hidden_after_due(visible, DUE + US))
        self.assertFalse(rules.statement_hidden_after_due(hidden, DUE + US, [DUE + timedelta(hours=1)]))

    def test_enum_members_work_like_their_values(self):
        policy = NS(value="no")  # an ORM row holds enum members, compared by value
        self.assertFalse(rules.after_due_submit(holder(after_due_submit=policy), DUE + US))


class SharedTests(unittest.TestCase):
    def test_contest_access_reexports_the_same_objects(self):
        self.assertIs(access.Decision, rules.Decision)
        self.assertIs(access.effective_due, rules.effective_due)
        self.assertIs(access.past_due, rules.past_due)

    def test_the_module_imports_nothing_of_the_backend(self):
        """deadline_rules is shared by contests and the class feed: it must import neither of them (nor any other
        part of app), so either can use it without a cycle."""
        path = ROOT / "backend" / "app" / "deadline_rules.py"
        found = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.level > 0 or (node.module or "").startswith("app")):
                found.append(f"from {'.' * node.level}{node.module or ''}")
            elif isinstance(node, ast.Import):
                found += [alias.name for alias in node.names if alias.name.startswith("app")]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
