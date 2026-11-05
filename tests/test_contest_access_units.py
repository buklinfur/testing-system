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


JOINED = NS(started_at=None, joined_via="open")  # has not pressed "start" (personal) / any participant (fixed)


def started(when, via="open"):
    return NS(started_at=when, joined_via=via)


def problem(**kw):
    base = dict(hidden=False, available_from=None, due_at=None, after_due_statement="visible", after_due_submit="late")
    base.update(kw)
    return NS(**base)


CP = problem()
HIDDEN_CP = problem(hidden=True)
US = timedelta(microseconds=1)

FIXED, PERSONAL = contest(), personal()
PERSONAL_NO_END = personal(ends_at=None)  # a timed practice
PRACTICE = contest(ends_at=None)  # fixed timing without an end: the practice archive


class CanSubmitTests(unittest.TestCase):
    # (name, contest, problem, participation, now, kwargs, expected: (allowed, reason, counted))
    CASES = [
        # fixed timing: before / during / after x participant or not x upsolving
        ("fixed before", FIXED, CP, JOINED, BEFORE, {}, (False, "not_started_yet", False)),
        ("fixed during", FIXED, CP, JOINED, DURING, {}, (True, "ok", True)),
        # the window is [starts_at, ends_at): the start belongs to it, the end does not
        ("fixed a microsecond before the start", FIXED, CP, JOINED, S - US, {}, (False, "not_started_yet", False)),
        ("fixed at the start", FIXED, CP, JOINED, S, {}, (True, "ok", True)),
        ("fixed a microsecond before the end", FIXED, CP, JOINED, E - US, {}, (True, "ok", True)),
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

    def test_enter(self):
        coded = contest(access_mode="code", invite_code="AC34KM")
        groups = contest(access_mode="groups")
        either = contest(access_mode="code_or_groups", invite_code="AC34KM")
        by_group = NS(started_at=None, joined_via="group")
        cases = [  # (name, contest, participation, now, code, in_group, (allowed, reason, joined_via))
            ("open, before", FIXED, None, BEFORE, None, False, (True, "ok", "open")),
            ("open, during", FIXED, None, DURING, None, False, (True, "ok", "open")),
            ("open, after, upsolving", FIXED, None, AFTER, None, False, (True, "ok", "open")),
            ("open, after, no upsolving", contest(allow_upsolving=False), None, AFTER, None, False,
             (False, "contest_over", None)),
            ("already joined", FIXED, JOINED, DURING, None, False, (False, "already_joined", None)),
            ("draft", contest(state="draft"), None, DURING, None, False, (False, "not_published", None)),
            ("archived", contest(state="archived"), None, DURING, None, False, (False, "not_published", None)),
            ("code, right", coded, None, DURING, "AC34KM", False, (True, "ok", "code")),
            ("code, other case and spaces", coded, None, DURING, " ac3-4km ", False, (True, "ok", "code")),
            ("code, wrong", coded, None, DURING, "AC34KN", False, (False, "wrong_code", None)),
            ("code, missing", coded, None, DURING, None, False, (False, "wrong_code", None)),
            ("code mode ignores groups", coded, None, DURING, None, True, (False, "wrong_code", None)),
            ("groups, member", groups, None, DURING, None, True, (True, "ok", "group")),
            ("groups, not a member", groups, None, DURING, None, False, (False, "no_access", None)),
            ("groups, a code does not help", groups, None, DURING, "AC34KM", False, (False, "no_access", None)),
            ("either, member without a code", either, None, DURING, None, True, (True, "ok", "group")),
            ("either, member with the code: still group", either, None, DURING, "AC34KM", True, (True, "ok", "group")),
            ("either, the code", either, None, DURING, "AC34KM", False, (True, "ok", "code")),
            ("either, neither", either, None, DURING, None, False, (False, "wrong_code", None)),
            # В6: taken out of the group, the student comes back with the code; the participation becomes a code one
            ("either, removed from the group, the code", either, by_group, DURING, "AC34KM", False,
             (True, "rejoin_with_code", "code")),
            ("either, removed, wrong code", either, by_group, DURING, "AC34KN", False, (False, "already_joined", None)),
            ("either, still in the group, the code: nothing to change", either, by_group, DURING, "AC34KM", True,
             (False, "already_joined", None)),
            ("code, came in while open, the code", coded, JOINED, DURING, "AC34KM", False,
             (True, "rejoin_with_code", "code")),
            ("code, already by the code", coded, started(None, "code"), DURING, "AC34KM", False,
             (False, "already_joined", None)),
            ("groups, by group, a code", groups, by_group, DURING, "AC34KM", False, (False, "already_joined", None)),
        ]
        for name, c, part, now, code, in_group, (allowed, reason, via) in cases:
            with self.subTest(name):
                d = access.can_enter(STUDENT, c, part, now, code, in_group=in_group)
                self.assertEqual((d.allowed, d.reason, d.joined_via), (allowed, reason, via))
        self.assertEqual(access.can_enter(ADMIN, FIXED, None, DURING).reason, "admins_do_not_join")

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
            ("no access any more", personal(access_mode="groups"), started(None, "group"), DURING,
             (False, "no_access")),
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


# ------------------------------------------------------------------ access modes and groups (D-020, D-021)
P_CODE, P_GROUP, P_OPEN = started(None, "code"), started(None, "group"), started(None, "open")
SITUATIONS = [  # (name, participation, in_group); an archived group or one taken off the contest is in_group False
    ("member, not entered yet", None, True),
    ("member, entered through the group", P_GROUP, True),
    ("removed from the group (or it is archived / detached)", P_GROUP, False),
    ("not a member, never entered", None, False),
    ("entered with the code", P_CODE, False),
    ("entered with the code and a member", P_CODE, True),
    ("entered while the contest was open", P_OPEN, False),
]
# has access (statements, submitting) per mode, in the order of SITUATIONS
ACCESS = {
    "open": [True, True, True, True, True, True, True],
    "code": [False, False, False, False, True, True, False],
    "groups": [True, True, False, False, False, True, False],
    "code_or_groups": [True, True, False, False, True, True, False],
}


class AccessModeTests(unittest.TestCase):
    def test_matrix(self):
        for mode, expected in ACCESS.items():
            c = contest(access_mode=mode, invite_code="AC34KM")
            for (name, part, in_group), want in zip(SITUATIONS, expected):
                with self.subTest(mode=mode, who=name):
                    self.assertEqual(access.has_access(STUDENT, c, part, in_group), want)
                    if part is not None:
                        d = access.can_submit(STUDENT, c, CP, part, DURING, in_group=in_group)
                        self.assertEqual((d.allowed, d.reason), (True, "ok") if want else (False, "no_access"))
                        self.assertEqual(access.can_view_problem(STUDENT, c, CP, part, DURING, in_group), want)
                        self.assertEqual(access.can_see_leaderboard(STUDENT, c, part, DURING, in_group), want)
                        # whatever the access, a student sees their own submissions and scores
                        self.assertTrue(access.can_see_own_results(STUDENT, c, part))
                    self.assertTrue(access.has_access(ADMIN, c, part, in_group))

    def test_a_contest_only_for_groups_is_not_shown_to_others(self):
        for mode in ACCESS:
            c = contest(access_mode=mode, invite_code="AC34KM")
            for name, part, in_group in SITUATIONS:
                with self.subTest(mode=mode, who=name):
                    want = mode != "groups" or access.has_access(STUDENT, c, part, in_group)
                    self.assertEqual(access.can_view_contest(STUDENT, c, part, DURING, in_group), want)

    def test_the_contest_list_of_a_student_removed_from_the_group(self):
        """For 4b: the list is can_view_contest OR can_see_own_results; a contest that is there only by the second
        opens as "my results only" (no statements, no submitting, no leaderboard)."""
        c = contest(access_mode="groups")
        self.assertFalse(access.can_view_contest(STUDENT, c, P_GROUP, DURING, in_group=False))
        self.assertTrue(access.can_see_own_results(STUDENT, c, P_GROUP))
        self.assertFalse(access.can_view_problem(STUDENT, c, CP, P_GROUP, DURING, in_group=False))
        self.assertFalse(access.can_see_leaderboard(STUDENT, c, P_GROUP, DURING, in_group=False))

    def test_archived_contest_needs_access_too_but_own_results_stay(self):
        c = contest(state="archived", access_mode="groups")
        self.assertTrue(access.can_view_contest(STUDENT, c, P_GROUP, AFTER, in_group=True))
        self.assertFalse(access.can_view_contest(STUDENT, c, P_GROUP, AFTER, in_group=False))
        self.assertTrue(access.can_see_own_results(STUDENT, c, P_GROUP))
        self.assertFalse(access.can_see_own_results(STUDENT, contest(state="draft"), P_GROUP))
        self.assertFalse(access.can_see_own_results(STUDENT, c, None))

    def test_changing_the_mode_cuts_off_the_right_students(self):
        cases = [  # (from -> to, participation, in_group, access before, access after)
            ("open -> code cuts those who came in open", P_OPEN, False, ("open", True), ("code", False)),
            ("open -> code keeps those with the code", P_CODE, False, ("open", True), ("code", True)),
            ("code -> groups cuts the code", P_CODE, False, ("code", True), ("groups", False)),
            ("code -> groups keeps members", P_CODE, True, ("code", True), ("groups", True)),
            ("groups -> code cuts members without the code", P_GROUP, True, ("groups", True), ("code", False)),
            ("groups -> code_or_groups keeps members", P_GROUP, True, ("groups", True), ("code_or_groups", True)),
            ("code_or_groups -> groups cuts the code", P_CODE, False, ("code_or_groups", True), ("groups", False)),
            ("anything -> open lets everyone in", P_GROUP, False, ("groups", False), ("open", True)),
        ]
        for name, part, in_group, (before, had), (after, has) in cases:
            with self.subTest(name):
                self.assertEqual(access.has_access(STUDENT, contest(access_mode=before), part, in_group), had)
                self.assertEqual(access.has_access(STUDENT, contest(access_mode=after), part, in_group), has)


# ------------------------------------------------------------------ deadlines of contest problems (D-023)
DUE = at(11)  # inside the window 09:00-12:00
AVAILABLE = at(9, 30)


class DeadlineTests(unittest.TestCase):
    def submit(self, cp, now, c=FIXED, part=JOINED, **kw):
        d = access.can_submit(STUDENT, c, cp, part, now, **kw)
        return d.allowed, d.reason, d.counted, d.late

    def test_due_boundary_for_each_policy(self):
        """On time while now <= the due moment: exactly at due_at is still on time (unlike the end of the window)."""
        ok, late = (True, "ok", True, False), (True, "late", True, True)
        expected = {  # policy: (due - 1 us, due, due + 1 us)
            "no": (ok, ok, (False, "deadline_passed", False, False)),
            "late": (ok, ok, late),
            "yes": (ok, ok, ok),
        }
        for policy, wants in expected.items():
            cp = problem(due_at=DUE, after_due_submit=policy)
            for now, want in zip((DUE - US, DUE, DUE + US), wants):
                with self.subTest(policy=policy, now=now.time()):
                    self.assertEqual(self.submit(cp, now), want)
        self.assertEqual(self.submit(CP, AFTER - timedelta(hours=1, seconds=1)), ok)  # no due_at: never late

    def test_available_from_boundary(self):
        cp = problem(available_from=AVAILABLE)
        self.assertEqual(self.submit(cp, AVAILABLE - US), (False, "not_available_yet", False, False))
        self.assertEqual(self.submit(cp, AVAILABLE), (True, "ok", True, False))  # inclusive
        self.assertFalse(access.problem_listed(STUDENT, cp, AVAILABLE - US))  # not even its label
        self.assertTrue(access.problem_listed(STUDENT, cp, AVAILABLE))
        self.assertFalse(access.can_view_problem(STUDENT, FIXED, cp, JOINED, AVAILABLE - US))
        self.assertTrue(access.can_view_problem(STUDENT, FIXED, cp, JOINED, AVAILABLE))
        self.assertTrue(access.problem_listed(ADMIN, cp, AVAILABLE - US))
        self.assertFalse(access.problem_listed(STUDENT, problem(hidden=True), DURING))

    def test_extensions(self):
        cp = problem(due_at=DUE, after_due_submit="no")
        mine, my_group = at(11, 30), at(11, 45)
        cases = [  # (name, extensions, effective due)
            ("none", [], DUE),
            ("own", [mine], mine),
            ("group", [my_group], my_group),
            ("the latest of several", [mine, my_group], my_group),
            ("one earlier than due_at changes nothing", [at(10)], DUE),
        ]
        for name, extensions, due in cases:
            with self.subTest(name):
                self.assertEqual(access.effective_due(cp, extensions), due)
                self.assertTrue(self.submit(cp, due, extensions=extensions)[0])
                self.assertEqual(self.submit(cp, due + US, extensions=extensions)[1], "deadline_passed")
        self.assertIsNone(access.effective_due(problem(), [mine]))  # an extension of no deadline extends nothing

    def test_personal_timer_and_due_whichever_comes_first(self):
        part = started(at(10, 30))  # own hour 10:30-11:30
        cp = problem(due_at=DUE)  # 11:00, policy late
        self.assertEqual(self.submit(cp, at(10, 45), PERSONAL, part), (True, "ok", True, False))
        self.assertEqual(self.submit(cp, at(11, 10), PERSONAL, part), (True, "late", True, True))  # due first
        cp = problem(due_at=at(12), after_due_submit="no")
        self.assertEqual(self.submit(cp, at(11, 40), PERSONAL, part), (True, "upsolving", False, False))  # timer first

    def test_end_of_contest_before_due(self):
        cp = problem(due_at=at(13), after_due_submit="yes")
        self.assertEqual(self.submit(cp, at(12, 30), contest(allow_upsolving=False)), (False, "contest_over", False, False))
        self.assertEqual(self.submit(cp, at(12, 30)), (True, "upsolving", False, False))

    def test_upsolving_is_not_touched_by_deadlines(self):
        """В3: after the window the deadline does not matter, whatever after_due_submit says."""
        for policy in ("no", "late", "yes"):
            with self.subTest(policy):
                cp = problem(due_at=DUE, after_due_submit=policy)
                self.assertEqual(self.submit(cp, AFTER), (True, "upsolving", False, False))

    def test_late_counts_and_uses_the_limit(self):
        cp = problem(due_at=DUE, after_due_submit="late")
        c = contest(max_submissions_per_problem=2)
        self.assertEqual(self.submit(cp, DUE + US, c, counted_submissions=1), (True, "late", True, True))
        self.assertEqual(self.submit(cp, DUE + US, c, counted_submissions=2)[1], "submission_limit")

    def test_statement_after_due(self):
        hidden = problem(due_at=DUE, after_due_statement="hidden")
        self.assertTrue(access.can_view_problem(STUDENT, FIXED, hidden, JOINED, DUE))
        self.assertFalse(access.can_view_problem(STUDENT, FIXED, hidden, JOINED, DUE + US))
        self.assertTrue(access.can_view_problem(STUDENT, FIXED, hidden, JOINED, DUE + US, extensions=[at(11, 30)]))
        self.assertFalse(access.can_view_problem(STUDENT, FIXED, hidden, JOINED, AFTER))  # in upsolving too
        self.assertTrue(access.can_view_problem(STUDENT, FIXED, problem(due_at=DUE), JOINED, AFTER))  # visible
        self.assertTrue(access.can_view_problem(ADMIN, FIXED, hidden, JOINED, AFTER))
        self.assertTrue(access.can_see_own_results(STUDENT, FIXED, JOINED))  # own results stay

    def test_admins_are_never_late(self):
        d = access.can_submit(ADMIN, FIXED, problem(due_at=DUE, after_due_submit="no"), None, AFTER)
        self.assertEqual((d.allowed, d.counted, d.late), (True, False, False))
