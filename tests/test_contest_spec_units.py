"""contest.yaml: the schema and the rules it cannot express (no database)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.contests.spec import ContestSpecError, parse, parse_duration  # noqa: E402
from app.timeutil import zone  # noqa: E402

NSK = zone("Asia/Novosibirsk")
EXAMPLES = ROOT / "contests-examples"
BASE = """format_version: 1
slug: c1
title: "Контест"
starts_at: 2026-11-10 09:00
problems:
  - {label: A, problem: sum-of-two}
"""


def spec(extra="", base=BASE, tz=NSK):
    return parse(base + extra, tz)


class SpecTests(unittest.TestCase):
    def refused(self, text, *fragments):
        with self.assertRaises(ContestSpecError) as ctx:
            parse(text, NSK)
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))
        return ctx.exception

    def test_examples(self):
        for path in sorted(EXAMPLES.glob("*.yaml")):
            with self.subTest(path.name):
                s = parse(path.read_text(encoding="utf-8"), NSK)
                self.assertTrue(s.problems)

    def test_defaults(self):
        s = spec()
        self.assertEqual((s.timing_mode, s.access_mode, s.leaderboard, s.allow_upsolving, s.submit_cooldown_sec,
                          s.scoring_mode, s.icpc_penalty_minutes, s.ends_at, s.languages),
                         ("fixed", "code", "hidden", True, 15, "points", 20, None, None))

    def test_access(self):
        self.assertEqual(spec("access: {mode: open}\n").access_mode, "open")
        self.refused(BASE + "access: {mode: everyone}\n", "schema: access/mode")
        self.refused(BASE + "access: {mode: code, code: X}\n", "schema: access")  # the code is never in the file
        # the key of 4a.2 gets a hint, not only "additional property"; other problems are still listed
        error = self.refused(BASE + "join_policy: open\nleaderboard: nope\n",
                             "join_policy is replaced by access: {mode: open}", "schema: leaderboard")
        self.assertFalse(any("join_policy" in p and "schema" in p for p in error.problems))

    def test_times_without_an_offset_are_judge_timezone_and_dates_stay_text(self):
        s = spec("ends_at: '2026-11-10T12:00:00+00:00'\n")
        self.assertEqual(s.starts_at, datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc))  # 09:00 in Novosibirsk
        self.assertEqual(s.ends_at, datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(spec(tz=zone("UTC")).starts_at, datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc))

    def test_durations(self):
        self.assertEqual(parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(parse_duration("1h30m"), timedelta(minutes=90))
        self.assertEqual(parse_duration("2h"), timedelta(hours=2))
        for bad in ("", "h", "1.5h", "90", "30m1h"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_duration(bad)

    def test_personal_timing(self):
        s = spec("timing_mode: personal\npersonal_duration: 1h30m\nends_at: 2026-11-10 18:00\n")
        self.assertEqual(s.personal_duration, timedelta(minutes=90))
        s = spec("timing_mode: personal\npersonal_duration: 45m\n")  # without ends_at: a timed practice
        self.assertIsNone(s.ends_at)
        self.refused(BASE + "timing_mode: personal\n", "personal_duration")  # required
        self.refused(BASE + "personal_duration: 1h\n", "schema")  # only with personal timing
        self.refused(BASE + "timing_mode: personal\npersonal_duration: 0m\n", "personal_duration must be positive")
        self.refused(BASE + "timing_mode: personal\npersonal_duration: 0h0m\n", "personal_duration must be positive")

    def test_freeze(self):
        ok = spec("ends_at: 2026-11-10 12:00\nfreeze_at: 2026-11-10 11:00\nleaderboard: visible\n")
        self.assertIsNotNone(ok.freeze_at)
        self.refused(BASE + "ends_at: 2026-11-10 12:00\nfreeze_at: 2026-11-10 11:00\nleaderboard: hidden\n",
                     "leaderboard: hidden")
        self.refused(BASE + "ends_at: 2026-11-10 12:00\nfreeze_at: 2026-11-10 11:00\n", "leaderboard: hidden")  # default
        self.refused(BASE + "freeze_at: 2026-11-10 11:00\nleaderboard: visible\n", "freeze_at needs ends_at")
        self.refused(BASE + "ends_at: 2026-11-10 12:00\nfreeze_at: 2026-11-10 13:00\nleaderboard: visible\n",
                     "before ends_at")
        self.refused(BASE + "timing_mode: personal\npersonal_duration: 1h\nends_at: 2026-11-10 12:00\n"
                            "freeze_at: 2026-11-10 11:00\nleaderboard: visible\n", "schema")  # not with personal

    def test_window_labels_and_problems(self):
        self.refused(BASE + "ends_at: 2026-11-10 09:00\n", "ends_at must be after starts_at")
        e = self.refused(BASE.replace("problems:\n", "problems:\n  - {label: A, problem: pair-with-sum}\n"
                                                     "  - {label: B, problem: sum-of-two}\n"),
                         "each label may occur once: A", "each problem may occur once: sum-of-two")
        self.assertEqual(len(e.problems), 2)

    def test_problem_entries(self):
        s = spec(base=BASE.replace("{label: A, problem: sum-of-two}",
                                   "{label: A, problem: sum-of-two, version: 2, points: 150}"))
        entry = s.problems[0]
        self.assertEqual((entry.label, entry.slug, entry.version, entry.points), ("A", "sum-of-two", 2, Decimal("150")))
        self.refused(BASE.replace("label: A", "label: a"), "schema")
        self.refused(BASE.replace("problem: sum-of-two}", "problem: sum-of-two, points: 0}"), "schema")
        self.refused(BASE + "state: published\n", "schema")  # the state is not part of the file
        self.refused(BASE + "invite_code: ABCDEF\n", "schema")  # nor is the invite code

    def test_not_yaml(self):
        self.refused("format_version: [1\n", "not valid YAML")
        self.refused("- a\n- b\n", "mapping")


if __name__ == "__main__":
    unittest.main()
