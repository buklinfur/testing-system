"""Pure rules of the student API (4b.1, D-091): intake of a submission, rates, the student's view of a submission
and the leaderboard. No database: every case is a row of a table."""
import io
import json
import sys
import unittest
import warnings
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]

from app import rates  # noqa: E402
from app.contests import standings as st  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.submissions import intake  # noqa: E402
from app.submissions.intake import IntakeError  # noqa: E402
from app.submissions.student_view import effective_feedback, effective_policy, meet, student_view  # noqa: E402

T0 = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)


def zipped(files: dict, links=(), encrypted=False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            if name in links:
                info.external_attr = (0o120777 << 16)
            zf.writestr(info, data)
    data = bytearray(buf.getvalue())
    if encrypted:  # zipfile clears the flag when writing: set bit 0 in the local and the central headers
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            at = data.find(signature)
            while at >= 0:
                data[at + offset] |= 0x1
                at = data.find(signature, at + 4)
    return bytes(data)


class IntakeTests(unittest.TestCase):
    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(IntakeError) as caught:
            fn(*args, **kw)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_the_server_never_takes_more_than_the_runner(self):
        from runner.config import RunnerConfig
        c = RunnerConfig()
        self.assertEqual((intake.RUNNER_SOURCE_MAX_BYTES, intake.RUNNER_ARCHIVE_MAX_BYTES),
                         (c.max_source_bytes, c.max_archive_bytes))
        s = Settings.from_env({})
        self.assertEqual((s.submit.source_max_bytes, s.submit.archive_max_bytes, s.submit.rate_per_min,
                          s.submit.enter_rate), (256 * 1024, 1024 * 1024, 6, 10))
        for bad in ({"JUDGE_SOURCE_MAX_KB": "257"}, {"JUDGE_ARCHIVE_MAX_KB": "1025"}, {"JUDGE_SOURCE_MAX_KB": "0"},
                    {"JUDGE_SUBMIT_RATE_PER_MIN": "-1"}, {"JUDGE_ENTER_RATE": "0"}):
            with self.subTest(env=bad), self.assertRaises(ValueError):
                Settings.from_env(bad)
        self.assertEqual(Settings.from_env({"JUDGE_SUBMIT_RATE_PER_MIN": "0"}).submit.rate_per_min, 0)

    def test_languages(self):
        enabled = ["python", "c", "cpp"]
        self.assertEqual(intake.allowed_languages(None, None, enabled), enabled)
        self.assertEqual(intake.allowed_languages(["cpp", "python"], None, enabled), ["python", "cpp"])
        self.assertEqual(intake.allowed_languages(["cpp", "python"], ["python", "c"], enabled), ["python"])
        self.assertEqual(intake.allowed_languages(None, ["pascal"], enabled), [])
        self.assertEqual(intake.check_language("python", ["python"]), "python")
        for bad in ("cpp", None, 1, "PYTHON"):
            with self.subTest(language=bad):
                self.refused("language_not_allowed", intake.check_language, bad, ["python"])

    def test_a_single_file_is_strict_utf8_without_a_bom_and_with_its_line_ends(self):
        self.assertEqual(intake.check_source(b"print(1)\r\n", 100).data, b"print(1)\r\n")
        self.assertEqual(intake.check_source("﻿print('я')\n".encode(), 100).data, "print('я')\n".encode())
        got = intake.check_source(b"x = 1\n", 100)
        self.assertEqual((got.is_archive, got.entry), (False, None))
        e = self.refused("source_encoding", intake.check_source, "print('я')".encode("cp1251"), 100)
        self.assertIn("UTF-8", str(e))
        self.refused("source_encoding", intake.check_source, b"\xed\xa0\x80", 100)  # an encoded surrogate
        for empty in (b"", b"  \n\t", intake.BOM, intake.BOM + b"\r\n"):
            with self.subTest(empty=empty):
                self.refused("source_empty", intake.check_source, empty, 100)
        self.assertEqual(self.refused("source_too_large", intake.check_source, b"x" * 101, 100).status, 413)
        self.assertEqual(intake.check_source(b"x" * 100, 100).data, b"x" * 100)
        self.refused("source_too_large", intake.check_source, intake.BOM + b"x" * 98, 100)  # the BOM counts

    def test_archives(self):
        ok = zipped({"main.py": b"import lib\n", "lib.py": b"x = 1\n"})
        got = intake.check_archive(ok, "python", None, None, 1024 * 1024)
        self.assertEqual((got.data, got.is_archive, got.entry), (ok, True, None))
        folder = zipped({"sol/main.py": b"1", "sol/lib.py": b"2"})  # a single top folder is stripped
        self.assertTrue(intake.check_archive(folder, "python", None, None, 1 << 20).is_archive)
        self.assertEqual(intake.check_archive(zipped({"run.py": b"1"}), "python", "run.py", None, 1 << 20).entry,
                         "run.py")
        self.assertTrue(intake.check_archive(zipped({"a.cpp": b"1", "b.h": b"2"}), "cpp", None, None, 1 << 20))
        self.assertTrue(intake.check_archive(zipped({"go.py": b"1"}), "python", None, {"entry": "go.py"}, 1 << 20))
        cases = [  # (code, data, language, entry, problem's submission)
            ("archive_not_allowed", ok, "python", None, {"allow_archive": False}),
            ("archive_invalid", b"not a zip", "python", None, None),
            ("archive_invalid", zipped({}), "python", None, None),
            ("archive_invalid", zipped({"../main.py": b"1"}), "python", None, None),
            ("archive_invalid", zipped({"/etc/main.py": b"1"}), "python", None, None),
            ("archive_invalid", zipped({"main.py": b"1", "link": b"/etc/passwd"}, links=("link",)), "python", None,
             None),
            ("archive_invalid", zipped({"main.py": b"1"}, encrypted=True), "python", None, None),
            ("archive_invalid", zipped({"main.py": b"1", "x\\y": b"2"}), "python", None, None),  # a backslash
            ("archive_too_many_files", zipped({f"f{i}.py": b"1" for i in range(3)} | {"main.py": b"1"}), "python",
             None, {"max_files": 3}),
            ("entry_not_found", zipped({"lib.py": b"1"}), "python", None, None),
            ("entry_not_found", zipped({"main.py": b"1"}), "python", "run.py", None),
            ("entry_not_found", zipped({"a.c": b"1"}), "c", "main.c", None),
            ("entry_invalid", ok, "python", "../main.py", None),
            ("entry_invalid", ok, "python", "", None),
            ("entry_invalid", ok, "python", "x" * 256, None),
        ]
        for code, data, language, entry, sub in cases:
            with self.subTest(code=code, entry=entry):
                self.refused(code, intake.check_archive, data, language, entry, sub, 1 << 20)
        self.refused("source_too_large", intake.check_archive, ok, "python", None, None, len(ok) - 1)
        self.assertEqual(intake.archive_limit({"max_archive_kb": 2}, 1 << 20), 2048)
        self.assertEqual(intake.archive_limit({"max_archive_kb": 4096}, 1 << 20), 1 << 20)
        self.refused("source_too_large", intake.check_archive, zipped({"main.py": b"x" * 3000}), "python", None,
                     {"max_archive_kb": 1}, 1 << 20)
        twice = io.BytesIO()
        with warnings.catch_warnings(), zipfile.ZipFile(twice, "w") as zf:
            warnings.simplefilter("ignore")  # a duplicate name is the point
            zf.writestr("main.py", "1")
            zf.writestr("top/main.py", "2")
            zf.writestr("main.py", "3")
        self.refused("archive_invalid", intake.check_archive, twice.getvalue(), "python", None, None, 1 << 20)

    def test_answers(self):
        choice = {"questions": [{"id": "q1", "options": [{"id": "a"}, {"id": "b"}], "correct": ["a"]},
                                {"id": "q2", "options": [{"id": "x"}, {"id": "y"}], "correct": ["y"]}]}
        text = {"answers": [{"id": "n", "accept": ["4"]}]}
        self.assertEqual(intake.check_answers("choice", choice, {"q1": ["a", "b"]}), {"q1": ["a", "b"]})
        self.assertEqual(intake.check_answers("choice", choice, {}), {})
        self.assertEqual(intake.check_answers("text_answer", text, {"n": " 4 "}), {"n": " 4 "})
        for ptype, config, answers in [
                ("choice", choice, ["a"]), ("choice", choice, {"q3": ["a"]}), ("choice", choice, {"q1": "a"}),
                ("choice", choice, {"q1": ["a", "a"]}), ("choice", choice, {"q1": ["x"]}),
                ("choice", choice, {"q1": [1]}), ("text_answer", text, {"n": 4}),
                ("text_answer", text, {"n": "x" * 1001}), ("text_answer", text, {"n": "a\x00b"}),
                ("text_answer", text, {"n": "\ud800"}), ("text_answer", text, {"m": "4"})]:
            with self.subTest(answers=answers):
                self.refused("answers_invalid", intake.check_answers, ptype, config, answers)
        self.refused("source_expected", intake.check_answers, "batch", {}, {})
        self.refused("answers_expected", intake.check_code_problem, "choice")
        self.assertIsNone(intake.check_code_problem("interactive"))
        self.assertEqual(set(intake.CODES.values()), {413, 422})


class RateTests(unittest.TestCase):
    def test_sliding_window(self):
        w = rates.SUBMIT_WINDOW
        now = T0 + timedelta(minutes=5)
        self.assertTrue(rates.window_ok([], now, 2, w))
        self.assertTrue(rates.window_ok([now - timedelta(seconds=10)], now, 2, w))
        two = [now - timedelta(seconds=50), now - timedelta(seconds=10)]
        got = rates.window_ok(two, now, 2, w)
        self.assertEqual((got.allowed, got.reason, got.retry_after_s), (False, "rate_limited", 10))
        self.assertTrue(rates.window_ok([now - timedelta(seconds=60), now - timedelta(seconds=10)], now, 2, w),
                        "an action exactly one window ago is out of it")
        self.assertTrue(rates.window_ok(two, now, 0, w) and rates.window_ok(two, now, None, w))
        many = [now - timedelta(seconds=s) for s in (59.5, 30, 20, 1)]
        self.assertEqual(rates.window_ok(many, now, 2, w).retry_after_s, 40)  # the 2nd newest must leave
        self.assertEqual(rates.window_ok(many, now, 1, w).retry_after_s, 59)
        self.assertEqual(rates.window_ok([now], now, 1, w).retry_after_s, 60)
        enter = [now - timedelta(minutes=m) for m in range(10)]
        self.assertFalse(rates.window_ok(enter, now, 10, rates.ENTER_WINDOW))
        self.assertTrue(rates.window_ok(enter[:9], now, 10, rates.ENTER_WINDOW))


def judgement(**kw):
    base = dict(status="finished", verdict="WA", score=D(30), max_score=D(60), compile_log=None, tests_done=None,
                tests_total=None, error="Traceback SECRET-ERROR",
                summary={"first_fail": "WA", "groups": [{"name": "samples", "points": "0", "score": "0"},
                                                         {"name": "small", "points": "30", "score": "30"},
                                                         {"name": "big", "points": "30", "score": "0"}]})
    base.update(kw)
    return NS(**base)


def result(group, case, verdict, **kw):
    base = dict(group=group, test_case_id=case, verdict=verdict, time_ms=12, memory_kb=900, exit_code=0, signal=None,
                tl_kind=None, checker_message=f"CHECKER-{case}", output_excerpt=f"OUTPUT-{case}",
                stderr_excerpt=f"STDERR-{case}", visible_to_student=True)
    base.update(kw)
    return NS(**base)


RESULTS = [result("samples", 1, "OK"), result("small", 2, "OK"), result("small", 3, "OK"),
           result("big", 4, "WA"), result("big", 5, "RE", exit_code=None, signal=11), result("big", 6, "SKIPPED")]
CONTENTS = {i: {"input": f"INPUT-{i}", "answer": f"ANSWER-{i}"} for i in range(1, 7)}


JUDGED = [("samples", "open", None), ("small", "hidden", None), ("big", "hidden", None)]


def groups(feedback, big=None, open_now=("samples",), same_version=True, pinned=None):
    """The policy of a judgement of the version pinned now (unless same_version is False)."""
    judged = [("samples", "open", None), ("small", "hidden", None), ("big", "hidden", big)]
    return effective_policy(feedback, judged, pinned if pinned is not None else [feedback], same_version, open_now)


class StudentViewTests(unittest.TestCase):
    def view(self, feedback="full", open_groups=("samples",), **kw):
        kw.setdefault("policy", groups(feedback, open_now=open_groups))
        kw.setdefault("results", RESULTS)
        kw.setdefault("contents", CONTENTS)
        return student_view(kw.pop("j", judgement()), policy=kw.pop("policy", groups(feedback)),
                            problem_feedback=feedback, **kw)

    def text(self, view) -> str:
        return json.dumps(view, default=str, ensure_ascii=False)

    def test_secrets_never_reach_a_student(self):
        for feedback in ("full", "first_fail", "groups_only", "verdict_only"):
            for j in (judgement(), judgement(verdict="IE", status="internal_error"), judgement(verdict="CE")):
                with self.subTest(feedback=feedback, verdict=j.verdict):
                    shown = self.text(self.view(feedback, j=j))
                    for secret in ("SECRET-ERROR", "STDERR-", "INPUT-2", "INPUT-4", "ANSWER-4", "OUTPUT-4",
                                   "CHECKER-4", "CHECKER-2", "visible_to_student", "Traceback"):
                        self.assertNotIn(secret, shown)

    def test_full_then_verdict_only_hides_the_tests_of_an_old_submission(self):
        """The policy is the one of now, not test_results.visible_to_student written when it was judged."""
        before = self.view("full")
        self.assertEqual([t["test_no"] for t in before["tests"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(before["tests"][3], {"test_no": 4, "group": "big", "verdict": "WA", "time_ms": 12,
                                              "memory_kb": 900})
        after = self.view("verdict_only")
        self.assertEqual([t["test_no"] for t in after["tests"]], [1])  # the open sample only
        self.assertNotIn("time_ms", after["tests"][0])
        self.assertEqual(after["groups"], [{"name": "samples", "points": D("0.00"), "score": D("0.00")}])

    def test_open_groups_show_their_content(self):
        sample = self.view("first_fail")["tests"][0]
        self.assertEqual(sample, {"test_no": 1, "group": "samples", "verdict": "OK", "time_ms": 12, "memory_kb": 900,
                                  "input": "INPUT-1", "answer": "ANSWER-1", "output": "OUTPUT-1",
                                  "checker_message": "CHECKER-1"})
        after_end = self.view("first_fail", open_groups=("samples", "small", "big"))  # tests_after_end
        self.assertEqual([t.get("input") for t in after_end["tests"]], [f"INPUT-{i}" for i in range(1, 7)])
        self.assertEqual(len(after_end["groups"]), 3)
        missing = self.view("first_fail", contents={})  # contents the caller did not read: nothing invented
        self.assertIsNone(missing["tests"][0]["input"])

    def test_feedback_policies(self):
        first = self.view("first_fail")
        self.assertEqual([(t["test_no"], t["verdict"]) for t in first["tests"]], [(1, "OK"), (4, "WA")])
        self.assertEqual([g["name"] for g in first["groups"]], ["samples"])
        groups_only = self.view("groups_only")
        self.assertEqual([t["test_no"] for t in groups_only["tests"]], [1])
        self.assertEqual([g["name"] for g in groups_only["groups"]], ["samples", "small", "big"])
        mixed = self.view("first_fail", policy=groups("first_fail", big="full", pinned=["full"]))
        self.assertEqual([t["test_no"] for t in mixed["tests"]], [1, 4, 5, 6])
        self.assertEqual(mixed["tests"][2]["runtime_hint"][:17], "Ошибка сегментаци")
        unknown = self.view("full", policy={})  # groups the pinned version no longer has: hidden, problem's feedback
        self.assertEqual([t["test_no"] for t in unknown["tests"]], [1, 2, 3, 4, 5, 6])
        self.assertNotIn("input", unknown["tests"][0])

    def test_points_status_and_flags(self):
        v = self.view("full", points=D(100), late=True, counted=True, rechecking=True)
        self.assertEqual((v["score"], v["max_score"], v["late"], v["rechecking"]), (D("50.00"), D("100.00"), True, True))
        self.assertEqual(v["groups"][1], {"name": "small", "points": D("50.00"), "score": D("50.00")})
        self.assertEqual((v["verdict"], v["verdict_text"]), ("WA", "Неверный ответ"))
        queued = self.view(j=judgement(status="queued", verdict=None, max_score=None))
        self.assertEqual(queued, {"status": "queued", "counted": True, "late": False, "rechecking": False,
                                  "score": None, "max_score": None})
        running = self.view(j=judgement(status="running", verdict=None, tests_done=3, tests_total=6))
        self.assertEqual((running["status"], running["progress"]), ("judging", {"done": 3, "total": 6}))
        self.assertNotIn("tests", running)

    def test_compile_error_internal_error_and_quiz(self):
        ce = self.view(j=judgement(verdict="CE", compile_log="main.cpp:1: error\n" * 1000))
        self.assertEqual(set(ce) - {"status", "counted", "late", "rechecking", "score", "max_score"},
                         {"verdict", "verdict_text", "hint", "compile_log"})
        self.assertLess(len(ce["compile_log"]), 4100)
        ie = self.view(j=judgement(verdict="IE", status="internal_error", score=D(0)))
        self.assertEqual((ie["verdict"], ie["score"]), ("IE", D("0.00")))
        self.assertNotIn("tests", ie)
        quiz = judgement(verdict="PARTIAL", summary={"quiz": {"details": [
            {"id": "q1", "correct": True, "explanation": "потому что"}, {"id": "q2", "correct": False}]}})
        self.assertEqual(self.view("full", j=quiz, results=[])["questions"],
                         [{"id": "q1", "correct": True, "explanation": "потому что"}, {"id": "q2", "correct": False}])
        self.assertEqual(self.view("first_fail", j=quiz, results=[])["questions"],
                         [{"id": "q1", "correct": True}, {"id": "q2", "correct": False}])
        self.assertEqual(self.view("verdict_only", j=quiz, results=[])["questions"], [])

    def test_time_limit_kind(self):
        tl = [result("samples", 1, "TL", tl_kind="wall")]
        self.assertEqual(self.view("full", results=tl)["tests"][0]["tl_kind"], "wall")
        self.assertNotIn("tl_kind", self.view("verdict_only", results=tl)["tests"][0])


def att(user, problem, minute, verdict="OK", score=None, max_score=D(60), counted=True, status="finished"):
    return st.Attempt(user, problem, T0 + timedelta(minutes=minute), counted, status, verdict,
                      D(60) if score is None and verdict == "OK" else D(score or 0), max_score)


PEOPLE = [st.Participant(1, "Аня", T0), st.Participant(2, "Борис", T0), st.Participant(3, "Вика", T0)]
PROBLEMS = [st.Problem(10, "A", points=D(100)), st.Problem(11, "B", max_score=D(60)),
            st.Problem(12, "C", hidden=True, max_score=D(60))]


class VersionPolicyTests(unittest.TestCase):
    """Owner's amendment of stage 5: openness only from the judgement's own version while it is pinned; the level is
    the stricter of the two versions; tests of an old judgement are never matched with groups of another version."""

    def shown(self, policy, feedback="full"):
        v = student_view(judgement(), results=RESULTS, policy=policy, problem_feedback=feedback, contents=CONTENTS)
        return v, json.dumps(v, default=str, ensure_ascii=False)

    def test_meet(self):
        table = {("full", "full"): "full", ("full", "first_fail"): "first_fail", ("groups_only", "full"): "groups_only",
                 ("first_fail", "groups_only"): "verdict_only", ("groups_only", "first_fail"): "verdict_only",
                 ("verdict_only", "full"): "verdict_only", ("first_fail", "first_fail"): "first_fail"}
        for (a, b), want in table.items():
            with self.subTest(a=a, b=b):
                self.assertEqual(meet(a, b), want)
        self.assertEqual(effective_feedback("full", ["full", "groups_only"]), "groups_only")
        self.assertEqual(effective_feedback(None, [None]), "first_fail")

    def test_a_new_version_that_closed_an_open_group_hides_it_in_old_submissions(self):
        v, text = self.shown(groups("full", same_version=False, pinned=["full"]))
        self.assertNotIn("INPUT-1", text)
        self.assertNotIn("CHECKER-1", text)
        self.assertEqual([g["name"] for g in v["groups"]], ["samples", "small", "big"])  # full: scores, no content

    def test_a_new_version_that_opened_a_closed_group_does_not_open_it_in_old_submissions(self):
        # the judgement's version had "big" hidden; whatever the new version says about a group of that name
        policy = effective_policy("full", JUDGED, ["full"], False, open_now=("samples", "small", "big"))
        _v, text = self.shown(policy)
        for secret in ("INPUT-", "ANSWER-", "OUTPUT-", "CHECKER-"):
            self.assertNotIn(secret, text)

    def test_renumbered_groups_leak_nothing(self):
        # the version pinned now calls its groups otherwise; the old judgement keeps its own names, all hidden
        policy = effective_policy("first_fail", JUDGED, ["first_fail", "full"], False, open_now=("g1", "g2"))
        v, text = self.shown(policy, "first_fail")
        self.assertEqual([t["test_no"] for t in v["tests"]], [4])
        self.assertNotIn("INPUT-", text)

    def test_the_stricter_level_wins(self):
        same = groups("full", pinned=["full", "verdict_only"])  # a group override of the pinned version is strict
        v, _ = self.shown(same)
        self.assertEqual([t["test_no"] for t in v["tests"]], [1])  # the open sample only
        self.assertNotIn("time_ms", v["tests"][0])
        loose = groups("groups_only", pinned=["full"])  # a looser pinned version does not loosen the old one
        v, _ = self.shown(loose, "groups_only")
        self.assertEqual([t["test_no"] for t in v["tests"]], [1])


class StandingsTests(unittest.TestCase):
    def test_points(self):
        attempts = [att(1, 10, 5, "PARTIAL", 30), att(1, 10, 20), att(1, 11, 30, "PARTIAL", 15),
                    att(2, 10, 10), att(2, 11, 25, "PARTIAL", 15),  # same total as Аня, reached earlier
                    att(2, 12, 26),  # a hidden problem: not shown, not counted
                    att(3, 10, 1, counted=False),  # upsolving does not count
                    att(3, 11, 50, "WA", 0), att(3, 11, 55, None, status="queued", max_score=None)]
        got = st.standings("points", PROBLEMS, PEOPLE, attempts, me=3)
        self.assertEqual(got["problems"], [{"label": "A", "max_score": D("100.00")},
                                           {"label": "B", "max_score": D("60.00")}])
        rows = got["rows"]
        self.assertEqual([(r["place"], r["name"], r["score"]) for r in rows],
                         [(1, "Борис", D("115.00")), (2, "Аня", D("115.00")), (3, "Вика", D("0.00"))])
        self.assertEqual(rows[1]["cells"]["A"], {"score": D("100.00"), "attempts": 2, "pending": 0})
        self.assertEqual(rows[2]["cells"], {"B": {"score": D("0.00"), "attempts": 1, "pending": 1}})
        self.assertEqual([r["me"] for r in rows], [False, False, True])
        for row in rows:  # the name the student chose, nothing else of them
            self.assertEqual(set(row), {"place", "name", "me", "score", "cells"})

    def test_equal_results_share_a_place(self):
        attempts = [att(1, 10, 5), att(2, 10, 5)]
        self.assertEqual([r["place"] for r in st.standings("points", PROBLEMS, PEOPLE, attempts)["rows"]], [1, 1, 3])
        self.assertEqual([r["place"] for r in st.standings("icpc", PROBLEMS, PEOPLE, attempts)["rows"]], [1, 1, 3])

    def test_icpc(self):
        late = st.Participant(3, "Вика", T0 + timedelta(minutes=30))  # a personal start
        attempts = [att(1, 10, 3, "WA"), att(1, 10, 4, "CE"), att(1, 10, 5, "IE", status="internal_error"),
                    att(1, 10, 9), att(1, 10, 15, "WA"),  # after the OK: changes nothing
                    att(1, 11, 7, "TL"),
                    att(2, 10, 50), att(2, 11, 51),
                    att(3, 10, 40), att(3, 11, 45, "RE"), att(3, 11, 46, None, status="running")]
        rows = st.standings("icpc", PROBLEMS, PEOPLE[:2] + [late], attempts, penalty_minutes=20)["rows"]
        self.assertEqual([(r["place"], r["name"], r["solved"], r["penalty"]) for r in rows],
                         [(1, "Борис", 2, 101), (2, "Вика", 1, 10), (3, "Аня", 1, 29)])
        self.assertEqual(rows[2]["cells"]["A"], {"solved": True, "attempts": 2, "minutes": 9, "pending": 0})
        self.assertEqual(rows[2]["cells"]["B"], {"solved": False, "attempts": 1, "pending": 0})
        self.assertEqual(rows[1]["cells"]["B"], {"solved": False, "attempts": 1, "pending": 1})

    def test_freeze(self):
        attempts = [att(1, 10, 5, "PARTIAL", 30), att(1, 10, 50), att(2, 10, 55)]
        got = st.standings("points", PROBLEMS, PEOPLE, attempts, cutoff=T0 + timedelta(minutes=45))
        self.assertTrue(got["frozen"])
        rows = {r["name"]: r for r in got["rows"]}
        self.assertEqual(rows["Аня"]["cells"]["A"], {"score": D("50.00"), "attempts": 1, "pending": 1})
        self.assertEqual(rows["Борис"]["cells"]["A"], {"score": D("0.00"), "attempts": 0, "pending": 1})
        self.assertFalse(st.standings("points", PROBLEMS, PEOPLE, attempts)["frozen"])

    def test_only_the_participants_given(self):
        rows = st.standings("points", PROBLEMS, PEOPLE[:1], [att(1, 10, 5), att(2, 10, 5)])["rows"]
        self.assertEqual([r["name"] for r in rows], ["Аня"])


if __name__ == "__main__":
    unittest.main()
