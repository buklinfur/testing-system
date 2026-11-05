"""/api/admin submissions on a real PostgreSQL: filters (group = current members, late), the teacher's view with
every test and the history, the source under the rules of 5.7, rejudges in two steps, judgectl submission list."""
import io
import json
import tempfile
import unittest
import zipfile

from tests.pg.apibase import HAVE_API, ORIGIN, ApiCase
from tests.pg.pgbase import HAVE_SA, URL, requires_pg

if URL and HAVE_SA and HAVE_API:
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select, text

    from app import groups as g
    from app import queue as q
    from app.auth import audit
    from app.blobstore import BlobStore
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import AuditLog, Judgement, JudgementStatus, Submission, TestResult, Verdict
    from app.settings import AuthSettings, Settings


@requires_pg
@unittest.skipUnless(HAVE_API, "needs fastapi and httpx")
class AdminSubmissionsTests(ApiCase):
    def settings(self):
        self.blob_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.blob_tmp.cleanup)
        return Settings(public_origin=ORIGIN, blob_dir=self.blob_tmp.name,
                        auth=AuthSettings(secret_key=Fernet.generate_key().decode()))

    def setUp(self):
        super().setUp()
        self.boss, password, secret = self.make_admin()
        self.b = self.browser()
        self.h = self.csrf(self.admin_login(self.b, password, secret))
        self.blobs = BlobStore(self.blob_tmp.name)
        self.a, _ = self.make_student("Иванов", "Пётр")
        self.other, _ = self.make_student("Петрова", "Анна")
        self.group = g.create(self.s, self.cli, "7Б", self.now)
        g.add_members(self.s, self.cli, self.group, [self.a], self.now)
        cp, tests = self.seed["cp"], self.seed["tests"]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", "print(1)\n")
            zf.writestr("lib/<b>evil</b>.py", "x = 1\n")
        self.py = self.add(self.a, b"print(int(input()) + 1)  # <script>alert(1)</script>\n",
                           verdict=Verdict.OK, results=[(tests[0], Verdict.OK, True), (tests[1], Verdict.OK, False)])
        self.zip = self.add(self.a, archive.getvalue(), archive=True, late=True, verdict=Verdict.WA,
                            results=[(tests[0], Verdict.WA, False)])
        self.waiting = self.add(self.other, b"print(0)\n")
        self.answers = Submission(user_id=self.other.id, contest_problem_id=cp.id, answers={"q1": "a"})
        self.s.add(self.answers)
        self.s.commit()

    def add(self, user, source, archive=False, late=False, verdict=None, results=()):
        sha = self.blobs.put(source)
        if self.s.execute(text("SELECT 1 FROM blobs WHERE sha256 = :s"), {"s": sha}).first() is None:
            self.s.execute(text("INSERT INTO blobs (sha256, size_bytes) VALUES (:s, :n)"), {"s": sha, "n": len(source)})
        sub = Submission(user_id=user.id, contest_problem_id=self.seed["cp"].id, language_key="python",
                         source_sha256=sha, is_archive=archive, entry_file="main.py" if archive else None, late=late)
        self.s.add(sub)
        self.s.flush()
        j = q.enqueue(self.s, sub)
        if verdict is not None:
            j.status, j.verdict, j.compile_log = JudgementStatus.finished, verdict, "warning: <i>x</i>"
            for case, v, visible in results:
                self.s.add(TestResult(judgement_id=j.id, test_case_id=case.id, verdict=v, time_ms=5,
                                      visible_to_student=visible, checker_message="ok <b>", stderr_excerpt="oops"))
        self.s.commit()
        return sub

    def get(self, path, **kw):
        return self.b.get(f"/api/admin{path}", **kw)

    def post(self, path, **kw):
        return self.b.post(f"/api/admin{path}", headers=self.h, **kw)

    def ok(self, r, status=200):
        self.assertEqual(r.status_code, status, r.text)
        return r.json()

    def ids(self, **params):
        return [i["id"] for i in self.ok(self.get("/submissions", params=params))["items"]]

    def refused(self, r, status, code):
        self.assertEqual((r.status_code, r.json()["detail"]), (status, code), r.text)
        return r.json()

    # ---------------------------------------------------------------- lists
    def test_filters(self):
        every = [self.answers.id, self.waiting.id, self.zip.id, self.py.id]  # newest first
        self.assertEqual(self.ids(), every)
        self.assertEqual(self.ids(contest="c"), every)
        self.assertEqual(self.ids(contest="c", label="A"), every)
        self.assertEqual(self.ids(problem="sum"), every)
        self.assertEqual(self.ids(user_id=self.a.id), [self.zip.id, self.py.id])
        self.assertEqual(self.ids(group_id=self.group.id), [self.zip.id, self.py.id])
        self.assertEqual(self.ids(late="true"), [self.zip.id])
        self.assertEqual(self.ids(late="false"), [self.answers.id, self.waiting.id, self.py.id])
        self.assertEqual(self.ids(verdict="WA"), [self.zip.id])
        self.assertEqual(self.ids(status="queued"), [self.answers.id, self.waiting.id][1:])  # answers: no judgement
        self.assertEqual(self.ids(group_id=self.group.id, late="true"), [self.zip.id])
        page = self.ok(self.get("/submissions", params={"limit": 2}))
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual([i["id"] for i in self.ok(self.get("/submissions", params={"cursor": page["next_cursor"]}))
                          ["items"]], every[2:])
        item = self.ok(self.get("/submissions", params={"late": "true"}))["items"][0]
        self.assertEqual((item["user"]["login"], item["contest"], item["label"], item["late"],
                          item["judgement"]["verdict"]), ("ivanov.p", "c", "A", True, "WA"))

        g.remove_members(self.s, self.cli, self.group, [self.a], self.now)  # the group is its current members
        self.s.commit()
        self.assertEqual(self.ids(group_id=self.group.id), [])

        self.refused(self.get("/submissions", params={"label": "A"}), 422, "label_needs_contest")
        self.refused(self.get("/submissions", params={"contest": "nope"}), 404, "contest_not_found")
        self.refused(self.get("/submissions", params={"contest": "c", "label": "Z"}), 404, "contest_problem_not_found")
        self.refused(self.get("/submissions", params={"group_id": 999}), 404, "group_not_found")
        self.refused(self.get("/submissions", params={"problem": "nope"}), 404, "problem_not_found")
        self.refused(self.get("/submissions", params={"verdict": "XX"}), 422, "invalid_request")

    # ---------------------------------------------------------------- one submission
    def test_the_teachers_view_has_every_test_and_the_history(self):
        q.request_rejudge(self.s, self.py.id, requested_by=self.boss.id)
        self.s.commit()
        data = self.ok(self.get(f"/submissions/{self.py.id}"))
        self.assertEqual([t["visible_to_student"] for t in data["tests"]], [True, False])  # hidden ones too
        self.assertEqual((data["tests"][1]["test"], data["tests"][1]["group"], data["tests"][1]["stderr_excerpt"],
                          data["tests"][1]["checker_message"]), ("t1", "main", "oops", "ok <b>"))
        self.assertEqual([(j["reason"], j["current"], j["requested_by"]) for j in data["judgements"]],
                         [("initial", True, None), ("rejudge", False, "boss")])
        self.assertEqual(data["judgements"][0]["compile_log"], "warning: <i>x</i>")  # as stored: text for the UI
        self.assertEqual(data["tests_of"], data["judgements"][0]["id"])
        self.assertIn("<script>", data["source"]["text"])
        self.assertEqual((data["source"]["name"], data["source"]["truncated"]), (f"submission-{self.py.id}.py", False))
        other = self.ok(self.get(f"/submissions/{self.py.id}", params={"judgement_id": data["judgements"][1]["id"]}))
        self.assertEqual(other["tests"], [])  # the rejudge has no results yet
        foreign = self.s.execute(select(Judgement.id).where(Judgement.submission_id == self.zip.id)).scalar()
        self.refused(self.get(f"/submissions/{self.py.id}", params={"judgement_id": foreign}), 404,
                     "judgement_not_found")
        self.refused(self.get("/submissions/9999"), 404, "submission_not_found")

        packed = self.ok(self.get(f"/submissions/{self.zip.id}"))["source"]
        self.assertEqual(([f["name"] for f in packed["files"]], packed["entry_file"], "text" in packed),
                         (["main.py", "lib/<b>evil</b>.py"], "main.py", False))
        self.assertEqual(self.ok(self.get(f"/submissions/{self.answers.id}"))["answers"], {"q1": "a"})

    def test_a_big_source_is_cut_in_the_view(self):
        big = self.add(self.a, b"#" * (300 * 1024))
        source = self.ok(self.get(f"/submissions/{big.id}"))["source"]
        self.assertEqual((source["truncated"], len(source["text"]), source["size"]), (True, 256 * 1024, 300 * 1024))

    def test_the_source_is_never_a_page(self):
        for sub, media, name in ((self.py, "text/plain; charset=utf-8", f"submission-{self.py.id}.py"),
                                 (self.zip, "application/zip", f"submission-{self.zip.id}.zip")):
            with self.subTest(media):
                r = self.get(f"/submissions/{sub.id}/source")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(r.headers["content-type"], media)
                self.assertTrue(r.headers["content-disposition"].startswith(f'attachment; filename="{name}"'))
                self.assertEqual(r.headers["x-content-type-options"], "nosniff")
                self.assertEqual(r.headers["content-security-policy"], "sandbox; default-src 'none'")
                self.assertEqual(r.content, self.blobs.get(sub.source_sha256))
        self.refused(self.get(f"/submissions/{self.answers.id}/source"), 404, "no_source")
        self.refused(self.get("/submissions/9999/source"), 404, "submission_not_found")

    # ---------------------------------------------------------------- rejudges
    def judgements(self):
        self.s.expire_all()
        return self.s.execute(select(func.count()).select_from(Judgement)).scalar_one()

    def test_rejudge_in_two_steps(self):
        self.s.execute(text("UPDATE judgements SET status = 'finished', finished_at = now()"))  # nothing in flight
        self.s.commit()
        before = self.judgements()
        self.refused(self.post("/submissions/rejudge", json={"dry_run": True}), 422, "rejudge_needs_filter")
        self.refused(self.post("/submissions/rejudge", json={"user_id": self.a.id, "dry_run": True}), 422,
                     "rejudge_needs_filter")  # who, not what: still everything of a student
        self.refused(self.post("/submissions/rejudge", json={"contest": "c", "label": "A"}), 422, "dry_run_or_expect")
        self.refused(self.post("/submissions/rejudge", json={"contest": "c", "label": "A", "dry_run": True,
                                                             "expect": 3}), 422, "dry_run_or_expect")
        what = {"contest": "c", "label": "A"}
        self.assertEqual(self.ok(self.post("/submissions/rejudge", json={**what, "dry_run": True})),
                         {"affected": 4, "dry_run": True})
        self.assertEqual(self.ok(self.post("/submissions/rejudge", json={**what, "verdicts": ["WA"],
                                                                         "dry_run": True}))["affected"], 1)
        changed = self.refused(self.post("/submissions/rejudge", json={**what, "expect": 3}), 409, "count_changed")
        self.assertEqual(changed["affected"], 4)
        self.assertEqual(self.judgements(), before)  # nothing done
        self.assertEqual(self.s.execute(select(AuditLog).where(AuditLog.action == "submissions.rejudge")).all(), [])

        done = self.ok(self.post("/submissions/rejudge", json={**what, "expect": 4}))
        self.assertEqual((done["affected"], done["created"], done["contest_problems"]), (4, 4, [self.seed["cp"].id]))
        self.assertEqual(self.judgements(), before + 4)
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "submissions.rejudge")).scalar_one()
        self.assertEqual((entry.actor_user_id, entry.details["filter"], entry.details["affected"]),
                         (self.boss.id, {"contest": "c", "label": "A"}, 4))
        requested = self.s.execute(select(Judgement.requested_by).where(Judgement.reason == "rejudge")).scalars().all()
        self.assertEqual(set(requested), {self.boss.id})

        again = self.ok(self.post("/submissions/rejudge", json={"submission_ids": [self.py.id], "expect": 1}))
        self.assertEqual((again["retargeted"], again["created"]), (1, 0))  # still queued: aimed again, not doubled
        one = self.ok(self.post(f"/submissions/{self.zip.id}/rejudge"))
        self.assertEqual(one["kind"], "retargeted")
        self.refused(self.post("/submissions/9999/rejudge"), 404, "submission_not_found")
        self.refused(self.post("/submissions/rejudge", json={"problem_version": "sum@9", "dry_run": True}), 404,
                     "version_not_found")
        self.assertEqual(self.ok(self.post("/submissions/rejudge", json={"problem_version": "sum@1",
                                                                         "dry_run": True}))["affected"], 4)
        self.refused(self.post("/submissions/rejudge", json={**what, "dry_run": True, "everything": True}), 422,
                     "invalid_request")

    # ---------------------------------------------------------------- judgectl on the same services
    def test_judgectl_submission_list(self):
        out, err = io.StringIO(), io.StringIO()
        ctx = Context(session_factory=self.Session, settings=Settings(blob_dir=self.blob_tmp.name),
                      actor=audit.Actor(cli="cli:judge"), out=out, err=err, interactive=lambda: False,
                      clock=lambda: self.now)
        self.assertEqual(main(["submission", "list", "--group", "7Б", "--late", "--json"], ctx), 0, err.getvalue())
        self.assertEqual([r["id"] for r in json.loads(out.getvalue())], [self.zip.id])
        out.truncate(0), out.seek(0)
        self.assertEqual(main(["submission", "list", "--contest", "c", "--verdicts", "OK,WA"], ctx), 0)
        self.assertRegex(out.getvalue(), rf"\n{self.zip.id} .*ivanov\.p\s+c A .* WA .* yes\n")
        out.truncate(0), out.seek(0)
        self.assertEqual(main(["submission", "show", str(self.py.id), "--json"], ctx), 0)
        self.assertEqual(len(json.loads(out.getvalue())["tests"]), 2)
        self.assertEqual(main(["submission", "list", "--label", "A"], ctx), 1)
        self.assertIn("a label needs its contest", err.getvalue())
        self.assertEqual(main(["submission", "rejudge", "--contest", "c", "--problem", "A", "--dry-run"], ctx), 0)
        self.assertIn("4 submission(s) would be rejudged", out.getvalue())
