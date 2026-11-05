"""Reaper: returns the judgements of lost workers to the queue. Runs as its own systemd unit.

About once an hour it also prunes old authentication rows (failures, lockouts, dead sessions, QR tokens).

    python -m app.maintenance

Several reapers at once are harmless (SKIP LOCKED), but one is enough.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timezone

from . import queue as q
from .auth import cleanup
from .db import session_scope
from .settings import Settings

log = logging.getLogger("judge.reaper")
PRUNE_EVERY_S = 3600


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s [reaper] %(message)s")
    all_settings = Settings.from_env()
    settings = all_settings.queue
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("started: heartbeat timeout %.0fs, max attempts %d, check timeout %.0fs", settings.heartbeat_timeout_s,
             settings.max_attempts, settings.check_timeout_s)
    last_prune = 0.0
    while not stop.is_set():
        try:
            with session_scope() as session:
                out = q.reap(session, settings)
            if any(out.values()):  # judgements: requeued, gave_up; problem checks: checks_* (only when not zero)
                log.warning(" ".join(f"{k}=%d" for k in out), *out.values())
        except Exception:  # noqa: BLE001 - the loop must survive a database hiccup
            log.exception("reaper pass failed")
        if time.monotonic() - last_prune >= PRUNE_EVERY_S:
            last_prune = time.monotonic()
            try:
                with session_scope() as session:
                    pruned = cleanup.prune(session, all_settings.auth, datetime.now(timezone.utc))
                if any(pruned.values()):
                    log.info("pruned auth rows: %s", pruned)
            except Exception:  # noqa: BLE001
                log.exception("auth prune failed")
        stop.wait(settings.reaper_interval_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
