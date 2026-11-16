"""The HTTP API of a student (4b.1, docs/STUDENT_API.md §6) on a real PostgreSQL: every route behind a student
session (the sweep takes the routes from the app itself), every refusal code of CONTEST_STATUS reached by a request (CLASSROOM_STATUS: test_student_classroom_api_pg),
identifiers of others refused on every endpoint, nothing of a hidden test, a judgement's error or stderr anywhere in
the JSON, the submission body read after the cheap refusals with no transaction open and cut at the problem's limit,
the poll interval and the load indicator. Skipped unless JUDGE_TEST_DATABASE_URL is set."""
import io
import json
import re
import tempfile
import unittest
import zipfile
from datetime import timedelta
from decimal import Decimal

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select, text

    from app import groups as g
    from app import queue as q
    from app.blobstore import BlobStore
    from app.models import (AccessMode, AfterDueStatement, AfterDueSubmit, Blob, Contest, ContestGroup, ContestProblem,
                            ContestState, Judgement, Problem, JudgementStatus, JoinedVia, LeaderboardMode, Participation,
                            ProblemType, ProblemVersion, Submission, TestCase, TestGroup, TestResult, TimingMode,
                            Verdict, VersionStatus, Visibility)
    from app.settings import AuthSettings, QueueSettings, Settings, SubmitSettings
    from app.student_api import PREFIXES
    from app.student_api.deps import CONTEST_STATUS, UNREACHABLE

SAFE = {"get", "head", "options"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SOURCE = b"print(sum(map(int, input().split())))\n"
MARKERS = ("HIDDEN-INPUT", "HIDDEN-ANSWER", "STDERR-SECRET", "ERROR-SECRET", "CHECKER-HIDDEN", "OUTPUT-HIDDEN")
FORBIDDEN_KEYS = {"error", "stderr", "stderr_excerpt", "visible_to_student", "server_gave_up_at", "worker_id",
                  "password_hash", "csrf_token", "correct", "accept", "explanation"}


def concrete(path: str) -> str:
    """A real URL for a route template: ids are 1, a slug c, a label A, a file path x."""
    def one(m):
        name = m.group(1)
        return {"slug": "cc", "label": "A"}.get(name, "1" if name.endswith("id") else "x")
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", one, path)


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


def zipped(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class StudentApiTests(ApiCase):
    def settings(self):
        return Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=Fernet.generate_key().decode()),
                        blob_dir=self.blob_dir, submit=SubmitSettings(source_max_bytes=4096, archive_max_bytes=8192,
                                                                      rate_per_min=0, enter_rate=3))

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blob_dir = tmp.name
        super().setUp()
        self.blobs = BlobStore(self.blob_dir)
        self.reached = set()
        self.contest, self.cp, self.version = self.seed["cp"].contest, self.seed["cp"], self.seed["version"]
        self.contest.leaderboard, self.contest.submit_cooldown_sec = LeaderboardMode.visible, 0
        self.contest.slug = "cc"  # a slug as contest.schema.json writes it (2..63): the submission path is exempt
        self.s.commit()

    # ---------------------------------------------------------------- helpers
    def student_browser(self, surname="Иванов", first="Пётр"):
        user, password = self.make_student(surname, first)
        b = self.browser()
        r = self.login(b, user.login, password)
        self.assertEqual(r.status_code, 200, r.text)
        b.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return user, b

    def expect(self, r, status, code=None):
        self.assertEqual(r.status_code, status, r.text)
        if code is not None:
            self.assertEqual(r.json()["detail"], code, r.text)
            self.reached.add(code)
        return r

    def blob(self, data: bytes) -> str:
        sha = self.blobs.put(data)
        if self.s.get(Blob, sha) is None:
            self.s.add(Blob(sha256=sha, size_bytes=len(data)))
            self.s.flush()
        return sha

    def contest_with_a(self, slug, **kw):
        c = Contest(slug=slug, title=slug.upper(), starts_at=kw.pop("starts_at", self.now - timedelta(days=1)),
                    state=kw.pop("state", ContestState.published), access_mode=kw.pop("access_mode", AccessMode.open),
                    created_by=self.seed["admin"].id, **kw)
        self.s.add(c)
        self.s.flush()
        self.s.add(ContestProblem(contest_id=c.id, problem_id=self.version.problem_id,
                                  problem_version_id=self.version.id, label="A", position=0))
        self.s.commit()
        return c

    def send(self, b, body, slug="cc", label="A", kind="text/plain", **params):
        if kind != "application/json" and "language" not in params:
            params["language"] = "python"
        return b.post(f"/api/contests/{slug}/problems/{label}/submissions", content=body, params=params,
                      headers={"Content-Type": kind})

    def count(self, model) -> int:
        self.s.rollback()
        return self.s.execute(select(func.count()).select_from(model)).scalar_one()

    def files(self) -> list:
        return sorted(str(p) for p in self.blobs.root.rglob("*") if p.is_file())

    # ---------------------------------------------------------------- the sweep of every route
    def routes(self):
        doc = self.app.openapi()
        out = [(m, p) for p, ops in doc["paths"].items() if p.startswith(PREFIXES) for m in ops]
        unknown = [p for p in doc["paths"] if not p.startswith(("/api/admin/", "/api/auth/") + PREFIXES)]
        self.assertEqual(unknown, [], "a new prefix of the public app: declare it (student_api.PREFIXES or admin)")
        # 12 of 4b.1, 11 of the feed, works and drafts (4b.2 stage 5), 10 of comments, materials and the library (D-103)
        self.assertEqual(len(out), 33)
        return out

    def test_only_a_fully_logged_in_student_with_csrf_gets_in(self):
        _student, student = self.student_browser()
        token = student.headers.pop("X-CSRF-Token")
        _boss, boss_password, secret = self.make_admin()
        admin = self.browser()
        ah = self.csrf(self.admin_login(admin, boss_password, secret))
        _half, half_password, _ = self.make_admin("half")
        half = self.browser()
        hh = self.csrf(self.login(half, "half", half_password))
        anonymous = self.browser()
        for method, path in self.routes():
            url = concrete(path)
            with self.subTest(method=method.upper(), path=path):
                r = anonymous.request(method, url)
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "not_authenticated"))
                r = half.request(method, url, headers=hh)
                self.assertEqual((r.status_code, r.json()["detail"]), (401, "mfa_required"))
                r = admin.request(method, url, headers=ah)
                if path == "/api/load":
                    self.assertEqual(r.status_code, 200)  # any full session sees the load
                    self.assertIsNone(r.json()["my_position"])
                else:
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "student_required"))
                if method not in SAFE:
                    r = student.request(method, url)  # the student's own browser, the Origin right, no CSRF
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "csrf_token_required"))
                    r = student.request(method, url, headers={"Origin": "https://evil.example",
                                                              "X-CSRF-Token": token})
                    self.assertEqual((r.status_code, r.json()["detail"]), (403, "origin_not_allowed"))

    # ---------------------------------------------------------------- one path through the API
    def test_a_student_enters_reads_submits_and_sees_their_result(self):
        user, b = self.student_browser()
        r = self.expect(b.get("/api/contests"), 200)
        self.assertEqual([(c["slug"], c["joined"]) for c in r.json()["items"]], [("cc", False)])
        self.assertTrue(r.json()["server_time"].endswith("Z"))
        page = self.expect(b.get("/api/contests/cc"), 200).json()
        self.assertEqual((page["phase"], page["problems"][0]["label"]), ("running", "A"))
        self.assertTrue(page["server_time"].endswith("Z"))
        self.assertEqual(self.expect(b.post("/api/contests/cc/enter"), 200).json()["entered"], "ok")
        problem = self.expect(b.get("/api/contests/cc/problems/A"), 200).json()
        self.assertEqual([lang["key"] for lang in problem["languages"]], ["c", "cpp", "python"])
        self.assertEqual(problem["source_max_kb"], 4)

        made = self.expect(self.send(b, SOURCE, user_id=999), 201).json()  # a user_id of a query is nothing
        sub = self.s.get(Submission, made["id"])
        self.assertEqual((sub.user_id, sub.created_at, made["counted"], made["late"]),
                         (user.id, self.now, True, False))
        view = self.expect(b.get(f"/api/submissions/{made['id']}"), 200).json()
        self.assertEqual((view["status"], view["poll_after_s"]), ("queued", 10))  # no worker online
        load = self.expect(b.get("/api/load"), 200).json()
        self.assertEqual((load["queue_length"], load["my_position"], load["level"]), (1, 1, "offline"))
        source = self.expect(b.get(f"/api/submissions/{made['id']}/source"), 200)
        self.assertEqual(source.content, SOURCE)
        listed = self.expect(b.get("/api/submissions", params={"contest": "cc", "label": "A"}), 200).json()
        self.assertEqual(([i["id"] for i in listed["items"]], listed["next_cursor"]), ([made["id"]], None))

        archive = zipped({"main.py": SOURCE, "lib.py": b"X = 1\n"})
        made_zip = self.expect(self.send(b, archive, kind="application/zip", entry="main.py"), 201).json()
        self.assertTrue(self.s.get(Submission, made_zip["id"]).is_archive)
        got = self.expect(b.get(f"/api/submissions/{made_zip['id']}/source"), 200)
        self.assertEqual(got.content, archive)
        page = self.expect(b.get("/api/submissions", params={"limit": 1}), 200).json()
        self.assertEqual(([i["id"] for i in page["items"]], page["next_cursor"]), ([made_zip["id"]], made_zip["id"]))
        page = self.expect(b.get("/api/submissions", params={"limit": 1, "cursor": page["next_cursor"]}), 200).json()
        self.assertEqual([i["id"] for i in page["items"]], [made["id"]])

        j = self.s.execute(select(Judgement).where(Judgement.submission_id == made["id"])).scalar_one()
        j.status, j.verdict, j.score, j.max_score = JudgementStatus.finished, Verdict.OK, Decimal(100), Decimal(100)
        self.s.commit()
        view = self.expect(b.get(f"/api/submissions/{made['id']}"), 200).json()
        self.assertEqual((view["status"], view["verdict"]), ("done", "OK"))
        self.assertNotIn("poll_after_s", view)  # nothing is judged any more
        board = self.expect(b.get("/api/contests/cc/standings"), 200).json()
        self.assertEqual([(r["name"], r["me"]) for r in board["rows"]], [(user.display_name, True)])

    def test_a_quiz_takes_json_answers(self):
        self.quiz()
        _user, b = self.student_browser()
        self.expect(b.post("/api/contests/cc/enter"), 200)
        problem = self.expect(b.get("/api/contests/cc/problems/Q"), 200).json()
        self.assertNotIn("correct", json.dumps(problem))
        made = self.expect(b.post("/api/contests/cc/problems/Q/submissions", json={"answers": {"q1": ["a"]}}), 201)
        self.assertEqual(self.s.get(Submission, made.json()["id"]).answers, {"q1": ["a"]})
        self.expect(b.get(f"/api/submissions/{made.json()['id']}/source"), 404, "file_not_found")

    def quiz(self):
        problem = Problem(slug="quiz", owner_id=self.seed["admin"].id)
        self.s.add(problem)
        self.s.flush()
        v = ProblemVersion(problem_id=problem.id, version=1, status=VersionStatus.ready,
                           type=ProblemType.choice, titles={"ru": "Q"}, statements={"ru": ""},
                           config={"questions": [{"id": "q1", "text": "2+2?", "correct": ["a"],
                                                  "options": [{"id": "a", "text": "4"}, {"id": "b", "text": "5"}]}]})
        self.s.add(v)
        self.s.flush()
        self.s.add(ContestProblem(contest_id=self.contest.id, problem_id=v.problem_id, problem_version_id=v.id,
                                  label="Q", position=1))
        self.s.commit()

    # ---------------------------------------------------------------- every code by a request
    def test_every_code_is_reached_by_a_request(self):
        self.quiz()
        a_user, a = self.student_browser("Алексеев", "Антон")
        _b_user, b = self.student_browser("Борисов", "Борис")
        _c_user, c = self.student_browser("Васильев", "Виктор")
        d_user, d = self.student_browser("Григорьев", "Глеб")
        _boss, boss_password, secret = self.make_admin()
        admin = self.browser()
        self.admin_login(admin, boss_password, secret)
        self.expect(admin.get("/api/contests"), 403, "student_required")

        # the main contest c (open, fixed, running) for a
        self.expect(a.get("/api/contests/nope"), 404, "contest_not_found")
        self.expect(a.get("/api/contests/cc/problems/A"), 409, "not_participant")
        self.expect(self.send(a, SOURCE), 409, "not_participant")
        self.expect(a.post("/api/contests/cc/enter"), 200)
        self.expect(a.post("/api/contests/cc/enter"), 409, "already_joined")
        self.expect(a.get("/api/contests/cc/problems/B"), 404, "problem_not_found")
        self.expect(a.post("/api/contests/cc/start"), 409, "not_personal")
        self.expect(a.get("/api/submissions/999"), 404, "submission_not_found")
        self.expect(a.get("/api/submissions", params={"label": "A"}), 422, "label_needs_contest")
        self.expect(a.get("/api/contests/cc/standings", params={"group": 999}), 404, "group_not_found")
        self.expect(a.get("/api/contests/cc/problems/A/files/tests/main/1.in"), 404, "file_not_found")
        self.expect(self.send(a, SOURCE, language="cobol"), 422, "language_not_allowed")
        self.expect(self.send(a, b"  \n"), 422, "source_empty")
        self.expect(self.send(a, "print('я')\n".encode("cp1251")), 422, "source_encoding")
        self.expect(self.send(a, b"x" * 5000), 413, "source_too_large")
        self.expect(self.send(a, b"not a zip", kind="application/zip"), 422, "archive_invalid")
        many = zipped({f"f{i}.py": b"1\n" for i in range(21)} | {"main.py": SOURCE})
        self.expect(self.send(a, many, kind="application/zip"), 422, "archive_too_many_files")
        self.expect(self.send(a, zipped({"sol.py": SOURCE}), kind="application/zip"), 422, "entry_not_found")
        self.expect(self.send(a, zipped({"main.py": SOURCE}), kind="application/zip", entry="../x.py"), 422,
                    "entry_invalid")
        self.expect(a.post("/api/contests/cc/problems/A/submissions", json={"answers": {}}), 422, "source_expected")
        self.expect(self.send(a, b"{", label="Q", kind="application/json"), 422, "invalid_request")
        self.expect(self.send(a, SOURCE, label="Q"), 422, "answers_expected")
        self.expect(a.post("/api/contests/cc/problems/Q/submissions", json={"answers": {"zz": ["a"]}}), 422,
                    "answers_invalid")
        self.version.config = {**self.version.config, "submission": {"allow_archive": False}}
        self.s.commit()
        self.expect(self.send(a, zipped({"main.py": SOURCE}), kind="application/zip"), 422, "archive_not_allowed")
        self.contest.submit_cooldown_sec = 60
        self.s.commit()
        self.expect(self.send(a, SOURCE), 201)
        r = self.expect(self.send(a, SOURCE), 429, "cooldown")
        self.assertEqual((r.headers["Retry-After"], r.json()["retry_after_s"]), ("60", 60))
        self.contest.submit_cooldown_sec, self.contest.max_submissions_per_problem = 0, 1
        self.s.commit()
        self.expect(self.send(a, SOURCE), 409, "submission_limit")
        self.contest.max_submissions_per_problem = None
        self.cp.due_at, self.cp.after_due_submit = self.now - timedelta(minutes=1), AfterDueSubmit.no
        self.s.commit()
        self.expect(self.send(a, SOURCE), 409, "deadline_passed")
        self.cp.after_due_statement = AfterDueStatement.hidden
        self.s.commit()
        self.expect(a.get("/api/contests/cc/problems/A"), 409, "statement_hidden")
        self.contest.leaderboard = LeaderboardMode.hidden
        self.s.commit()
        self.expect(a.get("/api/contests/cc/standings"), 403, "standings_closed")

        # personal contests for b: one running, one not begun
        self.contest_with_a("p", timing_mode=TimingMode.personal, personal_duration=timedelta(hours=1))
        self.contest_with_a("f", starts_at=self.now + timedelta(days=1), timing_mode=TimingMode.personal,
                            personal_duration=timedelta(hours=1))
        self.expect(b.post("/api/contests/p/enter"), 200)
        self.expect(b.get("/api/contests/p/problems/A"), 409, "press_start")
        self.expect(b.post("/api/contests/p/start"), 200)
        self.expect(b.post("/api/contests/p/start"), 409, "already_started")
        self.expect(b.post("/api/contests/f/enter"), 200)
        self.expect(b.post("/api/contests/f/start"), 409, "not_started_yet")

        # a contest with a code for c: wrong codes, then the rate of attempts
        self.contest_with_a("k", access_mode=AccessMode.code, invite_code="ACDEFG")
        for _ in range(3):
            self.expect(c.post("/api/contests/k/enter", json={"code": "WRONG1"}), 403, "wrong_code")
        r = self.expect(c.post("/api/contests/k/enter", json={"code": "ACDEFG"}), 429, "rate_limited")
        self.assertEqual(r.headers["Retry-After"], str(r.json()["retry_after_s"]))

        # over, archived, and a group contest left for d
        self.contest_with_a("old", ends_at=self.now - timedelta(hours=1), allow_upsolving=False)
        self.expect(d.post("/api/contests/old/enter"), 409, "contest_over")
        arch = self.contest_with_a("arch", state=ContestState.archived)
        self.s.add(Participation(contest_id=arch.id, user_id=d_user.id, joined_at=self.now, joined_via=JoinedVia.open))
        self.s.commit()
        self.expect(d.post("/api/contests/arch/enter"), 409, "not_published")
        lesson = self.contest_with_a("lesson", access_mode=AccessMode.groups)
        group = g.create(self.s, self.cli, "7Б", self.now)
        g.add_members(self.s, self.cli, group, [d_user], self.now)
        self.s.add(ContestGroup(contest_id=lesson.id, group_id=group.id))
        self.s.commit()
        self.expect(d.post("/api/contests/lesson/enter"), 200)
        g.remove_members(self.s, self.cli, group, [d_user], self.now)
        self.s.commit()
        self.expect(d.get("/api/contests/lesson/problems/A"), 403, "no_access")

        self.assertEqual(sorted(set(CONTEST_STATUS) - set(UNREACHABLE) - self.reached), [],
                         "codes of CONTEST_STATUS no request reached")
        self.assertEqual(self.reached & set(UNREACHABLE), set())

    # ---------------------------------------------------------------- identifiers of others
    def test_nothing_of_another_student_on_any_endpoint(self):
        self.version.config = {**self.version.config, "feedback": "full"}
        self.s.commit()
        mine_user, me = self.student_browser("Алексеев", "Антон")
        other_user, other = self.student_browser("Борисов", "Борис")
        for b in (me, other):
            self.expect(b.post("/api/contests/cc/enter"), 200)
        theirs = self.expect(self.send(other, SOURCE), 201).json()["id"]
        mine = self.expect(self.send(me, SOURCE), 201).json()["id"]
        for url in (f"/api/submissions/{theirs}", f"/api/submissions/{theirs}/source", "/api/submissions/2147483647"):
            with self.subTest(url=url):
                self.expect(me.get(url), 404, "submission_not_found")
        listed = self.expect(me.get("/api/submissions"), 200).json()["items"]
        self.assertEqual([i["id"] for i in listed], [mine])
        listed = self.expect(me.get("/api/submissions", params={"cursor": mine + 1}), 200).json()["items"]
        self.assertEqual([i["id"] for i in listed], [mine])  # a cursor past another's submission shows only mine

        # a contest of a group the student is not in: one answer with no contest at all, on every endpoint
        hidden = self.contest_with_a("hidden", access_mode=AccessMode.groups)
        group = g.create(self.s, self.cli, "8А", self.now)
        g.add_members(self.s, self.cli, group, [other_user], self.now)
        self.s.add(ContestGroup(contest_id=hidden.id, group_id=group.id))
        self.s.commit()
        for method, url in (("get", "/api/contests/hidden"), ("post", "/api/contests/hidden/enter"),
                            ("post", "/api/contests/hidden/start"), ("get", "/api/contests/hidden/standings"),
                            ("get", "/api/contests/hidden/problems/A"),
                            ("get", "/api/contests/hidden/problems/A/files/statement/images/p.png"),
                            ("get", "/api/submissions?contest=hidden")):
            with self.subTest(url=url):
                self.expect(me.request(method, url), 404, "contest_not_found")
        self.expect(self.send(me, SOURCE, slug="hidden"), 404, "contest_not_found")
        self.assertNotIn("hidden", json.dumps(self.expect(me.get("/api/contests"), 200).json()))
        # a group of another contest as the filter of the leaderboard
        self.expect(me.get("/api/contests/cc/standings", params={"group": group.id}), 404, "group_not_found")
        # bodies take no identifiers: an unknown field is refused, not ignored
        r = self.expect(me.post("/api/contests/cc/enter", json={"code": None, "user_id": other_user.id}), 422,
                        "invalid_request")
        self.assertIn("user_id", json.dumps(r.json()))
        r = me.post("/api/contests/cc/problems/A/submissions", content=SOURCE, headers={"Content-Type": "text/plain"},
                    params={"language": "python", "user_id": other_user.id, "contest_problem_id": 99})
        self.assertEqual(self.s.get(Submission, self.expect(r, 201).json()["id"]).user_id, mine_user.id)
        self.assertEqual(self.count(Participation), 2)  # nothing was made for anyone else

    # ---------------------------------------------------------------- nothing hidden in any JSON
    def test_no_hidden_test_error_or_stderr_anywhere(self):
        version = self.version
        version.config = {**version.config, "feedback": "full"}
        hidden_in, hidden_out = self.blob(b"HIDDEN-INPUT\n"), self.blob(b"HIDDEN-ANSWER\n")
        for case in self.seed["tests"]:
            case.input_sha256, case.answer_sha256 = hidden_in, hidden_out
        samples = TestGroup(version_id=version.id, position=-1, name="samples", points=0,
                            visibility=Visibility.open, show_in_statement=True)
        self.s.add(samples)
        self.s.flush()
        sample = TestCase(group_id=samples.id, position=0, name="s1", input_sha256=self.blob(b"1 2\n"),
                          answer_sha256=self.blob(b"3\n"))
        self.s.add(sample)
        self.s.commit()
        _user, b = self.student_browser()
        self.expect(b.post("/api/contests/cc/enter"), 200)
        made = self.expect(self.send(b, SOURCE), 201).json()["id"]
        j = self.s.execute(select(Judgement).where(Judgement.submission_id == made)).scalar_one()
        j.status, j.verdict, j.score, j.max_score = JudgementStatus.finished, Verdict.WA, Decimal(50), Decimal(100)
        j.error = "ERROR-SECRET"
        j.summary = {"groups": [{"name": "samples", "points": "0", "score": "0"},
                                {"name": "main", "points": "100", "score": "50"}]}
        for case, verdict in ((sample, "OK"), (self.seed["tests"][0], "OK"), (self.seed["tests"][1], "WA")):
            hidden = case is not sample
            self.s.add(TestResult(judgement_id=j.id, test_case_id=case.id, verdict=Verdict(verdict), time_ms=5,
                                  memory_kb=100, stderr_excerpt="STDERR-SECRET", visible_to_student=True,
                                  checker_message="CHECKER-HIDDEN" if hidden else "CHECKER-OPEN",
                                  output_excerpt="OUTPUT-HIDDEN" if hidden else "OUTPUT-OPEN"))
        self.s.commit()
        answers = {}
        for url in ("/api/contests", "/api/contests/cc", "/api/contests/cc/problems/A", "/api/submissions",
                    f"/api/submissions/{made}", "/api/contests/cc/standings", "/api/load"):
            answers[url] = self.expect(b.get(url), 200).json()
        text_all = json.dumps(answers, ensure_ascii=False)
        for marker in MARKERS:
            self.assertNotIn(marker, text_all)
        self.assertEqual(set(keys(answers)) & FORBIDDEN_KEYS, set())
        view = answers[f"/api/submissions/{made}"]
        self.assertIn("CHECKER-OPEN", text_all)  # the open test is shown: the search is not vacuous
        self.assertEqual(len(view["tests"]), 3)  # full: the verdicts of hidden tests, never their content
        self.assertEqual(answers["/api/contests/cc/problems/A"]["examples"][0]["input"], "1 2\n")

    # ---------------------------------------------------------------- the body
    def test_the_body_is_read_after_the_cheap_refusals_without_a_transaction(self):
        user, b = self.student_browser()
        self.expect(b.post("/api/contests/cc/enter"), 200)
        part_id = self.s.execute(select(Participation.id).where(Participation.user_id == user.id)).scalar_one()
        self.s.commit()
        seen = {}

        def body():
            with self.engine.connect() as conn:  # while the body arrives: no lock held, no transaction open
                seen["locked"] = conn.execute(text("SELECT id FROM participations WHERE id = :i FOR UPDATE NOWAIT"),
                                              {"i": part_id}).scalar()
                seen["open"] = conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> "
                    "pg_backend_pid() AND state LIKE 'idle in transaction%'")).scalar()
                conn.rollback()
            yield SOURCE

        self.expect(b.post("/api/contests/cc/problems/A/submissions", content=body(), params={"language": "python"},
                           headers={"Content-Type": "text/plain"}), 201)
        self.assertEqual(seen, {"locked": part_id, "open": 0})

        # refusals before the body: the generator is never read
        def never():
            raise AssertionError("the body was read")
            yield b""  # pragma: no cover
        self.expect(b.post("/api/contests/cc/problems/A/submissions", content=never(),
                           headers={"Content-Type": "text/html"}), 415, "unsupported_media_type")
        self.expect(b.post("/api/contests/cc/problems/A/submissions", content=never(), params={"language": "cobol"},
                           headers={"Content-Type": "text/plain"}), 422, "language_not_allowed")
        self.expect(b.post("/api/contests/cc/problems/B/submissions", content=never(), params={"language": "python"},
                           headers={"Content-Type": "text/plain"}), 404, "problem_not_found")

    def test_a_body_over_the_limit_is_413_and_leaves_nothing(self):
        _user, b = self.student_browser()
        self.expect(b.post("/api/contests/cc/enter"), 200)
        rows, blobs = self.count(Submission), self.files()

        def chunks(n, size=1024):  # no Content-Length: counted while it arrives
            for _ in range(n):
                yield b"x" * size
        r = b.post("/api/contests/cc/problems/A/submissions", content=chunks(5), params={"language": "python"},
                   headers={"Content-Type": "text/plain"})
        self.assertNotIn("content-length", {k.lower() for k in r.request.headers})
        self.expect(r, 413, "source_too_large")
        self.expect(self.send(b, b"x" * 5000), 413, "source_too_large")  # declared, before any service
        self.expect(b.post("/api/contests/cc/problems/A/submissions", json={"answers": {"x": "y" * 70_000}}), 413,
                    "too_large")
        # the limit of the problem, known to the precheck before the body: max_archive_kb 1 of 8 KB of the server
        self.version.config = {**self.version.config, "submission": {"max_archive_kb": 1}}
        self.s.commit()
        big = zipped({"main.py": SOURCE, "data.txt": bytes(range(256)) * 12})
        self.assertTrue(1024 < len(big) < 8192)
        self.expect(b.post("/api/contests/cc/problems/A/submissions", content=iter([big]),
                           params={"language": "python"}, headers={"Content-Type": "application/zip"}), 413,
                    "source_too_large")
        self.assertEqual((self.count(Submission), self.files()), (rows, blobs))

    # ---------------------------------------------------------------- files and polling
    def test_a_picture_of_the_statement_with_its_etag(self):
        version = self.version
        version.files = {"statement/images/p.png": self.blob(PNG), "tests/main/1.in": self.blob(b"HIDDEN-INPUT\n")}
        self.s.commit()
        _user, b = self.student_browser()
        self.expect(b.get("/api/contests/cc/problems/A/files/statement/images/p.png"), 409, "not_participant")
        self.expect(b.post("/api/contests/cc/enter"), 200)
        r = self.expect(b.get("/api/contests/cc/problems/A/files/statement/images/p.png"), 200)
        self.assertEqual((r.content, r.headers["content-type"]), (PNG, "image/png"))
        again = b.get("/api/contests/cc/problems/A/files/statement/images/p.png",
                      headers={"If-None-Match": r.headers["etag"]})
        self.assertEqual((again.status_code, again.content), (304, b""))
        for path in ("tests/main/1.in", "statement/images/../../tests/main/1.in", "statement/images/none.png"):
            with self.subTest(path=path):
                self.expect(b.get(f"/api/contests/cc/problems/A/files/{path}"), 404, "file_not_found")

    def test_the_poll_interval_follows_the_queue(self):
        _user, b = self.student_browser()
        self.expect(b.post("/api/contests/cc/enter"), 200)
        made = [self.expect(self.send(b, SOURCE), 201).json()["id"] for _ in range(4)]
        self.assertEqual(self.expect(b.get(f"/api/submissions/{made[-1]}"), 200).json()["poll_after_s"], 10)
        token = q.authenticate(self.s, q.create_worker_token(self.s, "tok-w0", ["cpu"]))
        worker = q.register_worker(self.s, token, "w0", ["cpu"], {}, QueueSettings())
        self.s.commit()
        polls = [self.expect(b.get(f"/api/submissions/{i}"), 200).json()["poll_after_s"] for i in made]
        self.assertEqual(polls, [2, 4, 6, 8])  # one worker: 2 s more per place
        j = self.s.execute(select(Judgement).where(Judgement.submission_id == made[0])).scalar_one()
        j.status, j.worker_id = JudgementStatus.running, worker.id
        self.s.commit()
        self.assertEqual(self.expect(b.get(f"/api/submissions/{made[0]}"), 200).json()["poll_after_s"], 2)
        load = self.expect(b.get("/api/load"), 200).json()
        self.assertEqual((load["queue_length"], load["workers_online"], load["my_position"]), (3, 1, 1))


if __name__ == "__main__":
    unittest.main()
