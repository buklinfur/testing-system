"""The services of the student API (4b.1, app/student/) on a real PostgreSQL: what a student sees of contests and
problems, sending a submission on the one path (intake, rate, access rules - nothing stored on a refusal), their own
submissions only, the view of a submission by the version it was judged on, the rate of attempts to enter kept in the
database, and the leaderboard. Skipped unless JUDGE_TEST_DATABASE_URL is set."""
import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace as NS

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from sqlalchemy import func, select

    from app import groups as g
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.models import (AccessMode, Blob, Contest, ContestEntryAttempt, ContestGroup, ContestState, Judgement,
                            JudgementStatus, LeaderboardMode, ProblemType, ProblemVersion, Submission, TestCase, TestGroup, TestResult,
                            TimingMode, Verdict, VersionStatus, Visibility)
    from app.settings import SubmitSettings
    from app.student import contests as sc
    from app.student import problems as sp
    from app.student import standings as ss
    from app.student import submissions as sv
    from app.student.common import StudentError

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@requires_pg
class StudentServicesTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blobs = BlobStore(tmp.name)
        self.settings = SubmitSettings()
        self.contest, self.cp, self.version = self.seed["cp"].contest, self.seed["cp"], self.seed["version"]
        self.contest.submit_cooldown_sec = 0
        self.a, self.b = self.student(1), self.student(2)
        # an open group shown in the statement, with real files, and the hidden group "main" of the seed (no files)
        self.samples = TestGroup(version_id=self.version.id, position=-1, name="samples", points=0,
                                 visibility=Visibility.open, show_in_statement=True)
        self.s.add(self.samples)
        self.s.flush()
        self.sample = TestCase(group_id=self.samples.id, position=0, name="s1", input_sha256=self.blob(b"1 2\n"),
                               answer_sha256=self.blob(b"3\n"))
        self.s.add(self.sample)
        self.s.commit()

    def blob(self, data: bytes) -> str:
        sha = self.blobs.put(data)
        if self.s.get(Blob, sha) is None:
            self.s.add(Blob(sha256=sha, size_bytes=len(data)))
            self.s.flush()
        return sha

    def submit(self, user, now=NOW, label="A", slug="c", **kw):
        kw.setdefault("language", "python")
        if not any(k in kw for k in ("source", "archive", "answers")):
            kw["source"] = b"print(sum(map(int, input().split())))\n"
        out = sv.submit(self.s, user, self.blobs, kw.pop("settings", self.settings), slug, label, now, **kw)
        self.s.commit()
        return out

    def refused(self, code, fn, *args, **kw):
        with self.assertRaises(StudentError) as caught:
            fn(*args, **kw)
        self.s.rollback()
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        return caught.exception

    def enter(self, user, slug="c", code=None, now=NOW):
        out = sc.enter(self.s, user, slug, now, self.settings, code)
        self.s.commit()
        return out

    def finish(self, sub_id, verdict="OK", score="100", max_score="100", results=(), summary=None):
        j = self.s.execute(select(Judgement).where(Judgement.submission_id == sub_id)).scalar_one()
        j.status, j.verdict, j.score, j.max_score = JudgementStatus.finished, Verdict(verdict), Decimal(score), \
            Decimal(max_score)
        j.summary = summary or {"groups": [{"name": "samples", "points": "0", "score": "0"},
                                           {"name": "main", "points": "100", "score": score}]}
        for case, v in results:
            self.s.add(TestResult(judgement_id=j.id, test_case_id=case.id, verdict=Verdict(v), time_ms=5,
                                  memory_kb=100, checker_message=f"CHECKER-{case.name}",
                                  output_excerpt=f"OUTPUT-{case.name}", stderr_excerpt="STDERR-SECRET",
                                  visible_to_student=True))
        self.s.commit()
        return j

    # ---------------------------------------------------------------- contests
    def test_the_list_and_the_page_follow_the_access_rules(self):
        lesson = Contest(slug="lesson", title="L", starts_at=NOW - timedelta(days=1), state=ContestState.published,
                         access_mode=AccessMode.groups, created_by=self.seed["admin"].id)
        self.s.add(lesson)
        self.s.commit()
        self.assertEqual([r["slug"] for r in sc.my_contests(self.s, self.a, NOW)], ["c"])
        self.refused("contest_not_found", sc.contest_view, self.s, self.a, "lesson", NOW)
        self.refused("contest_not_found", sc.contest_view, self.s, self.a, "nope", NOW)
        cli = audit.Actor(cli="cli:test")
        group = g.create(self.s, cli, "7Б", NOW)
        g.add_members(self.s, cli, group, [self.a], NOW)
        self.s.add(ContestGroup(contest_id=lesson.id, group_id=group.id))
        self.s.commit()
        self.assertEqual(self.enter(self.a, "lesson").code, "ok")
        self.assertEqual(sc.contest_view(self.s, self.a, "lesson", NOW)["mode"], "full")
        g.remove_members(self.s, cli, group, [self.a], NOW)  # keeps the history, loses the access
        self.s.commit()
        row = {r["slug"]: r for r in sc.my_contests(self.s, self.a, NOW)}["lesson"]
        self.assertEqual((row["mode"], row["has_access"]), ("own_results", False))
        page = sc.contest_view(self.s, self.a, "lesson", NOW)
        self.assertNotIn("problems", page)
        self.assertNotIn("description_md", page)
        self.refused("student_required", sc.my_contests, self.s, self.seed["admin"], NOW)
        self.refused("student_required", sv.my_submissions, self.s, self.seed["admin"], NOW)

    def test_problems_are_not_named_before_the_students_time_begins(self):
        self.contest.starts_at = NOW + timedelta(hours=1)
        self.s.commit()
        page = sc.contest_view(self.s, self.a, "c", NOW)
        self.assertEqual((page["problems"], page["problem_count"], page["phase"]), ([], 1, "before"))
        self.assertNotIn('"S"', json.dumps(page, default=str))  # the title of the problem
        self.contest.starts_at = NOW - timedelta(hours=1)
        self.contest.ends_at = NOW + timedelta(hours=5)
        self.contest.timing_mode, self.contest.personal_duration = TimingMode.personal, timedelta(hours=1)
        self.s.commit()
        self.enter(self.a)
        page = sc.contest_view(self.s, self.a, "c", NOW)
        self.assertEqual((page["problems"], page["problem_count"]), ([], 1))  # not started: nothing named
        self.refused("press_start", sp.problem_view, self.s, self.a, self.blobs, self.settings, "c", "A", NOW)
        sc.start(self.s, self.a, "c", NOW)
        self.s.commit()
        page = sc.contest_view(self.s, self.a, "c", NOW)
        self.assertEqual([(p["label"], p["titles"]) for p in page["problems"]], [("A", {"ru": "S"})])

    # ---------------------------------------------------------------- problems
    def test_the_statement_shows_open_examples_only_and_pictures_only_of_statement_images(self):
        self.refused("not_participant", sp.problem_view, self.s, self.a, self.blobs, self.settings, "c", "A", NOW)
        self.enter(self.a)
        hidden_in = self.blob(b"HIDDEN-TEST-INPUT\n")
        self.s.execute(TestCase.__table__.update().where(TestCase.group_id == self.seed["tests"][0].group_id)
                       .values(input_sha256=hidden_in))
        picture = self.blob(PNG)
        self.version.files = {"statement/images/p.png": picture, "tests/main/1.in": hidden_in,
                              "statement/ru.md": self.blob(b"# S\n")}
        self.s.commit()
        view = sp.problem_view(self.s, self.a, self.blobs, self.settings, "c", "A", NOW)
        self.assertEqual(view["examples"], [{"group": "samples", "name": "s1", "input": "1 2\n", "answer": "3\n",
                                             "truncated": False}])
        self.assertNotIn("HIDDEN-TEST-INPUT", json.dumps(view, default=str))
        self.assertEqual([lang["key"] for lang in view["languages"]], ["c", "cpp", "python"])
        request = NS(headers={})
        got = sp.statement_file(self.s, self.a, self.blobs, "c", "A", "statement/images/p.png", NOW, request=request)
        self.assertEqual((got.status_code, got.media_type), (200, "image/png"))
        for path in ("tests/main/1.in", "statement/ru.md", "statement/images/../../tests/main/1.in",
                     "statement/images/none.png", None):
            with self.subTest(path=path):
                self.refused("file_not_found", sp.statement_file, self.s, self.a, self.blobs, "c", "A", path, NOW,
                             request=request)
        self.refused("problem_not_found", sp.problem_view, self.s, self.a, self.blobs, self.settings, "c", "B", NOW)
        self.cp.available_from = NOW + timedelta(minutes=1)  # not there yet: the same answer as no problem at all
        self.s.commit()
        self.refused("problem_not_found", sp.problem_view, self.s, self.a, self.blobs, self.settings, "c", "A", NOW)

    def test_a_quiz_never_shows_its_answers(self):
        v = ProblemVersion(problem_id=self.version.problem_id, version=2, status=VersionStatus.ready,
                           type=ProblemType.choice, titles={"ru": "Q"}, statements={"ru": ""},
                           config={"feedback": "full", "questions": [
                               {"id": "q1", "text": "2+2?", "options": [{"id": "a", "text": "4"}, {"id": "b", "text": "5"}],
                                "correct": ["a"], "explanation": "SECRET-EXPLANATION"}]})
        self.s.add(v)
        self.s.flush()
        self.cp.problem_version_id = v.id
        self.s.commit()
        self.enter(self.a)
        view = sp.problem_view(self.s, self.a, self.blobs, self.settings, "c", "A", NOW)
        self.assertEqual(view["questions"], [{"id": "q1", "text": "2+2?", "points": 1,
                                              "options": [{"id": "a", "text": "4"}, {"id": "b", "text": "5"}]}])
        self.assertNotIn("SECRET", json.dumps(view, default=str))
        self.refused("answers_expected", self.submit, self.a)
        made = self.submit(self.a, answers={"q1": ["a"]}, language=None)
        self.assertEqual(self.s.get(Submission, made["id"]).answers, {"q1": ["a"]})
        self.refused("answers_invalid", self.submit, self.a, answers={"q1": ["c"]}, language=None)

    # ---------------------------------------------------------------- submitting
    def test_a_submission_on_the_one_path_and_nothing_stored_on_a_refusal(self):
        self.refused("not_participant", self.submit, self.a)
        self.enter(self.a)
        made = self.submit(self.a, source="﻿print(1)\r\n".encode(), ip="10.0.0.7")
        sub = self.s.get(Submission, made["id"])
        self.assertEqual((sub.language_key, sub.ip_address, sub.created_at, sub.late, made["counted"]),
                         ("python", "10.0.0.7", NOW, False, True))
        self.assertEqual(self.blobs.get(sub.source_sha256), b"print(1)\r\n")  # without the BOM, CRLF kept
        self.assertEqual(self.s.execute(select(Judgement.status).where(Judgement.submission_id == sub.id)
                                        ).scalar_one(), JudgementStatus.queued)
        blobs_before = sorted(p.name for p in self.blobs.root.rglob("*") if p.is_file())
        rows_before = self.s.execute(select(func.count()).select_from(Submission)).scalar_one()
        for code, kw in [("source_encoding", {"source": "print('я')".encode("cp1251")}),
                         ("source_empty", {"source": b"  \n"}),
                         ("source_too_large", {"source": b"x" * (256 * 1024 + 1)}),
                         ("language_not_allowed", {"language": "pascal"}),
                         ("archive_invalid", {"archive": b"not a zip"}),
                         ("source_expected", {"answers": {}}),
                         ("invalid_request", {"source": b"1", "archive": b"2"})]:
            with self.subTest(code=code):
                self.refused(code, self.submit, self.a, **kw)
        self.contest.languages = ["cpp"]
        self.s.commit()
        self.refused("language_not_allowed", self.submit, self.a)
        self.assertEqual(sorted(p.name for p in self.blobs.root.rglob("*") if p.is_file()), blobs_before)
        self.assertEqual(self.s.execute(select(func.count()).select_from(Submission)).scalar_one(), rows_before)
        self.refused("contest_not_found", self.submit, self.b, slug="nope")

    def test_the_rate_counts_every_contest_and_answers_when_to_retry(self):
        self.enter(self.a)
        two = SubmitSettings(rate_per_min=2)
        self.submit(self.a, settings=two, now=NOW - timedelta(seconds=50))
        self.submit(self.a, settings=two, now=NOW - timedelta(seconds=10))
        e = self.refused("rate_limited", self.submit, self.a, settings=two)
        self.assertEqual(e.retry_after_s, 10)
        self.submit(self.a, settings=two, now=NOW + timedelta(seconds=11))
        self.enter(self.b)
        self.submit(self.b, settings=two)  # another student: their own count

    def test_two_submissions_at_once_one_is_refused_by_the_cooldown(self):
        self.contest.submit_cooldown_sec = 15
        self.s.commit()
        self.enter(self.a)
        barrier, results = threading.Barrier(2), []

        def one():
            with self.Session() as s:
                user = s.get(type(self.a), self.a.id)
                barrier.wait()
                try:
                    sv.submit(s, user, self.blobs, self.settings, "c", "A", NOW, language="python", source=b"print(1)\n")
                    s.commit()
                    results.append("ok")
                except StudentError as exc:
                    s.rollback()
                    results.append(exc.code)
        threads = [threading.Thread(target=one) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), ["cooldown", "ok"])
        self.assertEqual(self.s.execute(select(func.count()).select_from(Submission)).scalar_one(), 1)

    def test_late_and_upsolving(self):
        self.enter(self.a)
        self.cp.due_at = NOW - timedelta(hours=1)
        self.s.commit()
        made = self.submit(self.a)
        self.assertEqual((made["counted"], made["late"]), (True, True))
        self.contest.ends_at = NOW + timedelta(minutes=1)  # the first one was inside the window
        self.s.commit()
        made = self.submit(self.a, now=NOW + timedelta(minutes=2))
        self.assertEqual((made["counted"], made["late"]), (False, False))
        listed = sv.my_submissions(self.s, self.a, NOW, contest="c")["items"]
        self.assertEqual([(i["counted"], i["late"]) for i in listed], [(False, False), (True, True)])

    # ---------------------------------------------------------------- reading one's own
    def test_only_ones_own_submissions(self):
        self.enter(self.a)
        self.enter(self.b)
        mine, theirs = self.submit(self.a), self.submit(self.b)
        self.assertEqual([i["id"] for i in sv.my_submissions(self.s, self.a, NOW)["items"]], [mine["id"]])
        for fn, args in ((sv.my_submission, (self.s, self.a, self.blobs, theirs["id"], NOW)),
                         (sv.my_submission, (self.s, self.a, self.blobs, 2 ** 31, NOW)),
                         (sv.my_submission, (self.s, self.a, self.blobs, 0, NOW))):
            with self.subTest(args=args[3]):
                self.refused("submission_not_found", fn, *args)
        self.refused("submission_not_found", sv.my_source, self.s, self.a, self.blobs, theirs["id"],
                     request=NS(headers={}))
        got = sv.my_source(self.s, self.a, self.blobs, mine["id"], request=NS(headers={}))
        self.assertEqual(got.status_code, 200)
        page = sv.my_submissions(self.s, self.a, NOW, contest="c", label="A", limit=1)
        self.assertEqual((len(page["items"]), page["next_cursor"]), (1, None))
        self.refused("label_needs_contest", sv.my_submissions, self.s, self.a, NOW, label="A")

    def test_the_view_follows_the_version_it_was_judged_on(self):
        """Owner's amendment of stage 5: open groups only of the judgement's own version while it is pinned; the
        level is the stricter of the two versions; nothing of an older version's tests is matched with the new."""
        self.enter(self.a)
        self.version.config = {**self.version.config, "feedback": "full"}
        self.s.commit()
        made = self.submit(self.a)
        main = self.seed["tests"]
        self.finish(made["id"], "WA", "50", "100",
                    results=[(self.sample, "OK"), (main[0], "OK"), (main[1], "WA")])
        view = sv.my_submission(self.s, self.a, self.blobs, made["id"], NOW)
        text = json.dumps(view, default=str)
        self.assertEqual([t.get("input") for t in view["tests"]], ["1 2\n", None, None])
        self.assertIn("CHECKER-s1", text)
        for secret in ("CHECKER-t", "OUTPUT-t", "STDERR", "visible_to_student", "error"):
            self.assertNotIn(secret, text)
        self.assertEqual([t["test_no"] for t in view["tests"]], [1, 2, 3])  # full: every hidden test's verdict

        # a new version pinned: the same group names, "samples" now hidden, "main" now open
        v2 = ProblemVersion(problem_id=self.version.problem_id, version=2, status=VersionStatus.ready,
                            type=ProblemType.batch, titles={"ru": "S"}, statements={"ru": ""},
                            config={"feedback": "full"})
        self.s.add(v2)
        self.s.flush()
        self.s.add_all([TestGroup(version_id=v2.id, position=0, name="samples", points=0, visibility=Visibility.hidden),
                        TestGroup(version_id=v2.id, position=1, name="main", points=100, visibility=Visibility.open)])
        self.cp.problem_version_id = v2.id
        self.s.commit()
        old = sv.my_submission(self.s, self.a, self.blobs, made["id"], NOW)
        text = json.dumps(old, default=str)
        for secret in ("1 2", "CHECKER-", "OUTPUT-"):
            self.assertNotIn(secret, text)
        self.assertEqual([t["test_no"] for t in old["tests"]], [1, 2, 3])  # verdicts by the level, no content

        v2.config = {"feedback": "verdict_only"}  # stricter now: old details go too
        self.s.commit()
        strict = sv.my_submission(self.s, self.a, self.blobs, made["id"], NOW)
        self.assertEqual((strict["tests"], [gr["name"] for gr in strict["groups"]]), ([], []))
        self.assertEqual((strict["verdict"], strict["score"]), ("WA", Decimal("50.00")))

    def test_rechecking_shows_the_current_judgement(self):
        from app import queue as q
        self.enter(self.a)
        made = self.submit(self.a)
        self.finish(made["id"], "OK")
        q.request_rejudge(self.s, made["id"])
        self.s.commit()
        view = sv.my_submission(self.s, self.a, self.blobs, made["id"], NOW)
        self.assertEqual((view["status"], view["verdict"], view["rechecking"]), ("done", "OK", True))

    # ---------------------------------------------------------------- entering: the rate in the database (D-093)
    def test_attempts_to_enter_are_counted_in_the_database(self):
        self.contest.access_mode, self.contest.invite_code = AccessMode.code, "ACDEFG"
        self.s.commit()
        for i in range(10):
            got = self.enter(self.a, code="WRONG1", now=NOW + timedelta(seconds=i))
            self.assertEqual((got.ok, got.code), (False, "wrong_code"))
        e = self.refused("rate_limited", sc.enter, self.s, self.a, "c", NOW + timedelta(seconds=30), self.settings,
                         "ACDEFG")
        self.assertEqual(e.retry_after_s, 570)
        with self.Session() as other:  # another process, or the same one after a restart: the same count
            with self.assertRaises(StudentError) as caught:
                sc.enter(other, other.get(type(self.a), self.a.id), "c", NOW + timedelta(seconds=31), self.settings,
                         "ACDEFG")
            self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(self.s.execute(select(func.count()).select_from(ContestEntryAttempt)).scalar_one(), 10)
        self.assertEqual(self.enter(self.b, "nope").code, "contest_not_found")  # also counted, for its student
        got = self.enter(self.a, code="acd-efg", now=NOW + timedelta(minutes=10))
        self.assertEqual((got.ok, got.code, got.participation.joined_via.value), (True, "ok", "code"))
        outcomes = self.s.execute(select(ContestEntryAttempt.outcome, ContestEntryAttempt.contest_id)
                                  .where(ContestEntryAttempt.user_id == self.b.id)).all()
        self.assertEqual([tuple(o) for o in outcomes], [("contest_not_found", None)])
        from app.auth import users
        erased = users.delete_user(self.s, audit.Actor(cli="cli:test"), self.b, NOW)  # not a trace: erased with them
        self.s.commit()
        self.assertEqual(erased["entry_attempts_deleted"], 1)
        from app.auth import cleanup
        from app.settings import AuthSettings
        pruned = cleanup.prune(self.s, AuthSettings(retention_days=1), NOW + timedelta(days=2))
        self.assertEqual(pruned["entry_attempts"], 11)

    # ---------------------------------------------------------------- the leaderboard
    def test_standings(self):
        self.contest.leaderboard = LeaderboardMode.visible
        self.cp.points = Decimal(50)
        self.s.commit()
        for user in (self.a, self.b):
            self.enter(user)
        self.finish(self.submit(self.a)["id"], "OK", "100", "100")
        self.finish(self.submit(self.b, now=NOW + timedelta(seconds=1))["id"], "PARTIAL", "40", "100")
        board = ss.standings(self.s, self.a, "c", NOW)
        self.assertEqual([(r["place"], r["name"], r["score"], r["me"]) for r in board["rows"]],
                         [(1, "S1", Decimal("50.00"), True), (2, "S2", Decimal("20.00"), False)])
        self.assertNotIn("student1", json.dumps(board, default=str))  # logins never
        self.refused("group_not_found", ss.standings, self.s, self.a, "c", NOW, group_id=99)
        cli = audit.Actor(cli="cli:test")
        group = g.create(self.s, cli, "7Б", NOW)
        g.add_members(self.s, cli, group, [self.b], NOW)
        self.s.add(ContestGroup(contest_id=self.contest.id, group_id=group.id))
        self.s.commit()
        only = ss.standings(self.s, self.a, "c", NOW, group_id=group.id)
        self.assertEqual([r["name"] for r in only["rows"]], ["S2"])
        self.contest.leaderboard = LeaderboardMode.hidden
        self.s.commit()
        self.refused("standings_closed", ss.standings, self.s, self.a, "c", NOW)

    def test_standings_of_a_personal_contest_wait_for_ones_own_window(self):
        self.contest.leaderboard = LeaderboardMode.visible
        self.contest.timing_mode, self.contest.personal_duration = TimingMode.personal, timedelta(hours=1)
        self.s.commit()
        self.enter(self.a)
        sc.start(self.s, self.a, "c", NOW)
        self.s.commit()
        self.refused("standings_closed", ss.standings, self.s, self.a, "c", NOW + timedelta(minutes=59))
        self.assertEqual(len(ss.standings(self.s, self.a, "c", NOW + timedelta(hours=1))["rows"]), 1)
