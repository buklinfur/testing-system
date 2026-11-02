"""Access rules of contests (R25), case by case: each row of a table is one situation (docs/CONTESTS.md)."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.contests import access  # noqa: E402


def at(hh, mm=0, day=10, year=2026):
    return datetime(year, 11, day, hh, mm, tzinfo=timezone.utc)


S, E = at(9), at(12)  # the window of the contests below: 09:00-12:00
BEFORE, DURING, AFTER = at(8), at(10), at(13)
ADMIN, STUDENT = NS(role="admin"), NS(role="student")


def contest(**kw):
    base = dict(state="published", timing_mode="fixed", starts_at=S, ends_at=E, personal_duration=None,
                allow_upsolving=True, access_mode="open", invite_code=None, languages=None, submit_cooldown_sec=0,
                max_submissions_per_problem=None, leaderboard="visible", freeze_at=None, tests_after_end=False)
    base.update(kw)
    return NS(**base)


def personal(**kw):
    return contest(timing_mode="personal", personal_duration=timedelta(hours=1), **kw)


JOINED = NS(started_at=None)  # a participant who has not pressed "start" (personal) / any participant (fixed)


def started(when):
    return NS(started_at=when)


CP = NS(hidden=False)
HIDDEN_CP = NS(hidden=True)

FIXED, PERSONAL = contest(), personal()
PERSONAL_NO_END = personal(ends_at=None)  # a timed practice
PRACTICE = contest(ends_at=None)  # fixed timing without an end: the practice archive


class CanSubmitTests(unittest.TestCase):
    # (name, contest, problem, participation, now, kwargs, expected: (allowed, reason, counted))
    CASES = [
        # fixed timing: before / during / after x participant or not x upsolving
        ("fixed before", FIXED, CP, JOINED, BEFORE, {}, (False, "not_started_yet", False)),
        ("fixed during", FIXED, CP, JOINED, DURING, {}, (True, "ok", True)),
        ("fixed at the start", FIXED, CP, JOINED, S, {}, (True, "ok", True)),
        ("fixed at the end", FIXED, CP, JOINED, E, {}, (True, "upsolving", False)),
        ("fixed after, upsolving", FIXED, CP, JOINED, AFTER, {}, (True, "upsolving", False)),
        ("fixed after, no upsolving", contest(allow_upsolving=False), CP, JOINED, AFTER, {}, (False, "contest_over", False)),
        ("fixed during, not a participant", FIXED, CP, None, DURING, {}, (False, "not_participant", False)),
        ("fixed after, not a participant", FIXED, CP, None, AFTER, {}, (False, "not_participant", False)),
        ("fixed before, not a participant", FIXED, CP, None, BEFORE, {}, (False, "not_participant", False)),
        # personal timing (the window ends at 12:00, the duration is 1 h)
        ("personal before, joined", PERSONAL, CP, JOINED, BEFORE, {}, (False, "not_started_yet", False)),
        ("personal during, not started", PERSONAL, CP, JOINED, DURING, {}, (False, "press_start", False)),
        ("personal during own hour", PERSONAL, CP, started(at(9, 30)), DURING, {}, (True, "ok", True)),
        ("personal after own hour, upsolving", PERSONAL, CP, started(at(9, 30)), at(11), {}, (True, "upsolving", False)),
        ("personal after own hour, no upsolving", personal(allow_upsolving=False), CP, started(at(9, 30)), at(11), {},
         (False, "contest_over", False)),
        ("personal own hour cut by ends_at", PERSONAL, CP, started(at(11, 30)), at(11, 59), {}, (True, "ok", True)),
        ("personal own hour cut by ends_at, after", PERSONAL, CP, started(at(11, 30)), E, {}, (True, "upsolving", False)),
        ("personal after the end, never started", PERSONAL, CP, JOINED, AFTER, {}, (True, "upsolving", False)),
        ("personal after the end, never started, no upsolving", personal(allow_upsolving=False), CP, JOINED, AFTER, {},
         (False, "contest_over", False)),
        ("personal after the end, started", PERSONAL, CP, started(at(9, 30)), AFTER, {}, (True, "upsolving", False)),
        ("personal during, not a participant", PERSONAL, CP, None, DURING, {}, (False, "not_participant", False)),
        # personal timing without ends_at: a timed practice
        ("timed practice, not started, years later", PERSONAL_NO_END, CP, JOINED, at(10, year=2030), {},
         (False, "press_start", False)),
        ("timed practice, own hour", PERSONAL_NO_END, CP, started(at(10, year=2030)), at(10, 30, year=2030), {},
         (True, "ok", True)),
        ("timed practice, after own hour", PERSONAL_NO_END, CP, started(at(10, year=2030)), at(11, 30, year=2030), {},
         (True, "upsolving", False)),
        ("timed practice, before starts_at", PERSONAL_NO_END, CP, JOINED, BEFORE, {}, (False, "not_started_yet", False)),
        # fixed timing without ends_at: the practice archive
        ("practice, years later", PRACTICE, CP, JOINED, at(10, year=2030), {}, (True, "ok", True)),
        ("practice, before starts_at", PRACTICE, CP, JOINED, BEFORE, {}, (False, "not_started_yet", False)),
        # hidden problems, states
        ("hidden problem", FIXED, HIDDEN_CP, JOINED, DURING, {}, (False, "problem_hidden", False)),
        ("hidden problem after the end", FIXED, HIDDEN_CP, JOINED, AFTER, {}, (False, "problem_hidden", False)),
        ("draft contest", contest(state="draft"), CP, JOINED, DURING, {}, (False, "not_published", False)),
        ("archived contest", contest(state="archived"), CP, JOINED, DURING, {}, (False, "not_published", False)),
        # cooldown and the per-problem limit
        ("cooldown not over", contest(submit_cooldown_sec=15), CP, JOINED, DURING,
         {"last_submission_at": DURING - timedelta(seconds=5)}, (False, "cooldown", False)),
        ("cooldown over", contest(submit_cooldown_sec=15), CP, JOINED, DURING,
         {"last_submission_at": DURING - timedelta(seconds=20)}, (True, "ok", True)),
        ("cooldown in upsolving too", contest(submit_cooldown_sec=15), CP, JOINED, AFTER,
         {"last_submission_at": AFTER - timedelta(seconds=1)}, (False, "cooldown", False)),
        ("limit reached", contest(max_submissions_per_problem=3), CP, JOINED, DURING, {"counted_submissions": 3},
         (False, "submission_limit", False)),
        ("limit not reached", contest(max_submissions_per_problem=3), CP, JOINED, DURING, {"counted_submissions": 2},
         (True, "ok", True)),
        ("limit does not apply to upsolving", contest(max_submissions_per_problem=3), CP, JOINED, AFTER,
         {"counted_submissions": 3}, (True, "upsolving", False)),
        # languages
        ("language not allowed", contest(languages=["python"]), CP, JOINED, DURING, {"language": "cpp"},
         (False, "language_not_allowed", False)),
        ("language allowed", contest(languages=["python"]), CP, JOINED, DURING, {"language": "python"}, (True, "ok", True)),
        ("every language when none listed", FIXED, CP, JOINED, DURING, {"language": "cpp"}, (True, "ok", True)),
    ]

    def test_students(self):
        for name, c, cp, part, now, kw, (allowed, reason, counted) in self.CASES:
            with self.subTest(name):
                d = access.can_submit(STUDENT, c, cp, part, now, **kw)
                self.assertEqual((d.allowed, d.reason, d.counted), (allowed, reason, counted))

    def test_cooldown_tells_how_long_to_wait(self):
        d = access.can_submit(STUDENT, contest(submit_cooldown_sec=15), CP, JOINED, DURING,
                              last_submission_at=DURING - timedelta(seconds=4, milliseconds=500))
        self.assertEqual((d.reason, d.retry_after_s), ("cooldown", 11))

    def test_admins_may_always_submit_and_never_count(self):
        for name, c, cp, part, now, kw, _ in self.CASES:
            with self.subTest(name):
                d = access.can_submit(ADMIN, c, cp, part, now, **kw)
                self.assertEqual((d.allowed, d.reason, d.counted), (True, "admin", False))


class WindowTests(unittest.TestCase):
    def test_windows(self):
        cases = [
            ("fixed", FIXED, JOINED, access.Window(S, E)),
            ("practice", PRACTICE, JOINED, access.Window(S, None)),
            ("personal, not started", PERSONAL, JOINED, None),
            ("personal, no participation", PERSONAL, None, None),
            ("personal", PERSONAL, started(at(9, 30)), access.Window(at(9, 30), at(10, 30))),
            ("personal, cut by ends_at", PERSONAL, started(at(11, 30)), access.Window(at(11, 30), E)),
            ("timed practice", PERSONAL_NO_END, started(at(10, year=2030)),
             access.Window(at(10, year=2030), at(11, year=2030))),
        ]
        for name, c, part, want in cases:
            with self.subTest(name):
                self.assertEqual(access.contest_window_for(c, part), want)


class ViewJoinStartTests(unittest.TestCase):
    def test_view_contest(self):
        cases = [  # (contest, participation, student sees it)
            (contest(state="draft"), JOINED, False),
            (contest(state="published"), None, True),
            (contest(state="archived"), JOINED, True),  # read-only for its participants
            (contest(state="archived"), None, False),
        ]
        for c, part, want in cases:
            with self.subTest(state=c.state, participant=part is not None):
                self.assertEqual(access.can_view_contest(STUDENT, c, part, DURING), want)
                self.assertTrue(access.can_view_contest(ADMIN, c, part, DURING))

    def test_view_problem(self):
        cases = [  # (name, contest, problem, participation, now, student sees the statement)
            ("fixed before", FIXED, CP, JOINED, BEFORE, False),
            ("fixed during", FIXED, CP, JOINED, DURING, True),
            ("fixed after", FIXED, CP, JOINED, AFTER, True),
            ("not a participant", FIXED, CP, None, DURING, False),
            ("hidden problem", FIXED, HIDDEN_CP, JOINED, DURING, False),
            ("draft", contest(state="draft"), CP, JOINED, DURING, False),
            ("archived, participant", contest(state="archived"), CP, JOINED, AFTER, True),
            ("personal, not started", PERSONAL, CP, JOINED, DURING, False),
            ("personal, started", PERSONAL, CP, started(at(9, 30)), DURING, True),
            ("personal, never started, after the end, upsolving", PERSONAL, CP, JOINED, AFTER, True),
            ("personal, never started, after the end, no upsolving", personal(allow_upsolving=False), CP, JOINED, AFTER,
             False),
            ("timed practice, not started", PERSONAL_NO_END, CP, JOINED, at(10, year=2030), False),
        ]
        for name, c, cp, part, now, want in cases:
            with self.subTest(name):
                self.assertEqual(access.can_view_problem(STUDENT, c, cp, part, now), want)
                self.assertTrue(access.can_view_problem(ADMIN, c, cp, part, now))

    def test_join(self):
        coded = contest(access_mode="code", invite_code="AC34KM")
        cases = [  # (name, contest, participation, now, code, (allowed, reason))
            ("open, before", FIXED, None, BEFORE, None, (True, "ok")),
            ("open, during", FIXED, None, DURING, None, (True, "ok")),
            ("open, after, upsolving", FIXED, None, AFTER, None, (True, "ok")),
            ("open, after, no upsolving", contest(allow_upsolving=False), None, AFTER, None, (False, "contest_over")),
            ("already joined", FIXED, JOINED, DURING, None, (False, "already_joined")),
            ("draft", contest(state="draft"), None, DURING, None, (False, "not_published")),
            ("archived", contest(state="archived"), None, DURING, None, (False, "not_published")),
            ("code, right", coded, None, DURING, "AC34KM", (True, "ok")),
            ("code, other case and spaces", coded, None, DURING, " ac3-4km ", (True, "ok")),
            ("code, wrong", coded, None, DURING, "AC34KN", (False, "wrong_code")),
            ("code, missing", coded, None, DURING, None, (False, "wrong_code")),
        ]
        for name, c, part, now, code, (allowed, reason) in cases:
            with self.subTest(name):
                d = access.can_join(STUDENT, c, part, now, code)
                self.assertEqual((d.allowed, d.reason), (allowed, reason))
        self.assertEqual(access.can_join(ADMIN, FIXED, None, DURING).reason, "admins_do_not_join")

    def test_start(self):
        cases = [  # (name, contest, participation, now, (allowed, reason))
            ("fixed timing has no start", FIXED, JOINED, DURING, (False, "not_personal")),
            ("not a participant", PERSONAL, None, DURING, (False, "not_participant")),
            ("before the window", PERSONAL, JOINED, BEFORE, (False, "not_started_yet")),
            ("inside the window", PERSONAL, JOINED, DURING, (True, "ok")),
            ("a minute before the end", PERSONAL, JOINED, at(11, 59), (True, "ok")),
            ("at the end", PERSONAL, JOINED, E, (False, "contest_over")),
            ("twice", PERSONAL, started(at(9, 30)), DURING, (False, "already_started")),
            ("draft", personal(state="draft"), JOINED, DURING, (False, "not_published")),
            ("timed practice, any day", PERSONAL_NO_END, JOINED, at(10, year=2030), (True, "ok")),
        ]
        for name, c, part, now, (allowed, reason) in cases:
            with self.subTest(name):
                d = access.can_start(STUDENT, c, part, now)
                self.assertEqual((d.allowed, d.reason), (allowed, reason))


class LeaderboardAndTestsTests(unittest.TestCase):
    def test_who_sees_the_leaderboard(self):
        cases = [  # (name, contest, participation, now, student sees it)
            ("visible", contest(leaderboard="visible"), JOINED, DURING, True),
            ("visible, not a participant", contest(leaderboard="visible"), None, DURING, False),
            ("hidden", contest(leaderboard="hidden"), JOINED, AFTER, False),
            ("after_end, during", contest(leaderboard="after_end"), JOINED, DURING, False),
            ("after_end, after", contest(leaderboard="after_end"), JOINED, AFTER, True),
            ("after_end, no end", contest(leaderboard="after_end", ends_at=None), JOINED, AFTER, False),
            ("draft", contest(state="draft"), JOINED, DURING, False),
        ]
        for name, c, part, now, want in cases:
            with self.subTest(name):
                self.assertEqual(access.can_see_leaderboard(STUDENT, c, part, now), want)
                self.assertTrue(access.can_see_leaderboard(ADMIN, c, part, now))

    def test_freeze(self):
        frozen = contest(freeze_at=at(11))
        cases = [  # (now, the moment a student's leaderboard is frozen at)
            (DURING, None), (at(11), at(11)), (at(11, 59), at(11)), (E, None), (AFTER, None),
        ]
        for now, want in cases:
            with self.subTest(now=now):
                self.assertEqual(access.leaderboard_cutoff(STUDENT, frozen, now), want)
                self.assertIsNone(access.leaderboard_cutoff(ADMIN, frozen, now))  # admins see it live
        self.assertIsNone(access.leaderboard_cutoff(STUDENT, FIXED, at(11, 30)))  # no freeze configured

    def test_hidden_tests_never_reach_students(self):
        groups = [("samples", "open"), ("main", "hidden")]
        cases = [  # (name, contest, now, groups a student may see)
            ("during", FIXED, DURING, {"samples"}),
            ("after, tests stay hidden", FIXED, AFTER, {"samples"}),
            ("after, tests_after_end", contest(tests_after_end=True), AFTER, {"samples", "main"}),
            ("during, tests_after_end", contest(tests_after_end=True), DURING, {"samples"}),
            ("no end, tests_after_end", contest(tests_after_end=True, ends_at=None), AFTER, {"samples"}),
        ]
        for name, c, now, want in cases:
            with self.subTest(name):
                self.assertEqual(access.visible_tests(STUDENT, c, now, groups), want)
                self.assertEqual(access.visible_tests(ADMIN, c, now, groups), {"samples", "main"})


if __name__ == "__main__":
    unittest.main()
