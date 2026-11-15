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
    # checks of problem versions (4a.4): taken only when no submission waits, at most this many at once
    max_concurrent_checks: int = 1  # JUDGE_MAX_CONCURRENT_CHECKS
    check_timeout_s: float = 600.0  # JUDGE_CHECK_TIMEOUT_SEC: a longer check ends failed (reason "timeout")


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
    # JUDGE_IMPORT_MAX_RATIO: unpacked / compressed, per file and in total; 0 = off (the default). Deflate tops out
    # near 1030 and legitimate monotonous tests reach it, so the unpacked total above is the protection.
    max_ratio: int = 0


@dataclass(frozen=True)
class FileSettings:
    """Files and links of the class feed (4v.3, docs/CLASSROOM.md §10): limits of a hand-in, the student's quota,
    a teacher's attachments. The defaults are classroom/attachment_rules.py's (a test compares)."""

    submission_files_max: int = 10  # JUDGE_SUBMISSION_FILES_MAX: files and links of one hand-in (answer.md apart)
    submission_file_max_bytes: int = 20 * 1024 * 1024  # JUDGE_SUBMISSION_FILE_MAX_MB
    submission_total_max_bytes: int = 50 * 1024 * 1024  # JUDGE_SUBMISSION_TOTAL_MAX_MB: the files of one hand-in
    student_quota_bytes: int = 200 * 1024 * 1024  # JUDGE_STUDENT_QUOTA_MB: distinct blobs of a student; 0 = none
    item_attachments_max: int = 20  # JUDGE_ITEM_ATTACHMENTS_MAX: files and links of one feed item
    attachment_max_bytes: int = 50 * 1024 * 1024  # JUDGE_ATTACHMENT_MAX_MB: one file of a teacher


@dataclass(frozen=True)
class SubmitSettings:
    """Submissions of students (4b.1, D-091): sizes checked by the server before anything is stored, and rates. The
    sizes cannot exceed what the runner takes (runner/config.py; app.submissions.intake.RUNNER_*), which keeps its
    own limits as a safety net."""

    source_max_bytes: int = 256 * 1024  # JUDGE_SOURCE_MAX_KB: one source file
    archive_max_bytes: int = 1024 * 1024  # JUDGE_ARCHIVE_MAX_KB: an archive (and never above max_archive_kb of a problem)
    rate_per_min: int = 6  # JUDGE_SUBMIT_RATE_PER_MIN: submissions of one student per minute, all contests; 0 = none
    enter_rate: int = 10  # JUDGE_ENTER_RATE: attempts to enter a contest (codes) per 10 minutes of one student


@dataclass(frozen=True)
class Settings:
    database_url: str = "postgresql+psycopg://judge:judge@127.0.0.1:5432/judge"
    blob_dir: str = "/var/lib/judge/blobs"
    queue: QueueSettings = QueueSettings()
    auth: AuthSettings = AuthSettings()
    imports: ImportSettings = ImportSettings()
    files: FileSettings = FileSettings()
    submit: SubmitSettings = SubmitSettings()
    metrics_token: Optional[str] = None  # if set, /internal/metrics needs "Authorization: Bearer <it>"
    public_origin: Optional[str] = None  # JUDGE_PUBLIC_ORIGIN, e.g. https://judge.example.ru: the only Origin accepted
    public_port: int = 8000  # the public app (app.public_main), proxied by Caddy
    internal_port: int = 8001  # the internal worker API; must match --port of judge-api-internal.service
    timezone: str = "UTC"  # JUDGE_TIMEZONE: judgectl tables and contest.yaml times without an offset; the API is UTC
    assignment_text_max_bytes: int = 64 * 1024  # JUDGE_ASSIGNMENT_TEXT_MAX_KB: the text answer of a manual assignment
    comment_rate_per_min: int = 10  # JUDGE_COMMENT_RATE_PER_MIN: comments of one student per minute; 0 = no limit

    @classmethod
    def from_env(cls, env: Optional[Mapping] = None) -> "Settings":
        env = os.environ if env is None else env
        d, q, a, i, f, sb = cls(), QueueSettings(), AuthSettings(), ImportSettings(), FileSettings(), SubmitSettings()
        mib = 1024 * 1024
        rate = int(env.get("JUDGE_COMMENT_RATE_PER_MIN", d.comment_rate_per_min))
        if rate < 0:
            raise ValueError("JUDGE_COMMENT_RATE_PER_MIN is 0 (no limit) or more")

        def positive(name: str, default: int, unit: int = 1, zero_ok: bool = False) -> int:
            value = int(env.get(name, default // unit))
            if value < 0 or (value == 0 and not zero_ok):
                raise ValueError(f"{name} is {'0 (no limit) or more' if zero_ok else 'at least 1'}")
            return value * unit

        def at_most(name: str, value: int, cap: int) -> int:
            if value > cap:
                raise ValueError(f"{name} is at most {cap // 1024} (the runner takes no more)")
            return value
        from .submissions.intake import RUNNER_ARCHIVE_MAX_BYTES, RUNNER_SOURCE_MAX_BYTES
        return cls(
            database_url=env.get("DATABASE_URL", d.database_url),
            blob_dir=env.get("JUDGE_BLOB_DIR", d.blob_dir),
            metrics_token=env.get("JUDGE_METRICS_TOKEN") or None,
            public_origin=env.get("JUDGE_PUBLIC_ORIGIN") or None,
            public_port=int(env.get("JUDGE_PUBLIC_PORT", d.public_port)),
            internal_port=int(env.get("JUDGE_INTERNAL_PORT", d.internal_port)),
            timezone=env.get("JUDGE_TIMEZONE") or d.timezone,
            assignment_text_max_bytes=int(env.get("JUDGE_ASSIGNMENT_TEXT_MAX_KB",
                                                  d.assignment_text_max_bytes // 1024)) * 1024,
            comment_rate_per_min=rate,
            files=FileSettings(
                submission_files_max=positive("JUDGE_SUBMISSION_FILES_MAX", f.submission_files_max),
                submission_file_max_bytes=positive("JUDGE_SUBMISSION_FILE_MAX_MB", f.submission_file_max_bytes, mib),
                submission_total_max_bytes=positive("JUDGE_SUBMISSION_TOTAL_MAX_MB", f.submission_total_max_bytes,
                                                    mib),
                student_quota_bytes=positive("JUDGE_STUDENT_QUOTA_MB", f.student_quota_bytes, mib, zero_ok=True),
                item_attachments_max=positive("JUDGE_ITEM_ATTACHMENTS_MAX", f.item_attachments_max),
                attachment_max_bytes=positive("JUDGE_ATTACHMENT_MAX_MB", f.attachment_max_bytes, mib),
            ),
            submit=SubmitSettings(
                source_max_bytes=at_most("JUDGE_SOURCE_MAX_KB", positive("JUDGE_SOURCE_MAX_KB", sb.source_max_bytes,
                                                                          1024), RUNNER_SOURCE_MAX_BYTES),
                archive_max_bytes=at_most("JUDGE_ARCHIVE_MAX_KB", positive("JUDGE_ARCHIVE_MAX_KB",
                                                                            sb.archive_max_bytes, 1024),
                                          RUNNER_ARCHIVE_MAX_BYTES),
                rate_per_min=positive("JUDGE_SUBMIT_RATE_PER_MIN", sb.rate_per_min, zero_ok=True),
                enter_rate=positive("JUDGE_ENTER_RATE", sb.enter_rate),
            ),
            queue=QueueSettings(
                heartbeat_timeout_s=float(env.get("JUDGE_HEARTBEAT_TIMEOUT_S", q.heartbeat_timeout_s)),
                max_attempts=int(env.get("JUDGE_MAX_ATTEMPTS", q.max_attempts)),
                max_judging_s=float(env.get("JUDGE_MAX_JUDGING_S", q.max_judging_s)),
                recent_window_s=float(env.get("JUDGE_FAIR_WINDOW_S", q.recent_window_s)),
                event_retention_days=int(env.get("JUDGE_EVENT_RETENTION_DAYS", q.event_retention_days)),
                reaper_interval_s=float(env.get("JUDGE_REAPER_INTERVAL_S", q.reaper_interval_s)),
                max_concurrent_checks=int(env.get("JUDGE_MAX_CONCURRENT_CHECKS", q.max_concurrent_checks)),
                check_timeout_s=float(env.get("JUDGE_CHECK_TIMEOUT_SEC", q.check_timeout_s)),
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
