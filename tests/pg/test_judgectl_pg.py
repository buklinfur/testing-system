"""judgectl end to end on a real PostgreSQL: main(argv) with a fake terminal (getpass, input, TTY, clock)."""
import io
import json
import os
import re
import stat
import tempfile
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from cryptography.fernet import Fernet
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import admin_cli
    from app import queue as q
    from app.auth import audit, ratelimit, totp
    from app.auth.passwords import verify_password
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.auth import sessions
    from app.models import (AuditLog, AuthFailure, AuthFailureKind, AuthLockout, Judgement, LoginToken, Participation,
                            Role, SessionState, User, UserSession, VersionCheck, WorkerToken)
    from app.settings import AuthSettings, Settings

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
ADMIN_PW = "correct-horse-battery"
ORIGIN = "https://judge.example.ru"


@requires_pg
class JudgectlTests(PgCase):
    def setUp(self):
        super().setUp()
        self.now = T0
        self.key = Fernet.generate_key().decode()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # ---------------------------------------------------------------- helpers
    def run_cli(self, *argv, passwords=(), codes=None, tty=True, key="default", alias=False, tz="UTC"):
        """(exit code, stdout, stderr). `codes`: what is typed as 2FA codes; None = the right one each time."""
        out, err = io.StringIO(), io.StringIO()
        typed = iter(passwords)
        self.prompts = []

        def getpass(prompt):
            self.prompts.append(prompt)
            return next(typed)

        given = iter(codes) if codes is not None else None

        def code_input(prompt):
            if given is not None:
                return next(given)
            secret = re.findall(r"secret=([A-Z2-7]+)", out.getvalue())[-1]
            return totp.code_at_step(secret, totp.step_at(self.now))

        settings = Settings(public_origin=ORIGIN, timezone=tz,
                            auth=AuthSettings(secret_key=self.key if key == "default" else key))
        ctx = Context(session_factory=self.Session, settings=settings, actor=audit.Actor(cli="cli:judge", sudo_user="teacher"),
                      out=out, err=err, getpass=getpass, input=code_input, interactive=lambda: tty, clock=lambda: self.now)
        code = (admin_cli.main if alias else main)(list(argv), ctx)
        return code, out.getvalue(), err.getvalue()

    def user(self, login):
        self.s.expire_all()
        return self.s.execute(select(User).where(User.login == login)).scalar_one_or_none()

    def actions(self):
        self.s.expire_all()
        return self.s.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()

    def lock(self, login, admin=False):
        for _ in range(5 if admin else 10):
            ratelimit.record_failure(self.s, AuthSettings(), self.now, login=login, ip="10.0.0.1",
                                     kind=AuthFailureKind.password, admin=admin)
        self.s.commit()

    def add_admin(self, login="boss"):
        code, out, err = self.run_cli("admin", "add", login, "--name", "Иван Петрович", passwords=[ADMIN_PW, ADMIN_PW])
        self.assertEqual(code, 0, err)
        return out

    # ---------------------------------------------------------------- admin
    def test_admin_add_with_confirmed_2fa(self):
        out = self.add_admin()
        self.assertIn("otpauth://totp/judge:boss?", out)
        self.assertIn("2FA confirmed", out)
        self.assertTrue(set(out) & set("▀▄█"))  # the QR code, drawn with block characters
        boss = self.user("boss")
        self.assertEqual((boss.role, boss.display_name), (Role.admin, "Иван Петрович"))
        self.assertIsNotNone(boss.totp_enabled_at)
        self.assertTrue(verify_password(boss.password_hash, ADMIN_PW)[0])
        self.assertEqual(len(self.prompts), 2)  # typed twice, without echo
        rows = self.actions()
        self.assertEqual([a.action for a in rows], ["user.create", "user.enroll_2fa", "user.confirm_2fa"])
        self.assertTrue(all(a.actor_cli == "cli:judge" and a.details.get("sudo_user") == "teacher" for a in rows))
        everything = out + repr([(a.details, a.object_id) for a in rows])
        self.assertNotIn(ADMIN_PW, everything)  # the password is nowhere: output, audit
        code, listing, _ = self.run_cli("admin", "list", "--json")
        mine = {r["login"]: r for r in json.loads(listing)}
        self.assertEqual(mine["boss"]["two_factor"], "confirmed")
        self.assertEqual(mine["admin"]["two_factor"], "none")  # the seed admin never enrolled

    def test_admin_password_is_typed_twice_and_checked(self):
        code, _, err = self.run_cli("admin", "add", "boss", "--name", "B",
                                    passwords=["short", ADMIN_PW, "different-one-xx", ADMIN_PW, ADMIN_PW])
        self.assertEqual(code, 0, err)
        self.assertIn("at least 12", err)
        self.assertIn("differ", err)
        code, _, err = self.run_cli("admin", "add", "boss2", "--name", "B", passwords=["short"] * 3)
        self.assertEqual(code, 1)
        self.assertIn("nothing changed", err)
        self.assertIsNone(self.user("boss2"))

    def test_unconfirmed_2fa_is_shown_and_can_be_repeated(self):
        code, _, err = self.run_cli("admin", "add", "boss", "--name", "B", passwords=[ADMIN_PW] * 2,
                                    codes=["000000", "111111", "222222"])
        self.assertEqual(code, 1)
        self.assertIn("NOT confirmed", err)
        self.assertIn("judgectl admin reset-2fa boss", err)
        _, listing, _ = self.run_cli("admin", "list")
        self.assertRegex(listing, r"boss\s+B\s+yes\s+pending")
        self.assertIsNone(self.user("boss").totp_enabled_at)
        code, out, err = self.run_cli("admin", "reset-2fa", "boss")
        self.assertEqual(code, 0, err)
        _, listing, _ = self.run_cli("admin", "list")
        self.assertRegex(listing, r"boss\s+B\s+yes\s+confirmed")
        actions = [a.action for a in self.actions()]
        self.assertEqual((actions.count("user.enroll_2fa"), actions.count("user.confirm_2fa")), (2, 1))

    def test_interactive_commands_need_a_terminal(self):
        self.add_admin()
        for argv in (("admin", "add", "x.admin", "--name", "X"), ("admin", "reset-2fa", "boss"),
                     ("admin", "reset-password", "boss")):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv, tty=False)
                self.assertEqual(code, 1)
                self.assertIn("interactive terminal", err)
                self.assertEqual(self.prompts, [])

    def test_admin_add_needs_the_secret_key_before_asking_anything(self):
        for key in (None, "CHANGE_ME"):
            code, _, err = self.run_cli("admin", "add", "boss", "--name", "B", passwords=[ADMIN_PW] * 2, key=key)
            self.assertEqual(code, 1)
            self.assertIn("JUDGE_SECRET_KEY", err)
            self.assertEqual(self.prompts, [])
        self.assertIsNone(self.user("boss"))

    def test_admin_reset_password_disable_enable(self):
        self.add_admin()
        new = "a-brand-new-admin-pass"
        code, _, err = self.run_cli("admin", "reset-password", "boss", passwords=[new, new])
        self.assertEqual(code, 0, err)
        self.assertTrue(verify_password(self.user("boss").password_hash, new)[0])
        self.assertEqual(self.run_cli("admin", "disable", "boss")[0], 0)
        self.assertFalse(self.user("boss").is_active)
        self.assertEqual(self.run_cli("admin", "enable", "boss")[0], 0)
        self.assertTrue(self.user("boss").is_active)
        self.assertNotIn(new, repr([a.details for a in self.actions()]))

    def test_unlock_in_both_groups(self):
        self.add_admin()
        self.run_cli("user", "add", "--surname", "Иванов", "--first-name", "Пётр")
        self.lock("boss", admin=True)
        self.lock("ivanov.p")
        _, listing, _ = self.run_cli("admin", "list", "--json")
        boss = [r for r in json.loads(listing) if r["login"] == "boss"][0]
        self.assertIsNotNone(boss["locked_until"])
        code, _, err = self.run_cli("user", "unlock", "boss")
        self.assertEqual(code, 1)
        self.assertIn("judgectl admin unlock", err)
        code, _, err = self.run_cli("admin", "unlock", "ivanov.p")
        self.assertEqual(code, 1)
        self.assertIn("judgectl user", err)
        code, out, _ = self.run_cli("admin", "unlock", "boss")
        self.assertEqual(code, 0)
        self.assertIn("lockout lifted", out)
        self.assertIsNone(ratelimit.locked_until(self.s, self.now, login="boss"))
        self.assertIn("lockout lifted", self.run_cli("user", "unlock", "IVANOV.P")[1])
        self.lock("nobody.here")
        self.assertIn("lockout lifted", self.run_cli("user", "unlock", "nobody.here")[1])
        self.assertEqual(self.run_cli("user", "unlock")[0], 1)
        self.assertIn("was not locked", self.run_cli("user", "unlock", "--ip", "10.0.0.1")[1])

    # ---------------------------------------------------------------- user
    def test_user_add_list_reset_set_login_qr(self):
        code, out, err = self.run_cli("user", "add", "--surname", "Подъячев", "--first-name", "Чеслав", "--json")
        self.assertEqual(code, 0, err)
        created = json.loads(out)[0]
        self.assertEqual((created["login"], created["display_name"]), ("podyachev.ch", "Чеслав П."))
        self.assertTrue(verify_password(self.user("podyachev.ch").password_hash, created["password"])[0])
        code, out, _ = self.run_cli("user", "reset-password", "podyachev.ch", "--json")
        new = json.loads(out)[0]["password"]
        self.assertNotEqual(new, created["password"])
        self.assertTrue(verify_password(self.user("podyachev.ch").password_hash, new)[0])
        self.assertEqual(self.run_cli("user", "set-login", "podyachev.ch", "Chesl.P")[0], 0)
        code, out, _ = self.run_cli("user", "qr-token", "chesl.p", "--json")
        qr = json.loads(out)
        self.assertTrue(qr["url"].startswith(ORIGIN + "/login/qr#"))
        self.assertEqual(qr["url"].split("#")[1], qr["token"])
        self.assertEqual(self.s.execute(select(LoginToken)).scalar_one().user_id, self.user("chesl.p").id)
        self.assertEqual(self.run_cli("user", "disable", "chesl.p")[0], 0)
        self.assertNotIn("chesl.p", self.run_cli("user", "list")[1])
        self.assertIn("chesl.p", self.run_cli("user", "list", "--all")[1])
        code, _, err = self.run_cli("user", "reset-password", "admin")
        self.assertEqual(code, 1)
        self.assertIn("admin", err)

    def test_import_cp1251_class_into_a_private_file(self):
        names = [("Иванов", "Пётр")] * 3 + [(f"Ученик{chr(0x430 + i)}", "Анна") for i in range(26)] + [("Щукина", "Яна")]
        lines = ["Фамилия;Имя;Логин;Имя в рейтинге"] + [f"{s};{f};;" for s, f in names]
        src = Path(self.tmp.name, "7b.csv")
        src.write_bytes("\r\n".join(lines).encode("cp1251"))
        out_path = os.path.join(self.tmp.name, "7b-passwords.csv")
        code, out, err = self.run_cli("user", "import", str(src), "--out", out_path)
        self.assertEqual(code, 0, err)
        self.assertIn("30 students created (cp1251", out)
        self.assertEqual(stat.S_IMODE(os.stat(out_path).st_mode), 0o600)
        raw = Path(out_path).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = [r.split(";") for r in raw.decode("utf-8-sig").splitlines()]
        self.assertEqual(rows[0], ["фамилия", "имя", "логин", "пароль", "display_name"])
        self.assertEqual([r[2] for r in rows[1:4]], ["ivanov.p", "ivanov.p2", "ivanov.p3"])
        self.assertEqual((rows[-1][2], rows[-1][4]), ("shchukina.ya", "Яна Щ."))
        for _s, _f, login, password, _n in rows[1:]:
            self.assertTrue(verify_password(self.user(login).password_hash, password)[0])
        self.assertNotIn(rows[1][3], out)  # the passwords are only in the file
        entry = [a for a in self.actions() if a.action == "user.import"][0]
        self.assertEqual((entry.details["count"], entry.details["encoding"]), (30, "cp1251"))
        self.assertNotIn(rows[1][3], repr(entry.details))
        code, _, err = self.run_cli("user", "import", str(src), "--out", out_path)  # the file exists
        self.assertEqual(code, 1)
        self.assertIn("refusing to overwrite", err)
        self.assertEqual(Path(out_path).read_bytes(), raw)

    def test_import_is_all_or_nothing(self):
        self.run_cli("user", "add", "--surname", "Занят", "--first-name", "Логин", "--login", "taken.login")
        src = Path(self.tmp.name, "c.csv")
        src.write_text("Иванов;Пётр\nПетров;Иван;taken.login\nСидоров;Олег\nКозлов;Ян;Petrov.i\n", encoding="utf-8")
        out_path = os.path.join(self.tmp.name, "out.csv")
        code, out, err = self.run_cli("user", "import", str(src), "--out", out_path)
        self.assertEqual(code, 1)
        self.assertIn("line 2: login 'taken.login' is already taken", err)
        self.assertFalse(os.path.exists(out_path))  # removed again
        self.assertEqual([u.login for u in self.s.execute(select(User).where(User.role == Role.student)).scalars()],
                         ["taken.login"])
        src.write_text("Иванов;Пётр\nПетров\nСидоров;Олег;x\n", encoding="utf-8")
        code, _, err = self.run_cli("user", "import", str(src), "--out", out_path)
        self.assertEqual(code, 1)
        self.assertIn("line 2:", err)
        self.assertIn("line 3:", err)
        self.assertFalse(os.path.exists(out_path))  # format errors: the file is not even created

    def test_import_to_stdout(self):
        src = Path(self.tmp.name, "c.csv")
        src.write_text("Иванов,Пётр\n", encoding="utf-8-sig")
        code, out, _ = self.run_cli("user", "import", str(src), "--out", "-", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)[0]["login"], "ivanov.p")

    # ---------------------------------------------------------------- queue, worker, alias
    def test_worker_tokens_and_queue(self):
        path = os.path.join(self.tmp.name, "token")
        code, out, err = self.run_cli("worker", "token", "create", "main", "--caps", "cpu,gpu", "--out", path)
        self.assertEqual(code, 0, err)
        token = Path(path).read_text()
        self.assertRegex(token, r"^jw_[A-Za-z0-9_-]{20,}\n$")
        self.assertNotIn("jw_", out + err)  # D-089: only the file has it
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(self.s.execute(select(WorkerToken.capabilities)).scalar_one(), ["cpu", "gpu"])
        self.assertIsNotNone(q.authenticate(self.s, token.strip()))  # the token of the file is the one that works
        self.s.commit()  # authenticate marks last_used_at: end that transaction before judgectl revokes the token
        self.assertEqual(self.run_cli("worker", "token", "revoke", "main")[0], 0)
        self.assertEqual(self.run_cli("worker", "token", "revoke", "main")[0], 1)  # nothing live left
        self.assertEqual(self.run_cli("worker", "list")[1].strip(), "(none)")
        code, out, _ = self.run_cli("queue", "status", "--json")
        self.assertEqual(json.loads(out)["load"]["queue_length"], 0)
        self.assertEqual(self.run_cli("queue", "judgements", "--last", "5")[0], 0)
        self.assertEqual(self.run_cli("queue", "events")[0], 0)
        self.assertEqual(self.run_cli("queue", "rejudge")[0], 1)
        actions = [a.action for a in self.actions()]
        self.assertEqual(actions, ["worker.token_create", "worker.token_revoke"])

    def test_worker_token_only_into_a_new_file(self):
        """D-089: no --out, --out -, an existing file, a symlink - refused before a token exists; a failure after the
        file is made (the database, the commit) removes it."""
        def tokens():
            self.s.expire_all()
            return self.s.execute(select(WorkerToken.name)).scalars().all()

        for argv in ((), ("--out", "-")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli("worker", "token", "create", "w", *argv)
                self.assertEqual(code, 2)
                self.assertIn("--out", err)
                self.assertNotIn("jw_", out + err)
        code, out, err = self.run_cli("worker", "token", "create", "w", "--out", "-", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(tokens(), [])
        path = os.path.join(self.tmp.name, "token")
        self.assertEqual(self.run_cli("worker", "token", "create", "w", "--out", path)[0], 0)
        first = Path(path).read_bytes()
        for name in ("w2", "w"):  # another token into the same file; the same name too
            with self.subTest(again=name):
                code, out, err = self.run_cli("worker", "token", "create", name, "--out", path)
                self.assertEqual(code, 1)
                self.assertIn("already exists", err)
                self.assertEqual(Path(path).read_bytes(), first)  # byte for byte
        self.assertEqual(tokens(), ["w"])
        link = os.path.join(self.tmp.name, "link")
        os.symlink(os.path.join(self.tmp.name, "elsewhere"), link)
        self.assertEqual(self.run_cli("worker", "token", "create", "w3", "--out", link)[0], 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "elsewhere")))
        code, out, err = self.run_cli("worker", "token", "create", "w4", "--out", os.path.join(self.tmp.name, "x"),
                                      "--json")
        self.assertEqual(json.loads(out), {"name": "w4", "capabilities": ["cpu", "problem_check"],
                                           "out": os.path.join(self.tmp.name, "x")})
        # the name is taken: the database refuses after the file was made - the file goes too
        taken = os.path.join(self.tmp.name, "taken")
        code, out, err = self.run_cli("worker", "token", "create", "w", "--out", taken)
        self.assertEqual(code, 1)
        self.assertIn("already exists (revoked ones keep their name)", err)
        self.assertFalse(os.path.exists(taken))
        # the commit itself fails: the file goes, no token stays
        failing = os.path.join(self.tmp.name, "failing")
        with mock.patch.object(Session, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaises(RuntimeError):
                self.run_cli("worker", "token", "create", "w5", "--out", failing)
        self.assertFalse(os.path.exists(failing))
        self.assertNotIn("w5", tokens())
        self.assertEqual([a.action for a in self.actions()], ["worker.token_create", "worker.token_create"])

    def test_admin_cli_alias(self):
        path = os.path.join(self.tmp.name, "w1")
        code, out, err = self.run_cli("create-worker-token", "w1", "--out", path, alias=True)
        self.assertEqual(code, 0, err)
        self.assertTrue(Path(path).read_text().startswith("jw_"))
        self.assertNotIn("jw_", out + err)
        self.assertIn("deprecated", err)
        self.assertIn(f"judgectl worker token create w1 --out {path}", err)
        code, out, err = self.run_cli("status", alias=True)
        self.assertEqual(code, 0)
        self.assertIn("queue_length", out)
        self.assertIn("judgectl queue status && judgectl worker list", err)
        self.assertEqual(admin_cli.translate(["judgements", "--last", "3"]), [["queue", "judgements", "--last", "3"]])
        self.assertEqual(admin_cli.translate(["rejudge", "--submission", "4"]), [["submission", "rejudge", "--id", "4"]])
        self.assertEqual(admin_cli.translate(["rejudge", "--contest-problem", "5", "--verdicts", "WA"]),
                         [["submission", "rejudge", "--contest-problem-id", "5", "--verdicts", "WA"]])
        self.assertEqual(admin_cli.translate(["revoke-worker-token", "w1"]), [["worker", "token", "revoke", "w1"]])

    # ---------------------------------------------------------------- deletion, import rollback, failures (4a.2 stage 1)
    def test_delete_is_refused_while_anything_references_the_user(self):
        code, _, err = self.run_cli("admin", "delete", "admin", "--yes")  # the seed admin owns a problem and a contest
        self.assertEqual(code, 1)
        self.assertIn("problems owned: 1", err)
        self.assertIn("contests created: 1", err)
        self.assertIn("judgectl admin disable admin", err)
        traced = {
            "submissions": lambda u: self.submission(u),
            "contest participations": lambda u: self.s.add(Participation(contest_id=self.seed["cp"].contest_id,
                                                                        user_id=u.id, joined_via="open")),
            "rejudges requested": lambda u: self.s.execute(Judgement.__table__.update().values(requested_by=u.id)),
            "problem checks requested": lambda u: self.s.add(VersionCheck(
                problem_version_id=self.seed["cp"].problem_version_id, reason="recheck", status="done",
                outcome="ready", requested_by=u.id)),
            "audit records as actor": lambda u: audit.record(self.s, audit.Actor(user_id=u.id), "auth.login", T0),
        }
        for n, (label, make) in enumerate(traced.items(), start=1):
            with self.subTest(trace=label):
                if label == "rejudges requested":
                    self.submission(self.seed["admin"])  # a judgement to point at the student
                u = self.student(n)
                make(u)
                self.s.commit()
                code, _, err = self.run_cli("user", "delete", u.login, "--yes")
                self.assertEqual(code, 1)
                self.assertIn(label, err)
                self.assertIn("disable", err)
                self.assertIsNotNone(self.user(u.login))
        self.assertNotIn("user.delete", [a.action for a in self.actions()])

    def test_delete_a_clean_admin_with_confirmation(self):
        self.add_admin("oops")
        oops = self.user("oops")
        sessions.create(self.s, oops, SessionState.mfa_pending, AuthSettings(), T0)
        self.lock("oops", admin=True)
        self.assertEqual(self.run_cli("user", "delete", "oops", "--yes")[0], 1)  # wrong group
        code, _, err = self.run_cli("admin", "delete", "oops", tty=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        code, _, err = self.run_cli("admin", "delete", "oops", codes=["boss"])
        self.assertEqual(code, 1)
        self.assertIn("not confirmed", err)
        self.assertIsNotNone(self.user("oops"))
        code, out, err = self.run_cli("admin", "delete", "oops", codes=["oops"])
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.user("oops"))
        self.assertEqual(self.s.execute(select(UserSession)).first(), None)
        self.assertEqual(self.s.execute(select(AuthFailure).where(AuthFailure.login == "oops")).first(), None)
        self.assertEqual(self.s.execute(select(AuthLockout).where(AuthLockout.login == "oops")).first(), None)
        last = self.actions()[-1]
        self.assertEqual((last.action, last.details["login"], last.details["display_name"], last.details["role"]),
                         ("user.delete", "oops", "Иван Петрович", "admin"))
        self.assertEqual(last.details["sessions_deleted"], 1)
        self.add_admin("oops")  # the login is free again

    def write_csv(self, name, text):
        path = Path(self.tmp.name) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_import_rollback(self):
        src = self.write_csv("a.csv", "Иванов;Пётр\nПетрова;Анна\nСидоров;Олег\n")
        code, out, err = self.run_cli("user", "import", src, "--out", "-", "--json")
        self.assertEqual(code, 0, err)
        import_id = int(re.search(r"import id (\d+)", err).group(1))
        self.assertIn(f"judgectl user import-rollback {import_id}", err)
        listing = json.loads(self.run_cli("user", "imports", "--json")[1])
        self.assertEqual([(r["id"], r["count"], r["rolled_back"]) for r in listing], [(import_id, 3, False)])
        self.assertEqual(self.run_cli("user", "import-rollback", str(import_id), tty=False)[0], 1)  # needs --yes
        code, out, err = self.run_cli("user", "import-rollback", str(import_id), codes=[str(import_id)])
        self.assertEqual(code, 0, err)
        self.assertIn("3 users deleted", out)
        self.assertIsNone(self.user("ivanov.p"))
        self.assertTrue(json.loads(self.run_cli("user", "imports", "--json")[1])[0]["rolled_back"])
        code, _, err = self.run_cli("user", "import-rollback", str(import_id), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("already rolled back", err)
        # somebody has logged in: refused, nothing deleted
        code, _, err = self.run_cli("user", "import", src, "--out", "-")
        second = int(re.search(r"import id (\d+)", err).group(1))
        self.user("petrova.a").last_login_at = T0
        self.s.commit()
        code, _, err = self.run_cli("user", "import-rollback", str(second), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("have logged in: petrova.a", err)
        self.assertIsNotNone(self.user("ivanov.p"))
        self.assertEqual(self.run_cli("user", "import-rollback", "999", "--yes")[0], 1)

    def test_import_rollback_of_an_import_from_session_4a1(self):
        """Imports of session 4a.1 recorded only logins, no user ids."""
        u = self.student(7)
        row = audit.record(self.s, audit.Actor(cli="cli:judge"), "user.import", T0, object_type="file",
                           object_id="old.csv", details={"count": 1, "logins": [u.login]})
        self.s.commit()
        code, out, err = self.run_cli("user", "import-rollback", str(row.id), "--yes")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.user("student7"))

    def test_failures_and_counters(self):
        self.add_admin("boss")
        settings = AuthSettings()
        for i in range(3):
            ratelimit.record_failure(self.s, settings, self.now - timedelta(minutes=i), login="ghost", ip=f"10.0.0.{i}",
                                     kind=AuthFailureKind.password)
        self.student(1)
        ratelimit.record_failure(self.s, settings, self.now, login="student1", ip="10.0.0.9", kind=AuthFailureKind.password)
        ratelimit.record_failure(self.s, settings, self.now - timedelta(hours=2), login="student1", ip="10.0.0.9",
                                 kind=AuthFailureKind.password)  # outside the window: listed, not counted
        for _ in range(2):
            ratelimit.record_failure(self.s, settings, self.now, login="boss", ip="10.0.0.5", kind=AuthFailureKind.totp,
                                     admin=True)
        self.s.commit()
        data = json.loads(self.run_cli("user", "failures", "ghost", "--json")[1])  # an unknown login
        self.assertEqual((data["counting"], data["limit"], len(data["failures"])), (3, 10, 3))
        self.assertEqual(data["failures"][0]["ip"], "10.0.0.0")  # newest first
        self.assertTrue(data["failures"][0]["at"].endswith("Z"))
        code, out, _ = self.run_cli("user", "failures", "student1")
        self.assertIn("1 of 10 failures towards a lockout", out)
        self.assertEqual(out.count("password"), 2)
        students = {r["login"]: r for r in json.loads(self.run_cli("user", "list", "--json")[1])}
        self.assertEqual(students["student1"]["failures"], 1)
        code, out, _ = self.run_cli("admin", "failures", "boss")
        self.assertIn("2 of 5 failures", out)
        self.assertIn("totp", out)
        self.assertEqual(self.run_cli("user", "failures", "boss")[0], 1)  # admins only in the admin group
        self.assertEqual(self.run_cli("admin", "failures", "student1")[0], 1)
        admins = {r["login"]: r for r in json.loads(self.run_cli("admin", "list", "--json")[1])}
        self.assertEqual(admins["boss"]["failures"], 2)
        self.run_cli("admin", "disable", "boss")
        self.assertNotIn("boss", self.run_cli("admin", "list")[1])
        self.assertIn("boss", self.run_cli("admin", "list", "--all")[1])

    def test_tables_use_judge_timezone_and_json_is_utc(self):
        self.student(1)
        self.s.commit()
        code, out, _ = self.run_cli("user", "qr-token", "student1", "--no-qr", tz="Asia/Novosibirsk")
        self.assertIn("valid until 2026-11-03 16:00 (Asia/Novosibirsk)", out)  # 09:00 UTC + 7 days, UTC+7
        data = json.loads(self.run_cli("user", "qr-token", "student1", "--json", tz="Asia/Novosibirsk")[1])
        self.assertEqual(data["expires_at"], "2026-11-03T09:00:00Z")
        code, _, err = self.run_cli("user", "list", tz="Mars/Olympus")
        self.assertEqual(code, 1)
        self.assertIn("JUDGE_TIMEZONE", err)
