"""Worker settings. Everything comes from environment variables (systemd EnvironmentFile)."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from runner.config import MANUAL_BOX_BASE


def _int(env: Mapping, key: str, default: int) -> int:
    value = env.get(key)
    return default if value in (None, "") else int(value)


def _float(env: Mapping, key: str, default: float) -> float:
    value = env.get(key)
    return default if value in (None, "") else float(value)


def _bool(env: Mapping, key: str, default: bool) -> bool:
    value = env.get(key)
    return default if value in (None, "") else value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class WorkerConfig:
    api_url: str = "http://127.0.0.1:8001"
    token: str = ""
    machine: str = field(default_factory=lambda: socket.gethostname().split(".")[0])
    capabilities: tuple = ("cpu",)

    workers: int = 5  # of 8 cores: one reserved for the system, two stay free for PostgreSQL/API/WSL
    reserve_cores: int = 1
    pin_cpus: bool = True
    cpu_plan: str = ""  # explicit plan "1-2;3-4;5-6", overrides the automatic one
    box_base: int = 0  # first isolate box id of this machine; worker i uses box_base+2i and +1

    backend: str = "isolate"  # "local" is UNSAFE (tests only)
    isolate_bin: str = "isolate"
    cache_dir: Path = Path("/var/cache/judge/worker")
    max_cache_bytes: int = 2 * 1024**3
    cache_min_age_s: int = 900  # entries used more recently than this are never evicted

    poll_interval_s: float = 1.0
    heartbeat_interval_s: float = 5.0
    request_timeout_s: float = 30.0
    result_retry_s: float = 600.0  # keep retrying to deliver a finished result this long
    job_deadline_s: float = 1800.0  # abandon a job that runs longer (server has its own limit too)
    drain_timeout_s: float = 120.0  # on SIGTERM: time to finish the current job

    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Optional[Mapping] = None) -> "WorkerConfig":
        env = os.environ if env is None else env
        token = env.get("JUDGE_WORKER_TOKEN", "")
        token_file = env.get("JUDGE_WORKER_TOKEN_FILE")
        if not token and token_file:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        default = cls()
        return cls(
            api_url=env.get("JUDGE_API_URL", default.api_url).rstrip("/"),
            token=token,
            machine=env.get("JUDGE_MACHINE_NAME") or default.machine,
            capabilities=tuple(c.strip() for c in env.get("JUDGE_CAPABILITIES", "cpu").split(",") if c.strip()),
            workers=_int(env, "JUDGE_WORKERS", default.workers),
            reserve_cores=_int(env, "JUDGE_RESERVE_CORES", default.reserve_cores),
            pin_cpus=_bool(env, "JUDGE_PIN_CPUS", default.pin_cpus),
            cpu_plan=env.get("JUDGE_CPU_PLAN", ""),
            box_base=_int(env, "JUDGE_BOX_BASE", default.box_base),
            backend=env.get("JUDGE_BACKEND", default.backend),
            isolate_bin=env.get("JUDGE_ISOLATE_BIN", default.isolate_bin),
            cache_dir=Path(env.get("JUDGE_CACHE_DIR", str(default.cache_dir))),
            max_cache_bytes=_int(env, "JUDGE_CACHE_MAX_MB", default.max_cache_bytes // 1024**2) * 1024**2,
            cache_min_age_s=_int(env, "JUDGE_CACHE_MIN_AGE_S", default.cache_min_age_s),
            poll_interval_s=_float(env, "JUDGE_POLL_INTERVAL_S", default.poll_interval_s),
            heartbeat_interval_s=_float(env, "JUDGE_HEARTBEAT_INTERVAL_S", default.heartbeat_interval_s),
            request_timeout_s=_float(env, "JUDGE_REQUEST_TIMEOUT_S", default.request_timeout_s),
            result_retry_s=_float(env, "JUDGE_RESULT_RETRY_S", default.result_retry_s),
            job_deadline_s=_float(env, "JUDGE_JOB_DEADLINE_S", default.job_deadline_s),
            drain_timeout_s=_float(env, "JUDGE_DRAIN_TIMEOUT_S", default.drain_timeout_s),
            log_level=env.get("JUDGE_LOG_LEVEL", default.log_level),
        )

    def validate(self) -> None:
        if not self.token:
            raise ValueError("JUDGE_WORKER_TOKEN (or JUDGE_WORKER_TOKEN_FILE) is not set")
        if self.workers < 1:
            raise ValueError("JUDGE_WORKERS must be >= 1")
        if self.backend not in ("isolate", "local"):
            raise ValueError(f"unknown backend {self.backend!r}")
        last_box = self.box_base + 2 * self.workers - 1
        if self.box_base < MANUAL_BOX_BASE + 2 and last_box >= MANUAL_BOX_BASE:
            raise ValueError(f"isolate boxes {self.box_base}..{last_box} overlap boxes {MANUAL_BOX_BASE}..{MANUAL_BOX_BASE + 1} "
                             f"of manual runs (runner.cli): lower JUDGE_BOX_BASE / JUDGE_WORKERS")
        if self.heartbeat_interval_s <= 0 or self.poll_interval_s <= 0:
            raise ValueError("intervals must be positive")
