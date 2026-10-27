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
class ImportSettings:
    """Limits of a problem package (R27): checked while unpacking, never trusting the sizes in zip headers."""

    max_archive_bytes: int = 100 * 1024 * 1024  # JUDGE_IMPORT_MAX_ARCHIVE_MB
    max_unpacked_bytes: int = 512 * 1024 * 1024  # JUDGE_IMPORT_MAX_UNPACKED_MB
    max_files: int = 5000  # JUDGE_IMPORT_MAX_FILES
    # JUDGE_IMPORT_MAX_RATIO: unpacked / compressed, per file and in total. Only a second line: deflate itself tops
    # out near 1030, legitimate monotonous tests reach 500-1000; the unpacked total above is the real protection.
    max_ratio: int = 1000


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://judge:judge@127.0.0.1:5432/judge"
    blob_dir: str = "/var/lib/judge/blobs"
    queue: QueueSettings = QueueSettings()
    auth: AuthSettings = AuthSettings()
    imports: ImportSettings = ImportSettings()
    metrics_token: Optional[str] = None  # if set, /internal/metrics needs "Authorization: Bearer <it>"
    public_origin: Optional[str] = None  # JUDGE_PUBLIC_ORIGIN, e.g. https://judge.example.ru: the only Origin accepted
    public_port: int = 8000  # the public app (app.public_main), proxied by Caddy
    internal_port: int = 8001  # the internal worker API; must match --port of judge-api-internal.service
    timezone: str = "UTC"  # JUDGE_TIMEZONE: judgectl tables and contest.yaml times without an offset; the API is UTC

    @classmethod
    def from_env(cls, env: Optional[Mapping] = None) -> "Settings":
        env = os.environ if env is None else env
        d, q, a, i = cls(), QueueSettings(), AuthSettings(), ImportSettings()
        mib = 1024 * 1024
        return cls(
            database_url=env.get("DATABASE_URL", d.database_url),
            blob_dir=env.get("JUDGE_BLOB_DIR", d.blob_dir),
            metrics_token=env.get("JUDGE_METRICS_TOKEN") or None,
            public_origin=env.get("JUDGE_PUBLIC_ORIGIN") or None,
            public_port=int(env.get("JUDGE_PUBLIC_PORT", d.public_port)),
            internal_port=int(env.get("JUDGE_INTERNAL_PORT", d.internal_port)),
            timezone=env.get("JUDGE_TIMEZONE") or d.timezone,
            queue=QueueSettings(
                heartbeat_timeout_s=float(env.get("JUDGE_HEARTBEAT_TIMEOUT_S", q.heartbeat_timeout_s)),
                max_attempts=int(env.get("JUDGE_MAX_ATTEMPTS", q.max_attempts)),
                max_judging_s=float(env.get("JUDGE_MAX_JUDGING_S", q.max_judging_s)),
                recent_window_s=float(env.get("JUDGE_FAIR_WINDOW_S", q.recent_window_s)),
                event_retention_days=int(env.get("JUDGE_EVENT_RETENTION_DAYS", q.event_retention_days)),
                reaper_interval_s=float(env.get("JUDGE_REAPER_INTERVAL_S", q.reaper_interval_s)),
            ),
            imports=ImportSettings(
                max_archive_bytes=int(env.get("JUDGE_IMPORT_MAX_ARCHIVE_MB", i.max_archive_bytes // mib)) * mib,
                max_unpacked_bytes=int(env.get("JUDGE_IMPORT_MAX_UNPACKED_MB", i.max_unpacked_bytes // mib)) * mib,
                max_files=int(env.get("JUDGE_IMPORT_MAX_FILES", i.max_files)),
                max_ratio=int(env.get("JUDGE_IMPORT_MAX_RATIO", i.max_ratio)),
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
