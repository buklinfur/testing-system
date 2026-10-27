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
class AuthSettings:
    """Accounts, sessions, 2FA and brute-force limits (docs/AUTH.md)."""

    secret_key: Optional[str] = None  # JUDGE_SECRET_KEY: Fernet key that encrypts TOTP secrets; required for 2FA
    totp_issuer: str = "judge"  # shown in the authenticator app
    student_idle_s: int = 30 * 86400  # student session: sliding, 30 days without activity
    admin_idle_s: int = 12 * 3600  # admin session: 12 h without activity ...
    admin_absolute_s: int = 7 * 86400  # ... and never longer than 7 days
    mfa_pending_s: int = 300  # password accepted, TOTP code not yet
    login_fail_limit: int = 10  # failures of one login within login_window_s -> lockout
    admin_login_fail_limit: int = 5
    login_window_s: int = 900
    login_lockout_s: int = 900
    ip_fail_limit: int = 300  # generous: a whole class shares one IP
    ip_window_s: int = 900
    ip_lockout_s: int = 900
    qr_token_ttl_s: int = 7 * 86400
    retention_days: int = 30  # old failures, lockouts, sessions and QR tokens are pruned by the reaper


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://judge:judge@127.0.0.1:5432/judge"
    blob_dir: str = "/var/lib/judge/blobs"
    queue: QueueSettings = QueueSettings()
    auth: AuthSettings = AuthSettings()
    metrics_token: Optional[str] = None  # if set, /internal/metrics needs "Authorization: Bearer <it>"

    @classmethod
    def from_env(cls, env: Optional[Mapping] = None) -> "Settings":
        env = os.environ if env is None else env
        d, q, a = cls(), QueueSettings(), AuthSettings()
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
            auth=AuthSettings(
                secret_key=env.get("JUDGE_SECRET_KEY") or None,
                totp_issuer=env.get("JUDGE_TOTP_ISSUER", a.totp_issuer),
                login_fail_limit=int(env.get("JUDGE_LOGIN_FAIL_LIMIT", a.login_fail_limit)),
                admin_login_fail_limit=int(env.get("JUDGE_ADMIN_LOGIN_FAIL_LIMIT", a.admin_login_fail_limit)),
                login_window_s=int(env.get("JUDGE_LOGIN_WINDOW_S", a.login_window_s)),
                login_lockout_s=int(env.get("JUDGE_LOGIN_LOCKOUT_S", a.login_lockout_s)),
                ip_fail_limit=int(env.get("JUDGE_IP_FAIL_LIMIT", a.ip_fail_limit)),
                ip_window_s=int(env.get("JUDGE_IP_WINDOW_S", a.ip_window_s)),
                ip_lockout_s=int(env.get("JUDGE_IP_LOCKOUT_S", a.ip_lockout_s)),
                retention_days=int(env.get("JUDGE_AUTH_RETENTION_DAYS", a.retention_days)),
            ),
        )
