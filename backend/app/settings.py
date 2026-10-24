"""Backend settings from environment variables (systemd EnvironmentFile /etc/judge/backend.env)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class QueueSettings:
    heartbeat_timeout_s: float = 30.0  # no heartbeat this long: the worker is considered lost
    max_attempts: int = 3  # failed attempts (worker lost / internal error) before the judgement ends as IE
    max_judging_s: float = 1800.0  # a judgement running longer is taken away even from a live worker
    recent_window_s: float = 300.0  # the "recently started" window of the fair order
    event_retention_days: int = 14
    reaper_interval_s: float = 5.0


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://judge:judge@127.0.0.1:5432/judge"
    blob_dir: str = "/var/lib/judge/blobs"
    queue: QueueSettings = QueueSettings()
    metrics_token: Optional[str] = None  # if set, /internal/metrics needs "Authorization: Bearer <it>"

    @classmethod
    def from_env(cls, env: Optional[Mapping] = None) -> "Settings":
        env = os.environ if env is None else env
        d, q = cls(), QueueSettings()
        return cls(
            database_url=env.get("DATABASE_URL", d.database_url),
            blob_dir=env.get("JUDGE_BLOB_DIR", d.blob_dir),
            metrics_token=env.get("JUDGE_METRICS_TOKEN") or None,
            queue=QueueSettings(
                heartbeat_timeout_s=float(env.get("JUDGE_HEARTBEAT_TIMEOUT_S", q.heartbeat_timeout_s)),
                max_attempts=int(env.get("JUDGE_MAX_ATTEMPTS", q.max_attempts)),
                max_judging_s=float(env.get("JUDGE_MAX_JUDGING_S", q.max_judging_s)),
                recent_window_s=float(env.get("JUDGE_FAIR_WINDOW_S", q.recent_window_s)),
                event_retention_days=int(env.get("JUDGE_EVENT_RETENTION_DAYS", q.event_retention_days)),
                reaper_interval_s=float(env.get("JUDGE_REAPER_INTERVAL_S", q.reaper_interval_s)),
            ),
        )
