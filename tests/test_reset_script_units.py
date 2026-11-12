"""deploy/dev/reset-production.sh and deploy/setup-postgres.sh share one definition of a judge database
(deploy/lib/pg-database.sh, D-073); the reset recreates the database in a fixed order after an explicit confirmation.
The scripts need root and are not run here: this reads them. The database half runs as root in
deploy/dev/check-reset-db.sh; its data and catalog checks in tests/pg/test_reset_check_pg.py.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy/lib/pg-database.sh"
SETUP = ROOT / "deploy/setup-postgres.sh"
RESET = ROOT / "deploy/dev/reset-production.sh"
CHECK = ROOT / "deploy/dev/check-reset-db.sh"


def code(path: Path) -> str:
    """The script without its comment lines."""
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))


class ResetScriptTests(unittest.TestCase):
    def test_bash_syntax(self):
        for path in (LIB, SETUP, RESET, CHECK):
            with self.subTest(path.name):
                subprocess.run(["bash", "-n", str(path)], check=True)

    def test_one_definition_of_a_database(self):
        lib = code(LIB)
        for name in ("judge_role_ensure", "judge_db_create", "judge_db_connections", "judge_db_recreate"):
            self.assertRegex(lib, rf"(?m)^{name}\(\) {{")
        self.assertNotIn("FORCE", lib.upper())  # no DROP DATABASE ... (FORCE): connections are stopped, D-064
        for path in (SETUP, RESET, CHECK):
            with self.subTest(path.name):
                self.assertRegex(code(path), r'(?m)^\. "\$\(dirname "\$0"\)/(\.\./)?lib/pg-database\.sh"')
                for sql in ("CREATE DATABASE", "ALTER DATABASE", "REVOKE", "GRANT", "CREATE ROLE", "ALTER ROLE",
                            "ALTER SCHEMA", "DROP DATABASE" if path != CHECK else "DROP SCHEMA"):
                    # the check script drops its own throw-away databases at the end, nothing else
                    self.assertNotIn(sql, code(path), f"{path.name} makes a database itself: use the library")
        self.assertIn('judge_role_ensure "$role" "$pw"', code(SETUP))
        self.assertIn('judge_db_create "$role"', code(SETUP))

    def test_reset_never_downgrades_or_truncates(self):
        reset = code(RESET)
        for word in ("downgrade", "TRUNCATE", "DROP SCHEMA", "dropdb", "pg_terminate_backend"):
            self.assertNotIn(word, reset)

    def test_reset_order(self):
        """Confirmation, checks, stop, no connections, recreate, migrate, wipe the files, token - in this order."""
        reset = code(RESET)
        steps = ['[ "${1:-}" = --yes-wipe-production ]', "[ -x /opt/judge/.venv/bin/alembic ]",
                 'systemctl --no-pager stop', 'judge_db_connections "$DB"', 'judge_db_recreate "$DB"',
                 "alembic.ini upgrade head", 'find "$BLOBS" "$CACHE" -mindepth 1 -delete',
                 "worker token create"]
        where = [reset.find(step) for step in steps]
        self.assertNotIn(-1, where, dict(zip(steps, where)))
        self.assertEqual(where, sorted(where), dict(zip(steps, where)))
        self.assertLess(reset.find("--yes-wipe-production"), reset.find("systemctl"))
        self.assertIn("trap on_exit EXIT", reset)
        phases = re.findall(r"(?m)^PHASE=(\w+)", reset)
        self.assertEqual(phases, ["checks", "stop", "connections", "recreate", "migrate", "files", "token", "done"])
        for phase in phases[:-1]:
            self.assertRegex(reset, rf"(?m)^\s+[\w|]*\b{phase}\b[\w|]*\)", f"no message for the phase {phase}")

    def test_the_check_script_does_what_the_reset_does_on_its_own_databases(self):
        check = code(CHECK)
        self.assertNotRegex(check, r"\bjudge\b(?!_)", "the check script never touches the database judge")
        self.assertIn('judge_db_recreate "$DB"', check)
        self.assertIn('judge_db_create "$FRESH" "$ROLE"', check)
        self.assertIn("trap cleanup EXIT", check)
        # the names are checked against a fixed list before anything is dropped or created
        allow = check.find("judge_reset_check|judge_reset_check_fresh) ;;")
        self.assertGreater(allow, 0)
        self.assertLess(allow, check.find("\ncleanup\n"))
        # the cleanup trap is set before the first thing is created
        self.assertLess(check.find("trap cleanup EXIT"), check.find('judge_role_ensure "$ROLE"'))
