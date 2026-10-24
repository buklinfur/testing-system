"""Pure functions of the queue: fair ordering (reference implementation) and the wait estimate.

The claim query in queue.py implements `fair_key` in SQL; tests/pg checks that both agree.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

DEFAULT_JUDGE_SECONDS = 5.0


def fair_key(row: dict, inflight: dict, recent: dict) -> tuple:
    """Order in which queued judgements are handed out.

    1. priority (lower first): fresh submissions before rejudges;
    2. how many judgements of the same student are running right now (fewer first): one student
       cannot occupy all workers while others wait;
    3. how many of the student's judgements started in the last few minutes (fewer first): a student who
       keeps sending submissions yields to those who sent nothing recently;
    4. age (created_at, then id).
    Work-conserving: with a single student in the queue, every free worker still takes their job.
    """
    user = row["user_id"]
    return (row["priority"], inflight.get(user, 0), recent.get(user, 0), row["created_at"], row["id"])


def fair_order(queued: Iterable[dict], inflight: dict, recent: dict) -> list:
    return sorted(queued, key=lambda r: fair_key(r, inflight, recent))


def typical_judge_seconds(durations: Sequence[float]) -> float:
    """Median of recent judging times; a default before there is any data."""
    clean = [d for d in durations if d is not None and d >= 0]
    return statistics.median(clean) if clean else DEFAULT_JUDGE_SECONDS


def estimate_wait_seconds(position: int, workers_online: int, workers_busy: int, typical_s: float) -> Optional[float]:
    """Expected seconds until judging of a queued job *starts*.

    position: 1-based place in the fair order. None = no workers online (cannot estimate).
    Free workers take the first jobs at once; after that jobs go in waves of `workers_online`, and the
    first wave waits for the earliest running job to finish (about half a typical duration on average).
    """
    if workers_online <= 0:
        return None
    free = max(0, workers_online - workers_busy)
    ahead = max(0, position - 1)
    if ahead < free:
        return 0.0
    waves = (ahead - free) // workers_online
    return typical_s / 2 + typical_s * waves


@dataclass
class LoadSnapshot:
    """What students see on the load indicator (module 4 returns it from the public API)."""

    queue_length: int
    workers_online: int
    workers_busy: int
    typical_seconds: float
    my_position: Optional[int] = None
    my_wait_seconds: Optional[float] = None

    @property
    def level(self) -> str:
        if self.workers_online == 0:
            return "offline"
        if self.queue_length == 0 and self.workers_busy < self.workers_online:
            return "free"
        if self.queue_length <= self.workers_online:
            return "busy"
        return "overloaded"

    def to_dict(self) -> dict:
        return {
            "level": self.level, "queue_length": self.queue_length, "workers_online": self.workers_online,
            "workers_busy": self.workers_busy, "typical_seconds": round(self.typical_seconds, 1),
            "my_position": self.my_position,
            "my_wait_seconds": None if self.my_wait_seconds is None else int(math.ceil(self.my_wait_seconds)),
        }
