"""The UNSAFE local backend runs only on a development machine (D-065, docs/RUNNER.md): JUDGE_ALLOW_UNSAFE_LOCAL=1 and
JUDGE_DEV=1, not under systemd (INVOCATION_ID). Two independent checks - the sandbox itself (runner/sandbox/local.py)
and the worker before it starts (worker/cli.py) - agree on every combination, each refuses on its own when the other
is bypassed, the refusal names what is missing and never a value, and the test helpers do not leak the variables.
"""
import io
import logging
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.common import SandboxError  # noqa: E402
from runner.config import RunnerConfig  # noqa: E402
from runner.sandbox import local as local_sandbox  # noqa: E402
from runner.sandbox import make_factory  # noqa: E402
from tests import unsafe_local as helpers  # noqa: E402
from worker import cli as worker_cli  # noqa: E402
from worker.config import WorkerConfig  # noqa: E402

NAMES = ("JUDGE_ALLOW_UNSAFE_LOCAL", "JUDGE_DEV", "INVOCATION_ID", "JUDGE_BACKEND")
UNSET = None
ALLOW, DEV, SYSTEMD = ("JUDGE_ALLOW_UNSAFE_LOCAL=1 is not set", "JUDGE_DEV=1 is not set",
                       "it runs under systemd (INVOCATION_ID is set)")

# JUDGE_BACKEND, JUDGE_ALLOW_UNSAFE_LOCAL, JUDGE_DEV, INVOCATION_ID -> None (allowed) or the missing conditions
TABLE = [
    ("isolate", UNSET, UNSET, UNSET, None),                     # production: the local rule does not apply
    ("isolate", UNSET, UNSET, "a1b2c3", None),                  # production under systemd
    ("local", UNSET, UNSET, UNSET, [ALLOW, DEV]),
    ("local", "1", UNSET, UNSET, [DEV]),
    ("local", UNSET, "1", UNSET, [ALLOW]),
    ("local", "1", "1", UNSET, None),                           # a development machine: the only allowed row
    ("local", "1", "1", "a1b2c3", [SYSTEMD]),                   # a systemd service, even with both flags
    ("local", "1", UNSET, "a1b2c3", [DEV, SYSTEMD]),
    ("local", UNSET, UNSET, "a1b2c3", [ALLOW, DEV, SYSTEMD]),
    ("local", "true", "1", UNSET, [ALLOW]),                     # only the exact value 1
    ("local", "1", "0", UNSET, [DEV]),
    ("local", "1", "yes", UNSET, [DEV]),
    ("local", "1", "1", "", [SYSTEMD]),                         # an empty INVOCATION_ID still means systemd
]


def env_of(backend, allow, dev, invocation) -> dict:
    """A clean environment with only these variables (and PATH) set."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "JUDGE_BACKEND": backend}
    for name, value in (("JUDGE_ALLOW_UNSAFE_LOCAL", allow), ("JUDGE_DEV", dev), ("INVOCATION_ID", invocation)):
        if value is not None:
            env[name] = value
    return env


def worker_config(backend: str) -> WorkerConfig:
    return WorkerConfig(token="test-token", backend=backend, workers=1, pin_cpus=False)


def log_to(stream) -> None:
    """worker.cli's setup_logging, into `stream` (the test reads what the worker logged)."""
    root = logging.getLogger()
    previous = root.handlers[:]
    root.handlers[:] = [logging.StreamHandler(stream)]
    unittest.addModuleCleanup(root.handlers.__setitem__, slice(None), previous)


def dispatch(command: str, backend: str, env: dict):
    """worker.cli._dispatch with this environment, the supervisor and the worker process replaced: (exit code,
    stderr). 0 means the check let it through to the stand-in."""
    args = SimpleNamespace(command=command, index=0)
    err = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch.object(worker_cli, "Supervisor") as sup, \
            mock.patch.object(worker_cli, "cmd_check", return_value=0), \
            mock.patch("worker.process.WorkerProcess") as proc, \
            mock.patch.object(worker_cli, "setup_logging", lambda *a, **k: log_to(err)), \
            redirect_stderr(err):
        sup.return_value.run.return_value = 0
        proc.return_value.run.return_value = 0
        code = worker_cli._dispatch(args, worker_config(backend))
    return code, err.getvalue()


class RuleTableTests(unittest.TestCase):
    def test_each_row_in_both_checks(self):
        for backend, allow, dev, invocation, missing in TABLE:
            env = env_of(backend, allow, dev, invocation)
            with self.subTest(backend=backend, allow=allow, dev=dev, invocation=invocation):
                # the sandbox: refuses or opens, whatever the worker said
                if backend == "local":
                    with mock.patch.dict(os.environ, env, clear=True):
                        if missing:
                            with self.assertRaises(SandboxError) as caught:
                                local_sandbox.LocalSandbox()
                            self.assertEqual([m for m in (ALLOW, DEV, SYSTEMD) if m in str(caught.exception)],
                                             missing)
                        else:
                            box = local_sandbox.LocalSandbox()
                            box.init()
                            box.cleanup()
                # the worker, for every command that runs programs
                for command in ("run", "run-one", "check"):
                    code, err = dispatch(command, backend, env)
                    if missing:
                        self.assertEqual(code, 78, f"{command}: {err}")
                        for m in missing:
                            self.assertIn(m, err)
                    else:
                        self.assertEqual(code, 0, f"{command}: {err}")
                # the two rules agree
                self.assertEqual(local_sandbox.local_refusal(env) is None, worker_cli.local_backend_refusal(env) is None)

    def test_without_the_variables_local_is_refused(self):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SandboxError):
                make_factory(RunnerConfig(backend="local"))(0)
        self.assertEqual(dispatch("run", "local", env)[0], 78)

    def test_the_refusal_names_what_is_missing_and_no_value(self):
        env = {"JUDGE_ALLOW_UNSAFE_LOCAL": "S3CRET-allow", "JUDGE_DEV": "S3CRET-dev", "INVOCATION_ID": "S3CRET-unit",
               "JUDGE_WORKER_TOKEN": "S3CRET-token", "DATABASE_URL": "postgresql://u:S3CRET@db/x"}
        for text in (local_sandbox.local_refusal(env), worker_cli.local_backend_refusal(env), dispatch("run", "local",
                                                                                                         env)[1]):
            with self.subTest(text=text[:40]):
                self.assertNotIn("S3CRET", text)
                for m in (ALLOW, DEV, SYSTEMD):
                    self.assertIn(m, text)


class IndependenceTests(unittest.TestCase):
    """Bypassing one check does not open the local backend: the other one still refuses."""

    def test_worker_check_bypassed_the_sandbox_refuses(self):
        env = env_of("local", UNSET, UNSET, "a1b2c3")
        with mock.patch.object(worker_cli, "local_backend_refusal", return_value=None):
            self.assertEqual(dispatch("run", "local", env)[0], 0)  # the worker let it through ...
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(SandboxError):
            make_factory(RunnerConfig(backend="local"))(0)  # ... but no sandbox opens

    def test_sandbox_check_bypassed_the_worker_refuses(self):
        env = env_of("local", "1", "1", "a1b2c3")
        with mock.patch.object(local_sandbox, "local_refusal", return_value=None):
            with mock.patch.dict(os.environ, env, clear=True):
                local_sandbox.LocalSandbox()  # the sandbox would open ...
            code, err = dispatch("run", "local", env)
        self.assertEqual(code, 78)  # ... but the worker does not start
        self.assertIn(SYSTEMD, err)

    def test_the_worker_does_not_call_the_sandbox_rule(self):
        source = (ROOT / "worker" / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("local_refusal", source.replace("local_backend_refusal", ""))


class HelperTests(unittest.TestCase):
    """tests/unsafe_local.py sets the variables only while a module or a test runs."""

    def test_the_helper_restores_the_environment(self):
        before = dict(os.environ)
        with mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
            snapshot = dict(os.environ)
            with helpers.unsafe_local():
                self.assertEqual((os.environ.get("JUDGE_ALLOW_UNSAFE_LOCAL"), os.environ.get("JUDGE_DEV")), ("1", "1"))
                self.assertNotIn("INVOCATION_ID", os.environ)
                self.assertIsNone(local_sandbox.local_refusal(os.environ))
            self.assertEqual(dict(os.environ), snapshot)
        self.assertEqual(dict(os.environ), before)

    def test_child_env(self):
        env = helpers.child_env({"PATH": "/bin", "INVOCATION_ID": "x"}, JUDGE_WORKERS="2")
        self.assertEqual(env, {"PATH": "/bin", "JUDGE_ALLOW_UNSAFE_LOCAL": "1", "JUDGE_DEV": "1", "JUDGE_WORKERS": "2"})

    def test_no_test_module_sets_the_variables_for_good(self):
        """Every test that needs the local backend goes through tests/unsafe_local.py, never os.environ[...] = ."""
        offenders = []
        for path in sorted((ROOT / "tests").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for name in ("JUDGE_ALLOW_UNSAFE_LOCAL", "JUDGE_DEV"):
                if f'os.environ["{name}"] =' in text or f"os.environ['{name}'] =" in text \
                        or f'os.environ.setdefault("{name}"' in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
