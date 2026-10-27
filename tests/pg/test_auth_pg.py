"""app.auth on a real PostgreSQL: sessions, brute-force limits, TOTP login, QR tokens, account management."""
from datetime import datetime, timedelta, timezone

from tests.pg.pgbase import HAVE_SA, URL, PgCase, requires_pg

if URL and HAVE_SA:
    from argon2 import PasswordHasher
    from cryptography.fernet import Fernet
    from sqlalchemy import func, select

    from app.auth import audit, cleanup, flows, ratelimit, sessions, totp, users
    from app.auth.flows import Outcome
    from app.models import AuditLog, AuthFailure, LoginToken, Role, SessionState
    from app.settings import AuthSettings

T0 = datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)
IP = "10.1.2.3"


@requires_pg
class AuthServiceTests(PgCase):
    def setUp(self):
        super().setUp()
        self.settings = AuthSettings(ip_fail_limit=1000)
        self.box = totp.SecretBox(Fernet.generate_key().decode())
        self.cli = audit.Actor(cli="cli:tester", sudo_user="alice")
        self.now = T0

    # ---------------------------------------------------------------- helpers
    def tick(self, **kw):
        self.now = self.now + timedelta(**kw)
        return self.now

    def make_student(self, surname="Иванов", first="Пётр", **kw):
        user, password = users.create_student(self.s, self.cli, self.now, surname=surname, first_name=first, **kw)
        self.s.commit()
        return user, password

    def make_admin(self, login="boss", password="correct-horse-battery"):
        user = users.create_admin(self.s, self.cli, self.now, login=login, display_name="Boss", password=password)
        secret = users.enroll_totp(self.s, self.cli, user, self.box, self.now)
        code = totp.code_at_step(secret, totp.step_at(self.now))
        self.assertTrue(users.confirm_totp(self.s, self.cli, user, self.box, code, self.now))
        self.s.commit()
        self.tick(seconds=31)  # the enrolment code's step is used up
        return user, password, secret

    def login(self, login, password, ip=IP):
        r = flows.login_password(self.s, self.settings, self.now, login=login, password=password, ip=ip, user_agent="UA")
        self.s.commit()
        return r

    def code(self, secret, delta=0):
        return totp.code_at_step(secret, totp.step_at(self.now) + delta)

    def find(self, token):
        return sessions.find(self.s, token, self.now)

    # ---------------------------------------------------------------- password login
    def test_student_login_creates_an_active_sliding_session(self):
        user, password = self.make_student()
        self.assertEqual(user.login, "ivanov.p")
        r = self.login(" IVANOV.P ", password)
        self.assertEqual(r.outcome, Outcome.ok)
        row, found = self.find(r.token)
        self.assertEqual((found.id, row.state), (user.id, SessionState.active))
        self.assertEqual(row.token_hash, sessions.hash_token(r.token))
        self.assertNotEqual(row.token_hash, r.token)
        self.assertIsNone(row.absolute_expires_at)
        self.assertEqual(row.expires_at, T0 + timedelta(days=30))
        self.assertEqual((found.last_login_at, found.last_login_ip), (T0, IP))
        self.tick(days=29)
        sessions.touch(row, found, self.settings, self.now)
        self.s.commit()
        self.tick(days=29)  # 58 days after login, 29 after the last activity
        self.assertIsNotNone(self.find(r.token))
        self.tick(days=2)
        self.assertIsNone(self.find(r.token))

    def test_unknown_login_and_wrong_password_look_the_same(self):
        _user, password = self.make_student()
        a = self.login("ivanov.p", password + "x")
        b = self.login("nobody.here", password)
        c = self.login("??", password)  # not even a valid login
        self.assertEqual([a.outcome, b.outcome, c.outcome], [Outcome.invalid] * 3)
        self.assertTrue(all(r.token is None and r.user is None for r in (a, b, c)))
        logged = self.s.execute(select(AuthFailure.login).order_by(AuthFailure.id)).scalars().all()
        self.assertEqual(logged, ["ivanov.p", "nobody.here", "??"])

    def test_rehash_on_login_when_parameters_changed(self):
        user, password = self.make_student()
        user.password_hash = PasswordHasher(time_cost=1, memory_cost=8192).hash(password)
        self.s.commit()
        old = user.password_hash
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)
        self.s.refresh(user)
        self.assertNotEqual(user.password_hash, old)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)

    def test_disabled_user_cannot_log_in_and_loses_sessions(self):
        user, password = self.make_student()
        r = self.login("ivanov.p", password)
        users.set_active(self.s, self.cli, user, False, self.now)
        self.s.commit()
        self.assertIsNone(self.find(r.token))
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.invalid)
        users.set_active(self.s, self.cli, user, True, self.now)
        self.s.commit()
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)

    # ---------------------------------------------------------------- brute force
    def test_login_lockout_and_unlock(self):
        _user, password = self.make_student()
        for _ in range(9):
            self.assertEqual(self.login("ivanov.p", "wrong").outcome, Outcome.invalid)
            self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", "wrong").outcome, Outcome.invalid)  # the 10th locks
        self.tick(seconds=1)
        r = self.login("ivanov.p", password)
        self.assertEqual(r.outcome, Outcome.locked)
        self.assertEqual(r.locked_until, T0 + timedelta(seconds=9 + 900))
        self.assertEqual(self.login("other.x", "wrong").outcome, Outcome.invalid)  # other logins are not affected
        self.assertTrue(users.unlock(self.s, self.cli, "IVANOV.P", self.now))
        self.s.commit()
        self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)
        # the counter was reset: 9 more failures do not lock
        for _ in range(9):
            self.tick(seconds=1)
            self.login("ivanov.p", "wrong")
        self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)
        self.assertEqual(self.s.execute(select(func.count()).select_from(AuthFailure)).scalar_one(), 20)  # history kept (incl. other.x)

    def test_lockout_expires_and_does_not_relock_from_old_failures(self):
        _user, password = self.make_student()
        for _ in range(10):
            self.login("ivanov.p", "wrong")
        self.tick(seconds=899)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.locked)
        self.tick(seconds=2)
        self.assertEqual(self.login("ivanov.p", "wrong").outcome, Outcome.invalid)  # 1 failure after the lockout
        self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)

    def test_failures_outside_the_window_do_not_count(self):
        _user, password = self.make_student()
        for _ in range(9):
            self.login("ivanov.p", "wrong")
        self.tick(seconds=901)
        self.login("ivanov.p", "wrong")
        self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.ok)

    def test_admin_limit_is_stricter(self):
        _admin, password, _secret = self.make_admin()
        for _ in range(5):
            self.assertEqual(self.login("boss", "wrong").outcome, Outcome.invalid)
        self.assertEqual(self.login("boss", password).outcome, Outcome.locked)

    def test_ip_limit(self):
        self.settings = AuthSettings(ip_fail_limit=3)
        _user, password = self.make_student()
        for login in ("a.aa", "b.bb", "c.cc"):
            self.login(login, "wrong", ip="192.0.2.7")
        self.assertEqual(self.login("ivanov.p", password, ip="192.0.2.7").outcome, Outcome.locked)
        self.assertEqual(self.login("ivanov.p", password, ip="192.0.2.8").outcome, Outcome.ok)
        self.assertTrue(ratelimit.unlock(self.s, self.now, "cli:tester", ip="192.0.2.7"))
        self.assertEqual(self.login("ivanov.p", password, ip="192.0.2.7").outcome, Outcome.ok)

    # ---------------------------------------------------------------- admin: TOTP
    def totp_step(self, row, code, ip=IP):
        r = flows.login_totp(self.s, self.settings, self.box, self.now, row=row, code=code, ip=ip)
        self.s.commit()
        return r

    def test_admin_login_needs_the_second_factor(self):
        admin, password, secret = self.make_admin()
        r = self.login("boss", password)
        self.assertEqual(r.outcome, Outcome.ok)
        row, _ = self.find(r.token)
        self.assertEqual(row.state, SessionState.mfa_pending)
        self.assertEqual(row.expires_at, self.now + timedelta(minutes=5))
        self.assertIsNone(admin.last_login_at)  # not logged in yet
        self.assertEqual(self.totp_step(row, "000000").outcome, Outcome.invalid)
        good = self.totp_step(row, self.code(secret))
        self.assertEqual(good.outcome, Outcome.ok)
        self.assertNotEqual(good.token, r.token)
        self.assertIsNone(self.find(r.token))  # the pre-2FA token is dead
        row2, user = self.find(good.token)
        self.assertEqual((row2.id, row2.state), (row.id, SessionState.active))
        self.assertEqual(row2.absolute_expires_at, self.now + timedelta(days=7))
        self.assertEqual(user.last_login_ip, IP)
        kinds = self.s.execute(select(AuthFailure.kind)).scalars().all()
        self.assertEqual([k.value for k in kinds], ["totp"])
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "auth.login")).scalar_one()
        self.assertEqual((entry.actor_user_id, entry.ip), (admin.id, IP))

    def test_totp_code_works_once(self):
        _admin, password, secret = self.make_admin()
        code = self.code(secret)
        first = self.find(self.login("boss", password).token)[0]
        self.assertEqual(self.totp_step(first, code).outcome, Outcome.ok)
        second = self.find(self.login("boss", password).token)[0]
        self.assertEqual(self.totp_step(second, code).outcome, Outcome.invalid)
        self.tick(seconds=30)
        self.assertEqual(self.totp_step(second, self.code(secret)).outcome, Outcome.ok)

    def test_totp_failures_lock_the_login(self):
        _admin, password, _secret = self.make_admin()
        row = self.find(self.login("boss", password).token)[0]
        for _ in range(5):
            self.assertEqual(self.totp_step(row, "000000").outcome, Outcome.invalid)
        self.assertEqual(self.totp_step(row, "000000").outcome, Outcome.locked)

    def test_mfa_pending_expires(self):
        _admin, password, secret = self.make_admin()
        row = self.find(self.login("boss", password).token)[0]
        self.tick(minutes=5, seconds=1)
        self.assertEqual(self.totp_step(row, self.code(secret)).outcome, Outcome.invalid)

    def test_admin_session_has_an_absolute_limit(self):
        _admin, password, secret = self.make_admin()
        row = self.find(self.login("boss", password).token)[0]
        token = self.totp_step(row, self.code(secret)).token
        for _ in range(16):  # active every 11 h: the idle timeout alone would never expire it
            self.tick(hours=11)
            found = self.find(token)
            if found is None:
                break
            sessions.touch(found[0], found[1], self.settings, self.now)
            self.s.commit()
        self.assertIsNone(self.find(token))
        self.assertLessEqual(self.now - T0, timedelta(days=7, hours=12))

    def test_admin_without_confirmed_2fa_cannot_log_in(self):
        admin = users.create_admin(self.s, self.cli, self.now, login="newboss", display_name="N",
                                   password="correct-horse-battery")
        users.enroll_totp(self.s, self.cli, admin, self.box, self.now)  # not confirmed
        self.s.commit()
        self.assertEqual(self.login("newboss", "correct-horse-battery").outcome, Outcome.invalid)

    def test_reset_2fa_revokes_sessions_and_needs_a_new_confirmation(self):
        admin, password, secret = self.make_admin()
        row = self.find(self.login("boss", password).token)[0]
        token = self.totp_step(row, self.code(secret)).token
        new_secret = users.enroll_totp(self.s, self.cli, admin, self.box, self.now)
        self.s.commit()
        self.assertNotEqual(new_secret, secret)
        self.assertIsNone(self.find(token))
        self.assertEqual(self.login("boss", password).outcome, Outcome.invalid)
        self.assertFalse(users.confirm_totp(self.s, self.cli, admin, self.box, self.code(secret), self.now))
        self.assertTrue(users.confirm_totp(self.s, self.cli, admin, self.box, self.code(new_secret), self.now))
        self.s.commit()
        self.assertEqual(self.login("boss", password).outcome, Outcome.ok)
        actions = self.s.execute(select(AuditLog.action).order_by(AuditLog.id)).scalars().all()
        self.assertIn("user.reset_2fa", actions)

    # ---------------------------------------------------------------- QR tokens
    def qr(self, token, ip=IP):
        r = flows.login_qr(self.s, self.settings, self.now, token=token, ip=ip, user_agent="UA")
        self.s.commit()
        return r

    def test_qr_token_is_one_time(self):
        user, _ = self.make_student()
        token, expires = users.issue_login_token(self.s, self.cli, user, self.settings, self.now)
        self.s.commit()
        self.assertEqual(expires, T0 + timedelta(days=7))
        r = self.qr(token)
        self.assertEqual(r.outcome, Outcome.ok)
        self.assertEqual(self.find(r.token)[1].id, user.id)
        self.assertEqual(self.qr(token).outcome, Outcome.invalid)
        self.assertEqual(self.qr("garbage").outcome, Outcome.invalid)
        kinds = self.s.execute(select(AuthFailure.kind, AuthFailure.login)).all()
        self.assertEqual([(k.value, login) for k, login in kinds], [("qr", None), ("qr", None)])

    def test_new_qr_token_revokes_the_old_and_tokens_expire(self):
        user, _ = self.make_student()
        old, _ = users.issue_login_token(self.s, self.cli, user, self.settings, self.now)
        new, _ = users.issue_login_token(self.s, self.cli, user, self.settings, self.now)
        self.s.commit()
        self.assertEqual(self.qr(old).outcome, Outcome.invalid)
        self.tick(days=7)
        self.assertEqual(self.qr(new).outcome, Outcome.invalid)

    def test_qr_is_not_for_admins(self):
        admin, _password, _secret = self.make_admin()
        with self.assertRaises(users.UserError):
            users.issue_login_token(self.s, self.cli, admin, self.settings, self.now)
        # even a token row put in by hand does not log an admin in
        self.s.rollback()
        token = sessions.new_token()
        self.s.add(LoginToken(token_hash=sessions.hash_token(token), user_id=admin.id, created_at=self.now,
                              expires_at=self.now + timedelta(days=1)))
        self.s.commit()
        self.assertEqual(self.qr(token).outcome, Outcome.invalid)

    def test_qr_token_of_a_disabled_student_does_not_work(self):
        user, _ = self.make_student()
        token, _ = users.issue_login_token(self.s, self.cli, user, self.settings, self.now)
        users.set_active(self.s, self.cli, user, False, self.now)  # also revokes the token
        self.s.commit()
        self.assertEqual(self.qr(token).outcome, Outcome.invalid)
        with self.assertRaises(users.UserError):
            users.issue_login_token(self.s, self.cli, user, self.settings, self.now)

    # ---------------------------------------------------------------- account management
    def test_reset_password_revokes_everything_and_unlocks(self):
        user, password = self.make_student()
        token = self.login("ivanov.p", password).token
        qr, _ = users.issue_login_token(self.s, self.cli, user, self.settings, self.now)
        self.s.commit()
        for _ in range(10):
            self.login("ivanov.p", "wrong")
        new = users.reset_password(self.s, self.cli, user, self.now)
        self.s.commit()
        self.assertNotEqual(new, password)
        self.assertIsNone(self.find(token))
        self.assertEqual(self.qr(qr).outcome, Outcome.invalid)
        self.tick(seconds=1)
        self.assertEqual(self.login("ivanov.p", password).outcome, Outcome.invalid)
        self.assertEqual(self.login("ivanov.p", new).outcome, Outcome.ok)
        entry = self.s.execute(select(AuditLog).where(AuditLog.action == "user.reset_password")).scalar_one()
        self.assertEqual((entry.actor_cli, entry.actor_user_id), ("cli:tester", None))
        self.assertEqual(entry.details, {"sessions_revoked": 1, "qr_tokens_revoked": 1, "unlocked": True,
                                         "sudo_user": "alice"})

    def test_admin_password_reset_takes_a_chosen_password(self):
        admin, _password, _secret = self.make_admin()
        with self.assertRaises(users.UserError):
            users.reset_password(self.s, self.cli, admin, self.now)
        with self.assertRaises(users.UserError):
            users.reset_password(self.s, self.cli, admin, self.now, new_password="too-short")
        users.reset_password(self.s, self.cli, admin, self.now, new_password="another-long-phrase")
        self.s.commit()
        self.assertEqual(self.login("boss", "another-long-phrase").outcome, Outcome.ok)

    def test_create_student_logins(self):
        a, _ = self.make_student()
        b, _ = self.make_student()
        c, _ = self.make_student(login=" Petya.Ivanov ")
        self.assertEqual([a.login, b.login, c.login], ["ivanov.p", "ivanov.p2", "petya.ivanov"])
        self.assertEqual(a.display_name, "Пётр И.")
        self.assertEqual(c.role, Role.student)
        with self.assertRaises(users.UserError):
            self.make_student(login="ivanov.p")
        with self.assertRaises(users.UserError):
            self.make_student(login="no")
        self.s.rollback()
        self.assertEqual(users.set_login(self.s, self.cli, a, "Ivanov.Petr", self.now), "ivanov.petr")
        with self.assertRaises(users.UserError):
            users.set_login(self.s, self.cli, b, "ivanov.petr", self.now)
        with self.assertRaises(users.UserError):
            users.create_admin(self.s, self.cli, self.now, login="boss2", display_name="B", password="short")

    # ---------------------------------------------------------------- password change
    def change(self, token, current, new, code=None):
        row, user = self.find(token)
        r = flows.change_password(self.s, self.settings, self.now, row=row, user=user, current=current, new=new, ip=IP,
                                  box=self.box, totp_code=code)
        self.s.commit()
        return r

    def test_student_changes_password(self):
        _user, password = self.make_student()
        current = self.login("ivanov.p", password).token
        other = self.login("ivanov.p", password).token
        self.assertEqual(self.change(current, "wrong", "my-new-secret").outcome, Outcome.invalid)
        with self.assertRaises(flows.PasswordChangeError):
            self.change(current, password, "qwerty123")
        with self.assertRaises(flows.PasswordChangeError):
            self.change(current, password, "short")
        self.s.rollback()
        r = self.change(current, password, "my-new-secret")
        self.assertEqual(r.outcome, Outcome.ok)
        self.assertIsNone(self.find(current))  # the old cookie value is gone ...
        self.assertIsNotNone(self.find(r.token))  # ... the same session lives on with a new one
        self.assertIsNone(self.find(other))
        self.assertEqual(self.login("ivanov.p", "my-new-secret").outcome, Outcome.ok)

    def test_admin_password_change_needs_a_totp_code(self):
        _admin, password, secret = self.make_admin()
        row = self.find(self.login("boss", password).token)[0]
        token = self.totp_step(row, self.code(secret)).token
        self.tick(seconds=30)
        self.assertEqual(self.change(token, password, "brand-new-passphrase").outcome, Outcome.invalid)
        self.assertEqual(self.change(token, password, "brand-new-passphrase", code="000000").outcome, Outcome.invalid)
        with self.assertRaises(flows.PasswordChangeError):
            self.change(token, password, "short-one", code=self.code(secret))
        self.s.rollback()
        self.tick(seconds=30)
        r = self.change(token, password, "brand-new-passphrase", code=self.code(secret))
        self.assertEqual(r.outcome, Outcome.ok)
        self.assertEqual(self.login("boss", "brand-new-passphrase").outcome, Outcome.ok)

    # ---------------------------------------------------------------- housekeeping
    def test_prune_removes_only_old_rows(self):
        user, password = self.make_student()
        self.login("ivanov.p", "wrong")
        token = self.login("ivanov.p", password).token  # expires at day 30, pruned 30 days later
        self.tick(days=61)
        fresh = self.login("ivanov.p", password).token
        self.login("ivanov.p", "wrong")
        out = cleanup.prune(self.s, self.settings, self.now)
        self.s.commit()
        self.assertEqual((out["failures"], out["sessions"]), (1, 1))
        self.assertIsNone(self.find(token))
        self.assertIsNotNone(self.find(fresh))
        self.assertEqual(self.s.execute(select(func.count()).select_from(AuthFailure)).scalar_one(), 1)
