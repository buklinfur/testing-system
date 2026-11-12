"""judgectl problem / contest / submission / status and dev_seed --contest on a real PostgreSQL."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests.pg.pgbase import HAVE_SA, ROOT, URL, PgCase, requires_pg

EXAMPLES = ROOT / "problems-examples"
T0 = datetime(2026, 11, 10, 3, 0, tzinfo=timezone.utc)  # 10:00 in Novosibirsk

if URL and HAVE_SA:
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select, text

    from app import admin_cli, db, dev_seed
    from app.auth import audit
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.judgectl.ops import migration_head
    from app.models import AuditLog, Blob, Contest, Judgement, Participation, ProblemVersion, Submission
    from app.settings import AuthSettings, Settings

CONTEST = """format_version: 1
slug: olymp
title: "Олимпиада"
starts_at: 2026-11-10 09:00
ends_at: 2026-11-10 18:00
timing_mode: personal
personal_duration: 1h
submit_cooldown_sec: 600
problems:
  - {label: A, problem: sum-of-two}
"""


def pack(folder: Path) -> bytes:
    r = subprocess.run(["bash", str(ROOT / "tools" / "pack-problem.sh"), str(folder)], capture_output=True, check=True)
    return r.stdout


@requires_pg
class JudgectlContestTests(PgCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.blob_dir = self.tmp / "blobs"
        self.key = Fernet.generate_key().decode()

    def run_cli(self, *argv, stdin=b"", origin="https://judge.example.ru", alias=False):
        out, err = io.StringIO(), io.StringIO()
        settings = Settings(public_origin=origin, timezone="Asia/Novosibirsk", blob_dir=str(self.blob_dir),
                            auth=AuthSettings(secret_key=self.key))
        ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:judge"),
                      out=out, err=err, stdin=io.BytesIO(stdin), interactive=lambda: False, clock=lambda: T0)
        code = (admin_cli.main if alias else main)(list(argv), ctx)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, **kw):
        code, out, err = self.run_cli(*argv, **kw)
        self.assertEqual(code, 0, err)
        return out

    def file(self, name, body):
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return str(path)

    def blob_files(self):
        return sorted(p.name for p in self.blob_dir.rglob("*") if p.is_file()) if self.blob_dir.exists() else []

    # ---------------------------------------------------------------- problem
    def test_problem_import_list_versions_show_archive(self):
        self.assertIn("sum-of-two: imported as v1 (pending_check); check #",
                      self.ok("problem", "import", str(EXAMPLES / "sum-of-two")))
        self.assertIn("already exists as v1", self.ok("problem", "import", str(EXAMPLES / "sum-of-two")))
        copy = self.tmp / "sum-of-two"
        shutil.copytree(EXAMPLES / "sum-of-two", copy)
        (copy / "statement" / "ru.md").write_text("Другое.\n", encoding="utf-8")
        blobs = self.blob_files()
        out = self.ok("problem", "import", str(copy), "--dry-run")
        self.assertIn("would be imported as v2 (dry run: nothing was written)", out)
        self.assertEqual(self.blob_files(), blobs)
        images = copy / "statement" / "images"  # a substituted picture: imported, with a warning (D-071)
        images.mkdir(parents=True, exist_ok=True)
        (images / "fake.png").write_bytes(b"<svg/>")
        code, out, err = self.run_cli("problem", "import", str(copy), "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("warning: statement/images/fake.png: not a PNG, JPEG or WebP picture", err)
        data = json.loads(self.ok("problem", "import", "-", "--json", stdin=pack(copy)))  # a zip on stdin
        self.assertTrue(any(w.startswith("statement/images/fake.png") for w in data["warnings"]))
        self.assertEqual((data["result"], data["version"], data["status"]), ("created", 2, "pending_check"))
        self.assertIsInstance(data["check_id"], int)
        self.assertRegex(self.ok("problem", "versions", "sum-of-two"),
                         r"\n1  pending_check .*\n2  pending_check .* yes\n")
        self.assertIn("check #", self.ok("problem", "show", "sum-of-two@2"))
        self.assertEqual(self.finish_checks(), 2)
        self.assertRegex(self.ok("problem", "versions", "sum-of-two"), r"\n1  ready .*\n2  ready .* yes\n")
        listing = json.loads(self.ok("problem", "list", "--json"))
        mine = {r["slug"]: r for r in listing}
        self.assertEqual((mine["sum-of-two"]["latest"], mine["sum-of-two"]["versions"]), (2, 2))
        shown = self.ok("problem", "show", "sum-of-two@1")
        self.assertIn("sum-of-two v1  ready  batch", shown)
        self.assertIn("solutions/ok.py", shown)
        self.assertEqual(self.run_cli("problem", "show", "sum-of-two@9")[0], 1)
        self.ok("problem", "archive", "sum-of-two")
        code, _, err = self.run_cli("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.assertEqual(code, 1)
        self.assertIn("judgectl problem unarchive sum-of-two", err)
        self.assertNotIn("sum-of-two", self.ok("problem", "list"))
        self.assertIn("sum-of-two", self.ok("problem", "list", "--all"))
        self.ok("problem", "unarchive", "sum-of-two")
        actions = [a for a in self.s.execute(select(AuditLog.action)).scalars() if a.startswith("problem.")]
        self.assertEqual(actions, ["problem.import", "problem.import", "problem.archive", "problem.unarchive"])

    def test_refused_imports_are_reported_with_every_reason(self):
        code, _, err = self.run_cli("problem", "import", "-", stdin=b"not a zip")
        self.assertEqual(code, 1)
        self.assertIn("not a zip archive", err)
        code, _, err = self.run_cli("problem", "import", "-")
        self.assertIn("nothing on stdin", err)
        code, _, err = self.run_cli("problem", "import", "/nonexistent/x.zip")
        self.assertIn("give the file on stdin with `-`", err)

    # ---------------------------------------------------------------- contest
    def test_contest_apply_dry_run_show_publish_pin_code(self):
        self.ok("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.finish_checks()  # a worker's check makes v1 ready
        path = self.file("olymp.yaml", CONTEST)
        out, err = self.run_cli("contest", "apply", path)[1:]
        self.assertIn("contest created (draft)", out)
        self.assertIn("judgectl contest publish olymp", err)
        self.assertIn("no changes", self.ok("contest", "apply", "-", stdin=CONTEST.encode()))
        changed = CONTEST.replace("Олимпиада", "Олимпиада 7Б")
        out = self.ok("contest", "apply", "-", "--dry-run", stdin=changed.encode())
        self.assertIn("would change (dry run: nothing was written)", out)
        self.assertEqual(self.s.execute(select(Contest.title).where(Contest.slug == "olymp")).scalar_one(), "Олимпиада")
        shown = json.loads(self.ok("contest", "show", "olymp", "--json"))
        self.assertEqual((shown["state"], shown["spec"]["timing_mode"], shown["spec"]["personal_duration"]),
                         ("draft", "personal", "1h"))  # the shape of contest.yaml (same as the admin API)
        self.assertEqual(len(shown["invite_code"]), 6)
        self.assertIn(f"invite code {shown['invite_code']}", self.ok("contest", "show", "olymp"))
        self.assertIn("published", self.ok("contest", "publish", "olymp"))
        self.assertIn("olymp", self.ok("contest", "list"))
        with self.assertRaises(SystemExit):  # --rejudge or --no-rejudge is a required choice
            self.run_cli("contest", "pin", "olymp", "A", "1")
        self.assertIn("v1 -> v1; no rejudge", self.ok("contest", "pin", "olymp", "A", "1", "--no-rejudge"))
        new = json.loads(self.ok("contest", "code", "regenerate", "olymp", "--json"))["invite_code"]
        self.assertNotEqual(new, shown["invite_code"])
        self.ok("contest", "archive", "olymp")
        self.assertNotIn("olymp", self.ok("contest", "list"))
        code, _, err = self.run_cli("contest", "apply", "-", stdin=b"format_version: 1\nslug: x\n")
        self.assertEqual(code, 1)
        self.assertIn("schema", err)

    # ---------------------------------------------------------------- submission, aliases
    def test_submission_show_and_every_kind_of_rejudge(self):
        self.ok("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.finish_checks()  # a worker's check makes v1 ready
        self.ok("contest", "apply", "-", stdin=CONTEST.replace("timing_mode: personal\npersonal_duration: 1h\n", "")
                .encode())
        c = self.s.execute(select(Contest).where(Contest.slug == "olymp")).scalar_one()
        cp = c.problems[0]
        pupil = self.student(1)
        sub = Submission(user_id=pupil.id, contest_problem_id=cp.id, language_key="python", source_sha256="c" * 64)
        self.s.add(sub)
        self.s.commit()
        self.s.execute(text("UPDATE judgements SET status = 'finished', verdict = 'WA', finished_at = now()"))
        from app import queue as q
        q.enqueue(self.s, sub)
        self.s.execute(text("UPDATE judgements SET status = 'finished', verdict = 'WA', finished_at = now()"))
        self.s.commit()
        listing = json.loads(self.ok("queue", "judgements", "--json"))  # scores are Decimal: crashed before 4a.2
        self.assertEqual(listing[0]["score"], "0.00")
        self.assertEqual(json.loads(self.ok("submission", "show", str(sub.id), "--json"))["judgements"][0]["score"], "0.00")
        shown = self.ok("submission", "show", str(sub.id))
        self.assertIn("student1, olymp A (sum-of-two)", shown)
        self.assertIn("WA", shown)
        self.assertIn("created", self.ok("submission", "rejudge", "--id", str(sub.id)))
        self.s.execute(text("UPDATE judgements SET status = 'finished', finished_at = now() WHERE status = 'queued'"))
        self.s.commit()
        self.assertIn("1 rejudges queued", self.ok("submission", "rejudge", "--contest", "olymp", "--problem", "A"))
        self.s.execute(text("UPDATE judgements SET status = 'finished', finished_at = now() WHERE status = 'queued'"))
        self.s.commit()
        data = json.loads(self.ok("submission", "rejudge", "--problem-version", "sum-of-two@1", "--json"))
        self.assertEqual((data["contest_problems"], data["created"]), ([cp.id], 1))
        self.assertEqual(self.run_cli("submission", "rejudge", "--id", str(sub.id), "--verdicts", "WA")[0], 1)
        self.assertEqual(self.run_cli("submission", "rejudge")[0], 1)
        self.assertEqual(self.run_cli("submission", "rejudge", "--problem-version", "sum-of-two@7")[0], 1)
        # the old spellings still work, with a warning
        self.s.execute(text("UPDATE judgements SET status = 'finished', finished_at = now() WHERE status = 'queued'"))
        self.s.commit()
        code, out, err = self.run_cli("queue", "rejudge", "--submission", str(sub.id))
        self.assertEqual(code, 0, err)
        self.assertIn(f"judgectl submission rejudge --id {sub.id}", err)
        self.s.execute(text("UPDATE judgements SET status = 'finished', finished_at = now() WHERE status = 'queued'"))
        self.s.commit()
        code, out, err = self.run_cli("rejudge", "--contest-problem", str(cp.id), alias=True)
        self.assertEqual(code, 0, err)
        self.assertIn(f"judgectl submission rejudge --contest-problem-id {cp.id}", err)
        self.assertEqual(self.s.execute(select(func.count()).select_from(Judgement)).scalar_one(), 6)

    # ---------------------------------------------------------------- status
    def test_status_reports_migrations_config_and_blobs(self):
        self.ok("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.finish_checks()  # a worker's check makes v1 ready
        code, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["database"], data["migration"]), (1, "ok", None))  # create_all: no alembic
        self.s.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
        self.s.execute(text("INSERT INTO alembic_version VALUES (:h)"), {"h": migration_head()})
        self.s.commit()
        before = json.loads(self.run_cli("status", "--json")[1])["blobs"]  # the seed has rows without files
        stray = self.blob_dir / "ab" / ("ab" + "0" * 62)
        stray.parent.mkdir(exist_ok=True)
        stray.write_bytes(b"left by an import that did not commit")
        files = self.s.execute(select(ProblemVersion.files).where(ProblemVersion.files.is_not(None))).scalar_one()
        some = files["statement/ru.md"]
        (self.blob_dir / some[:2] / some).unlink()
        self.s.add(Blob(sha256="f" * 64, size_bytes=1))  # a row nothing refers to
        self.s.commit()
        code, out, _ = self.run_cli("status", "--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["config_problems"], [])
        b = data["blobs"]
        self.assertEqual((b["files_without_row"], b["rows_without_file"] - before["rows_without_file"],
                          b["rows_unreferenced"] - before["rows_unreferenced"]), (1, 2, 1))
        sub = Submission(user_id=self.student(1).id, contest_problem_id=self.seed["cp"].id, language_key="python",
                         source_sha256="c" * 64)  # the seed's blob c...c has no file either
        self.s.add(sub)
        self.s.commit()
        details = {m["sha256"]: m["referenced_by"]
                   for m in json.loads(self.run_cli("status", "--details", "--json")[1])["blobs"]["missing_files"]}
        self.assertEqual(details[some], ["sum-of-two@1 file statement/ru.md"])
        self.assertEqual(details["f" * 64], [])
        self.assertIn("sum@1 test main/t0 (input)", details["a" * 64])
        self.assertIn(f"submission {sub.id} (student1, c A)", details["c" * 64])
        shown = self.run_cli("status", "--details")[1]
        self.assertIn(f"{'f' * 12}  nothing refers to it", shown)
        self.assertIn(f"{some[:12]}  sum-of-two@1 file statement/ru.md", shown)
        self.assertIn("judgectl status --details", self.run_cli("status")[1])  # the hint without --details
        text_out = self.run_cli("status", origin=None)[1]
        self.assertIn("files without a row     1", text_out)
        self.assertIn("JUDGE_PUBLIC_ORIGIN is not set", text_out)
        self.s.execute(text("UPDATE alembic_version SET version_num = 'c13b55a3e4d5'"))
        self.s.commit()
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("run the migrations", out)

    # ---------------------------------------------------------------- checks of problem versions (4a.4)
    FAILED_REPORT = {
        "outcome": "failed", "reason": "solution_mismatch", "worker": "judge-w0", "toolchain_id": "t", "duration_ms": 4200,
        "programs": [],
        "solutions": [{"name": "solutions/ok.py", "language": "python", "expected": "OK", "got": "OK", "ok": True,
                       "score": 100.0, "first_fail": None, "message": "", "error": None},
                      {"name": "solutions/wa_int_overflow.cpp", "language": "cpp", "expected": "WA", "got": "OK",
                       "ok": False, "score": 100.0, "first_fail": None, "message": "", "error": None}]}

    def test_checks_recheck_report_refusals_and_warnings(self):
        self.ok("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.finish_checks()  # v1 ready
        copy = self.tmp / "sum-of-two"
        shutil.copytree(EXAMPLES / "sum-of-two", copy)
        (copy / "statement" / "ru.md").write_text("Версия 2.\n", encoding="utf-8")
        self.ok("problem", "import", str(copy))  # v2 pending_check

        # apply without a version: the newest ready one, with a warning about v2
        code, out, err = self.run_cli("contest", "apply", "-", stdin=CONTEST.encode())
        self.assertEqual(code, 0, err)
        self.assertIn("A (sum-of-two): added, v1", out)
        self.assertIn("warning: A: sum-of-two v2 is pending_check (report: judgectl problem show sum-of-two@2", err)
        # pin a version still being checked: refused, with the status and where the report is
        code, _, err = self.run_cli("contest", "pin", "olymp", "A", "2", "--no-rejudge")
        self.assertEqual(code, 1)
        self.assertIn("sum-of-two v2 is pending_check: its check is not finished yet", err)
        self.assertIn("judgectl problem show sum-of-two@2", err)
        shown = self.ok("problem", "show", "sum-of-two@2")
        self.assertRegex(shown, r"check #\d+ \(import\): queued")

        # v2 fails its check: the report for people, a refusal to pin it
        self.finish_checks(report=self.FAILED_REPORT)
        shown = self.ok("problem", "show", "sum-of-two@2")
        self.assertIn("sum-of-two v2  failed  batch", shown)
        self.assertIn("check report: failed (solution_mismatch), by judge-w0, 4.2 s", shown)
        self.assertRegex(shown, r"FAIL\s+solutions/wa_int_overflow.cpp\s+WA\s+OK")
        code, _, err = self.run_cli("contest", "pin", "olymp", "A", "2", "--no-rejudge")
        self.assertIn("sum-of-two v2 is failed: its check did not pass", err)

        # a recheck of v1 (pinned): queued once, the status stays until it ends
        data = json.loads(self.ok("problem", "recheck", "sum-of-two@1", "--json"))
        self.assertEqual((data["result"], data["status"]), ("created", "ready"))
        again = self.ok("problem", "recheck", "sum-of-two@1")
        self.assertIn(f"check #{data['check_id']} already queued", again)
        self.assertIn("sum-of-two v1  ready", self.ok("problem", "show", "sum-of-two@1"))
        self.assertEqual(self.finish_checks("failed", "checker_compile_failed"), 1)

        # v1 now failed while pinned: contest show and status warn; submissions are still accepted (D-054)
        shown = self.ok("contest", "show", "olymp")
        self.assertIn("warning: A: sum-of-two v1 is pinned but failed its check (checker_compile_failed)", shown)
        detail = json.loads(self.ok("contest", "show", "olymp", "--json"))
        self.assertIn("pinned but failed", detail["rows"][0]["warning"])
        status = self.run_cli("status")[1]  # exit 1 here: create_all leaves no alembic_version
        self.assertRegex(status, r"checks\s+0 queued, 0 running; 0 versions pending_check")
        self.assertIn("failed      sum-of-two@1", status)
        self.assertIn("warning     contest olymp A: sum-of-two v1 is pinned but failed", status)
        snap = json.loads(self.run_cli("status", "--json")[1])["checks"]
        self.assertEqual([(r["contest"], r["label"], r["problem"], r["version"]) for r in snap["pinned_failed"]],
                         [("olymp", "A", "sum-of-two", 1)])

        # no ready version at all: apply names every status
        changed = CONTEST.replace("slug: olymp", "slug: second")
        code, _, err = self.run_cli("contest", "apply", "-", stdin=changed.encode())
        self.assertEqual(code, 1)
        self.assertIn("sum-of-two has no ready version (v1 failed, v2 failed", err)

        listing = self.ok("problem", "checks", "--last", "5")
        self.assertRegex(listing, r"sum-of-two@1\s+recheck\s+failed\s+checker_compile_failed")
        self.assertRegex(listing, r"sum-of-two@2\s+import\s+failed\s+solution_mismatch")
        rows = json.loads(self.ok("problem", "checks", "--json"))
        self.assertEqual(len(rows), 3)
        self.ok("problem", "archive", "sum-of-two")
        code, _, err = self.run_cli("problem", "recheck", "sum-of-two@2")
        self.assertEqual(code, 1)
        self.assertIn("unarchive it first", err)
        actions = [a for a in self.s.execute(select(AuditLog.action)).scalars() if a == "problem.recheck"]
        self.assertEqual(actions, ["problem.recheck"], "audited once: the second request created nothing")

    # ---------------------------------------------------------------- dev_seed --contest
    def test_dev_seed_submits_through_the_access_rules(self):
        self.ok("problem", "import", str(EXAMPLES / "sum-of-two"))
        self.finish_checks()  # a worker's check makes v1 ready
        self.ok("contest", "apply", "-", stdin=CONTEST.replace("2026-11-10 18:00", "2099-01-01 00:00")  # dev_seed runs now
                .replace("2026-11-10 09:00", "2026-01-01 00:00").encode())

        def seed(*argv):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"JUDGE_DEV": "1", "JUDGE_BLOB_DIR": str(self.blob_dir)}), \
                    mock.patch.object(db, "get_sessionmaker", lambda: self.Session), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = dev_seed.main(list(argv))
            return code, out.getvalue(), err.getvalue()

        code, _, err = seed("--contest", "olymp", "--label", "A")
        self.assertEqual(code, 1)
        self.assertIn("not_published", err)  # a draft: nobody can join it
        self.ok("contest", "publish", "olymp")
        code, out, err = seed("--contest", "olymp", "--label", "A", "--count", "2")
        self.assertEqual(code, 1)  # the second one is refused by the 600 s cooldown
        self.assertIn("dev-student-1 entered olymp (code)", err)
        self.assertIn("pressed start", err)
        self.assertIn("refused by the access rules: cooldown", err)
        made = json.loads(out)
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0]["counted"])
        part = self.s.execute(select(Participation)).scalar_one()
        self.assertIsNotNone(part.started_at)  # the personal start went through service.start
        self.assertEqual(self.s.execute(select(func.count()).select_from(Submission)).scalar_one(), 1)
        with self.assertRaises(SystemExit):
            seed("--contest", "olymp")  # --label is required with --contest
        self.assertEqual(self.s.execute(select(func.count()).select_from(ProblemVersion)).scalar_one(), 2)
