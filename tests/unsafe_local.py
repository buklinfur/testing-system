"""The environment of the UNSAFE local sandbox for tests (D-065), set only while a test module or test runs.

The local backend runs only with JUDGE_ALLOW_UNSAFE_LOCAL=1 and JUDGE_DEV=1 and not under systemd (no INVOCATION_ID).
Tests never set these in os.environ for good: a module calls enable_for_module() from setUpModule (restored when the
module ends), a single test or class uses `with unsafe_local():` / @unsafe_local(), and a child process gets
child_env(...). Nothing leaks into the next test module.
"""
import contextlib
import os
import unittest
from unittest import mock

ENV = {"JUDGE_ALLOW_UNSAFE_LOCAL": "1", "JUDGE_DEV": "1"}
SYSTEMD = ("INVOCATION_ID",)


@contextlib.contextmanager
def unsafe_local():
    """os.environ with the local backend allowed; everything (INVOCATION_ID included) restored afterwards."""
    with mock.patch.dict(os.environ, ENV):
        for name in SYSTEMD:
            os.environ.pop(name, None)  # patch.dict restores it on exit
        yield


def enable_for_module() -> None:
    """Call from setUpModule: allowed for the tests of this module only."""
    ctx = unsafe_local()
    ctx.__enter__()
    unittest.addModuleCleanup(ctx.__exit__, None, None, None)


def child_env(base=None, **extra) -> dict:
    """The environment of a child process that runs the local backend (a worker, runner.cli)."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in SYSTEMD}
    env.update(ENV)
    env.update(extra)
    return env


def enable_for_test(case: unittest.TestCase) -> None:
    """Call from setUp: allowed for this test (and its setUp) only, restored by the test's cleanup."""
    ctx = unsafe_local()
    ctx.__enter__()
    case.addCleanup(ctx.__exit__, None, None, None)
