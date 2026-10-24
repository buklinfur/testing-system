"""Reaper: returns the judgements of lost workers to the queue. Runs as its own systemd unit.

    python -m app.maintenance

Several reapers at once are harmless (SKIP LOCKED), but one is enough.
"""
from __future__ import annotations

import logging
import signal
import threading

from . import queue as q
from .db import session_scope
from .settings import Settings

log = logging.getLogger("judge.reaper")


def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s [reaper] %(message)s")
    settings = Settings.from_env().queue
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("started: heartbeat timeout %.0fs, max attempts %d", settings.heartbeat_timeout_s, settings.max_attempts)
    while not stop.is_set():
        try:
            with session_scope() as session:
                out = q.reap(session, settings)
            if out["requeued"] or out["gave_up"]:
                log.warning("requeued=%d gave_up=%d", out["requeued"], out["gave_up"])
        except Exception:  # noqa: BLE001 - the loop must survive a database hiccup
            log.exception("reaper pass failed")
        stop.wait(settings.reaper_interval_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
