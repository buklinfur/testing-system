"""Deprecated alias of judgectl (module 3 syntax), kept until module 6 for deploy scripts and judge-dev.

    python -m app.admin_cli create-worker-token NAME [--caps cpu,gpu]   -> judgectl worker token create
    python -m app.admin_cli revoke-worker-token NAME                    -> judgectl worker token revoke
    python -m app.admin_cli status                                      -> judgectl queue status + worker list
    python -m app.admin_cli reap | judgements [--last N] | events [--last N]
                            | rejudge --submission N | --contest-problem N [--verdicts WA,TL]   -> judgectl queue ...
"""
from __future__ import annotations

import sys
from typing import Optional

from .judgectl.cli import main as judgectl

RENAMED = {"create-worker-token": ["worker", "token", "create"], "revoke-worker-token": ["worker", "token", "revoke"],
           "reap": ["queue", "reap"], "judgements": ["queue", "judgements"], "events": ["queue", "events"],
           "rejudge": ["queue", "rejudge"]}


def translate(argv: list) -> list:
    """Old arguments -> a list of judgectl argument lists (status is two commands)."""
    if not argv:
        return [["--help"]]
    cmd, rest = argv[0], list(argv[1:])
    if cmd == "status":
        return [["queue", "status"] + rest, ["worker", "list"] + rest]
    if cmd in RENAMED:
        return [RENAMED[cmd] + rest]
    return [argv]  # let judgectl report it


def main(argv: Optional[list] = None, ctx=None) -> int:
    commands = translate(list(sys.argv[1:] if argv is None else argv))
    print("admin_cli is deprecated, use: " + " && ".join("judgectl " + " ".join(c) for c in commands),
          file=sys.stderr if ctx is None else ctx.err)
    for args in commands:
        code = judgectl(args, ctx)
        if code:
            return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
