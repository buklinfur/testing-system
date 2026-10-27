"""Pure parts of app.auth (no database): logins, transliteration, passwords, TOTP, the secret box, settings."""
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from argon2 import PasswordHasher  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402

from app.auth import logins, passwords, totp  # noqa: E402
from app.models import Role  # noqa: E402
from app.settings import Settings  # noqa: E402

NOW = datetime(2026, 10, 27, 12, 0, 10, tzinfo=timezone.utc)


class LoginTests(unittest.TestCase):
    def test_transliteration_scheme(self):
        cases = {
            ("Иванов", "Пётр"): "ivanov.p",
            ("Щукина-Жукова", "Эля"): "shchukina-zhukova.e",
            ("Цыплёнкова", "Юлия"): "tsyplenkova.y",
            ("Рыбин", "Ян"): "rybin.y",
            ("Соловьёв", "Хасан"): "solovev.k",
            ("Подъячев", "Чеслав"): "podyachev.c",
            ("Ли", "Ольга"): "li.o",
            ("де ла Круз", "Мария"): "de-la-kruz.m",
            ("Smith", "John"): "smith.j",
        }
        for (surname, first), login in cases.items():
            with self.subTest(surname=surname):
                self.assertEqual(logins.default_login(surname, first), login)

    def test_default_login_limits(self):
        long = logins.default_login("Константинопольский-Северодвинский", "Ян")
        self.assertLessEqual(len(long), 32)
        self.assertTrue(logins.is_valid_login(long))
        for surname in ("", "ъь", "!!"):
            with self.assertRaises(logins.LoginError):
                logins.default_login(surname, "Ян")

    def test_unique_login_adds_a_number(self):
        taken = {"ivanov.p", "ivanov.p2"}
        self.assertEqual(logins.unique_login("ivanov.p", taken.__contains__), "ivanov.p3")
        self.assertEqual(logins.unique_login("petrov.a", taken.__contains__), "petrov.a")
        base = "a" * 32
        got = logins.unique_login(base, {base}.__contains__)
        self.assertEqual(got, "a" * 31 + "2")

    def test_normalisation_and_format(self):
        self.assertEqual(logins.normalize_login("  Ivanov.P "), "ivanov.p")
        self.assertEqual(logins.checked_login(" Ivanov.P "), "ivanov.p")
        for bad in ("ab", ".abc", "-abc", "иванов", "a b c", "a" * 33, ""):
            with self.subTest(bad=bad), self.assertRaises(logins.LoginError):
                logins.checked_login(bad)

    def test_display_name(self):
        self.assertEqual(logins.default_display_name("Иванов", "Пётр"), "Пётр И.")
        self.assertEqual(logins.default_display_name("иванов", " Пётр "), "Пётр И.")


class PasswordTests(unittest.TestCase):
    def test_generated_passwords(self):
        pattern = re.compile(r"[a-z]{3,7}-[a-z]{3,7}-[2-9]{3}")
        seen = {passwords.generate_password() for _ in range(300)}
        self.assertGreater(len(seen), 290)
        for p in seen:
            self.assertRegex(p, pattern)
        self.assertGreaterEqual(len(passwords.wordlist()), 1024)
        self.assertGreaterEqual(passwords.generated_password_bits(), 29.9)
        self.assertTrue(all(re.fullmatch(r"[a-z]{3,7}", w) for w in passwords.wordlist()))

    def test_policy(self):
        ok = (("lisa-kot-47", Role.student), ("correct-horse-1", Role.admin))
        for pw, role in ok:
            passwords.check_policy(pw, "ivanov.p", role)
        bad = [
            ("short12", Role.student),  # 7 < 8
            ("elevenchars", Role.admin),  # 11 < 12
            ("ivanov.p", Role.student),  # equals the login
            ("IVANOV.P", Role.student),
            ("Password123", Role.student),  # common
            ("qwerty123", Role.student),
            (" " * 12, Role.admin),
            ("x" * 129, Role.student),
        ]
        for pw, role in bad:
            with self.subTest(pw=pw), self.assertRaises(passwords.PasswordPolicyError):
                passwords.check_policy(pw, "ivanov.p", role)

    def test_hash_verify_and_rehash(self):
        h = passwords.hash_password("lisa-kot-472")
        self.assertTrue(h.startswith("$argon2id$"))
        self.assertEqual(passwords.verify_password(h, "lisa-kot-472"), (True, None))
        self.assertEqual(passwords.verify_password(h, "lisa-kot-473"), (False, None))
        self.assertEqual(passwords.verify_password("!", "anything"), (False, None))  # unusable hash
        weak = PasswordHasher(time_cost=1, memory_cost=8192).hash("lisa-kot-472")
        ok, new = passwords.verify_password(weak, "lisa-kot-472")
        self.assertTrue(ok)
        self.assertIsNotNone(new)
        self.assertEqual(passwords.verify_password(new, "lisa-kot-472"), (True, None))


class TotpTests(unittest.TestCase):
    secret = "JBSWY3DPEHPK3PXP"

    def code(self, delta_steps=0):
        return totp.code_at_step(self.secret, totp.step_at(NOW) + delta_steps)

    def test_window_and_replay(self):
        step = totp.step_at(NOW)
        self.assertEqual(totp.match_code(self.secret, self.code(), NOW, None), step)
        self.assertEqual(totp.match_code(self.secret, self.code(-1), NOW, None), step - 1)
        self.assertEqual(totp.match_code(self.secret, self.code(1), NOW, None), step + 1)
        self.assertIsNone(totp.match_code(self.secret, self.code(-2), NOW, None))
        self.assertIsNone(totp.match_code(self.secret, self.code(2), NOW, None))
        self.assertIsNone(totp.match_code(self.secret, self.code(), NOW, step))  # the same code twice
        self.assertIsNone(totp.match_code(self.secret, self.code(-1), NOW, step))  # an older one after it
        self.assertEqual(totp.match_code(self.secret, self.code(1), NOW, step), step + 1)

    def test_code_format(self):
        c = self.code()
        self.assertIsNotNone(totp.match_code(self.secret, f" {c[:3]} {c[3:]} ", NOW, None))
        for bad in ("", "12345", "1234567", "abcdef"):
            self.assertIsNone(totp.match_code(self.secret, bad, NOW, None))

    def test_matches_rfc6238_vector(self):
        # RFC 6238 appendix B, SHA-1, T = 59 s: 94287082 (8 digits); its last 6 digits are the 6-digit code
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
        self.assertEqual(totp.code_at_step(secret, 59 // 30), "287082")

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri(self.secret, "boss", "judge")
        self.assertTrue(uri.startswith("otpauth://totp/judge:boss?"))
        self.assertIn("secret=" + self.secret, uri)

    def test_secret_box(self):
        key = Fernet.generate_key().decode()
        box = totp.SecretBox(key)
        enc = box.encrypt(self.secret)
        self.assertNotIn(self.secret, enc)
        self.assertEqual(box.decrypt(enc), self.secret)
        with self.assertRaises(totp.TotpConfigError):
            totp.SecretBox(Fernet.generate_key().decode()).decrypt(enc)
        for bad in (None, "", "not-a-key"):
            with self.assertRaises(totp.TotpConfigError):
                totp.SecretBox(bad)
        self.assertEqual(len(totp.new_secret()), 32)


class SettingsTests(unittest.TestCase):
    def test_auth_settings_from_env(self):
        s = Settings.from_env({"JUDGE_SECRET_KEY": "k", "JUDGE_IP_FAIL_LIMIT": "50", "JUDGE_LOGIN_FAIL_LIMIT": "7"}).auth
        self.assertEqual((s.secret_key, s.ip_fail_limit, s.login_fail_limit), ("k", 50, 7))
        d = Settings.from_env({}).auth
        self.assertIsNone(d.secret_key)
        self.assertEqual((d.ip_fail_limit, d.ip_window_s, d.ip_lockout_s), (300, 900, 900))
        self.assertEqual((d.login_fail_limit, d.admin_login_fail_limit, d.login_lockout_s), (10, 5, 900))
        self.assertEqual((d.student_idle_s, d.admin_idle_s, d.admin_absolute_s, d.mfa_pending_s),
                         (30 * 86400, 12 * 3600, 7 * 86400, 300))


if __name__ == "__main__":
    unittest.main()
