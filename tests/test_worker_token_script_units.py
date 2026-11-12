"""deploy/lib/worker-token.sh run by a real bash (D-089), without root: chown is a stub, judgectl a stand-in that writes
a token into the --out file as the real one does. The token reaches worker.env and nothing else: not the output, not
an xtrace (every command line with its arguments expanded, what ps would show), not a file left behind.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "deploy" / "lib" / "worker-token.sh"
INSTALL = ROOT / "deploy" / "install-wsl.sh"
RESET = ROOT / "deploy" / "dev" / "reset-production.sh"
TOKEN = "jw_" + "Q" * 43

HARNESS = r"""
set -euo pipefail
. "$LIB"
trap judge_token_cleanup EXIT
chown() { :; }  # not root here
fake_judge() {  # stands for `as_judge`: "$@" = python -m app.judgectl worker token create NAME --out FILE
  [ -f "$FAKE" ] || { echo "judgectl: refused" >&2; return 1; }
  local out=${@: -1}
  (umask 077; cp "$FAKE" "$out")  # the token comes from a file, as from judgectl: the trace shows only paths
  echo "created: $*"
}
judge_worker_token_new main "$ENVFILE" fake_judge
echo "dir left: ${JUDGE_TOKEN_DIR:-none}"
"""


class WorkerTokenScriptTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = self.tmp / "worker.env"
        self.env.write_text("JUDGE_API_URL=http://127.0.0.1:8001\nJUDGE_WORKER_TOKEN=CHANGE_ME\nJUDGE_WORKERS=5\n")
        self.tmpdir = self.tmp / "t"
        self.tmpdir.mkdir()

    def run_lib(self, fake=TOKEN, xtrace=False):
        given = self.tmp / "given-token"
        if fake == "fail":
            given.unlink(missing_ok=True)
        else:
            given.write_text(fake + "\n")
        env = {"PATH": os.environ["PATH"], "LIB": str(LIB), "ENVFILE": str(self.env), "FAKE": str(given),
               "TMPDIR": str(self.tmpdir)}
        argv = ["bash", "-x", "-c", HARNESS] if xtrace else ["bash", "-c", HARNESS]
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    def test_the_token_reaches_worker_env_only(self):
        for xtrace in (False, True):
            with self.subTest(xtrace=xtrace):
                self.env.write_text("JUDGE_API_URL=x\nJUDGE_WORKER_TOKEN=CHANGE_ME\nJUDGE_WORKERS=5\n")
                r = self.run_lib(xtrace=xtrace)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.env.read_text(),
                                 f"JUDGE_API_URL=x\nJUDGE_WORKER_TOKEN={TOKEN}\nJUDGE_WORKERS=5\n")
                self.assertNotIn(TOKEN, r.stdout + r.stderr)  # with -x: no command line carried it
                self.assertIn("dir left: none", r.stdout)
                self.assertEqual(list(self.tmpdir.iterdir()), [])  # the private directory is gone
                self.assertFalse(Path(f"{self.env}.new").exists())
                self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o640)

    def test_a_missing_line_is_added(self):
        self.env.write_text("JUDGE_API_URL=x\n")
        self.assertEqual(self.run_lib().returncode, 0)
        self.assertEqual(self.env.read_text(), f"JUDGE_API_URL=x\nJUDGE_WORKER_TOKEN={TOKEN}\n")

    def test_a_failure_changes_nothing_and_leaves_nothing(self):
        before = self.env.read_text()
        for fake in ("fail", "not-a-token"):
            with self.subTest(fake=fake):
                r = self.run_lib(fake=fake)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(self.env.read_text(), before)
                self.assertEqual(list(self.tmpdir.iterdir()), [])  # the EXIT trap removed it
                self.assertNotIn("not-a-token", r.stdout + r.stderr)

    def test_the_scripts_use_the_library_and_never_handle_the_token_themselves(self):
        lib = LIB.read_text()
        self.assertIn('worker token create "$name" --out "$JUDGE_TOKEN_DIR/token"', lib)
        self.assertIn("umask 077", lib)
        self.assertIn("chmod 640", lib)
        for path in (INSTALL, RESET):
            text = path.read_text()
            with self.subTest(script=path.name):
                self.assertIn("lib/worker-token.sh", text)
                self.assertIn("judge_worker_token_new ", text)
                self.assertNotIn("worker token create", text)
                self.assertRegex(text, r"trap [^\n]*\bEXIT\b")
        self.assertIn("trap judge_token_cleanup EXIT", INSTALL.read_text())
        self.assertRegex(RESET.read_text(), r"on_exit\(\) \{\n  local code=\$\?\n  judge_token_cleanup")
        for path in (LIB, INSTALL, RESET):
            text = path.read_text()
            with self.subTest(no_token_in_argv=path.name):
                self.assertNotIn("tail -1", text)
                self.assertNotRegex(text, r"awk\s+-v")
                self.assertNotRegex(text, r"\$\{?[Tt][Oo][Kk][Ee][Nn]\b")  # no shell variable holds a token
                self.assertNotIn('ENVIRON["TOKEN"]', text)
                self.assertIsNone(re.search(r"echo \"?\$\(?cat [^\n]*token", text))


if __name__ == "__main__":
    unittest.main()
