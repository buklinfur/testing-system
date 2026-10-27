"""judgectl end to end on a real PostgreSQL: main(argv) with a fake terminal (getpass, input, TTY, clock)."""
import io
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from cryptography.fernet import Fernet
    from sqlalchemy import select

    from app import admin_cli
    from app.auth import audit, ratelimit, totp
    from app.auth.passwords import verify_password
    from app.judgectl.cli import main
    from app.judgectl.core import Context
    from app.models import AuditLog, AuthFailureKind, LoginToken, Role, User, WorkerToken
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
    def run_cli(self, *argv, passwords=(), codes=None, tty=True, key="default", alias=False):
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

        settings = Settings(public_origin=ORIGIN, auth=AuthSettings(secret_key=self.key if key == "default" else key))
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
        code, out, _ = self.run_cli("worker", "token", "create", "main", "--caps", "cpu,gpu")
        self.assertEqual(code, 0)
        token = out.strip().splitlines()[-1]  # deploy scripts rely on `| tail -1`
        self.assertTrue(token.startswith("jw_"))
        self.assertEqual(self.s.execute(select(WorkerToken.capabilities)).scalar_one(), ["cpu", "gpu"])
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

    def test_admin_cli_alias(self):
        code, out, err = self.run_cli("create-worker-token", "w1", alias=True)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().splitlines()[-1].startswith("jw_"))
        self.assertIn("deprecated", err)
        self.assertIn("judgectl worker token create w1", err)
        code, out, err = self.run_cli("status", alias=True)
        self.assertEqual(code, 0)
        self.assertIn("queue_length", out)
        self.assertIn("judgectl queue status && judgectl worker list", err)
        self.assertEqual(admin_cli.translate(["judgements", "--last", "3"]), [["queue", "judgements", "--last", "3"]])
        self.assertEqual(admin_cli.translate(["rejudge", "--submission", "4"]), [["queue", "rejudge", "--submission", "4"]])
        self.assertEqual(admin_cli.translate(["revoke-worker-token", "w1"]), [["worker", "token", "revoke", "w1"]])
