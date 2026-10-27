"""Database models (SQLAlchemy 2.0, PostgreSQL only).

Key ideas (see docs/PROBLEM_FORMAT.md and docs/DATA_MODEL.md):

* A published ProblemVersion is immutable. Submissions always point to a concrete
  version through ContestProblem, so editing tests never changes old results.
* A Submission is what the student sent. A Judgement is one attempt to check it.
  Rejudging adds a new Judgement; exactly one Judgement per submission is current.
* The queue is the set of Judgements with status = 'queued'
  (workers claim rows with SELECT ... FOR UPDATE SKIP LOCKED, or via the API).
* Large files (tests, sources, archives) live in a content-addressed store on disk,
  the database keeps only their sha256.
"""
from __future__ import annotations

import enum
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Interval,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# --------------------------------------------------------------------------- enums
class Role(str, enum.Enum):
    admin = "admin"  # every administrator (teachers included) has the same rights in the web UI
    student = "student"


class SessionState(str, enum.Enum):
    mfa_pending = "mfa_pending"  # admin passed the password, TOTP not yet: allows nothing but /totp and /logout
    active = "active"


class AuthFailureKind(str, enum.Enum):
    password = "password"
    totp = "totp"
    qr = "qr"


class VersionStatus(str, enum.Enum):
    """Every version is immutable from its import on; the status only says whether it may be used (R30)."""

    pending_check = "pending_check"  # imported, the check by a worker (session 4a.4) has not finished
    ready = "ready"  # may be pinned in contests
    failed = "failed"  # the check found a problem (check_report); never pinned


class ProblemType(str, enum.Enum):
    batch = "batch"
    interactive = "interactive"
    output_only = "output_only"
    choice = "choice"
    text_answer = "text_answer"


class Scoring(str, enum.Enum):
    sum = "sum"
    all_or_nothing = "all_or_nothing"
    min = "min"


class Visibility(str, enum.Enum):
    open = "open"
    hidden = "hidden"


class Feedback(str, enum.Enum):
    full = "full"
    first_fail = "first_fail"
    groups_only = "groups_only"
    verdict_only = "verdict_only"


class ProgramRole(str, enum.Enum):
    checker = "checker"
    interactor = "interactor"
    validator = "validator"
    generator = "generator"
    solution = "solution"


class TimingMode(str, enum.Enum):
    fixed = "fixed"  # one window [starts_at, ends_at] for everybody
    personal = "personal"  # each participant presses "start" inside the window and gets personal_duration


class LeaderboardMode(str, enum.Enum):
    hidden = "hidden"
    visible = "visible"
    after_end = "after_end"  # after ends_at


class ContestState(str, enum.Enum):
    draft = "draft"  # admins only
    published = "published"
    archived = "archived"


class JoinPolicy(str, enum.Enum):
    open = "open"  # any student joins with a button
    code = "code"  # with invite_code (or the link /join/<code>)


class ScoringMode(str, enum.Enum):
    points = "points"  # IOI style: sum of points, ties by time of last improvement
    icpc = "icpc"  # number of solved problems and penalty time


class JudgementStatus(str, enum.Enum):
    queued = "queued"
    compiling = "compiling"
    running = "running"
    finished = "finished"
    internal_error = "internal_error"


class Verdict(str, enum.Enum):
    OK = "OK"
    WA = "WA"  # wrong answer
    PE = "PE"  # presentation error
    TL = "TL"  # time limit
    ML = "ML"  # memory limit
    RE = "RE"  # runtime error
    CE = "CE"  # compilation error
    OL = "OL"  # output limit exceeded
    PARTIAL = "PARTIAL"  # some points, not all (submission level)
    SKIPPED = "SKIPPED"  # not run: a dependency group failed
    IE = "IE"  # internal error of the judging system


class JudgementReason(str, enum.Enum):
    initial = "initial"
    rejudge = "rejudge"


def pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


# Enums used by several columns are created once, so Alembic emits a single CREATE TYPE.
VERDICT_T = pg_enum(Verdict, "verdict")
VISIBILITY_T = pg_enum(Visibility, "visibility")
FEEDBACK_T = pg_enum(Feedback, "feedback")


def utcnow_column() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# --------------------------------------------------------------------------- users
LOGIN_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,31}$"  # lower case only: input is trimmed and lowered before it gets here


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # argon2id
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)  # the only name other students see
    role: Mapped[Role] = mapped_column(pg_enum(Role, "role"), nullable=False, default=Role.student)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utcnow_column()
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(45))
    totp_secret_enc: Mapped[Optional[str]] = mapped_column(Text)  # Fernet(JUDGE_SECRET_KEY), admins only
    totp_enabled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # set once a code confirmed it
    totp_last_step: Mapped[Optional[int]] = mapped_column(BigInteger)  # last accepted 30 s step: a code works once

    __table_args__ = (CheckConstraint(f"login ~ '{LOGIN_PATTERN}'", name="login_format"),)


class UserSession(Base):
    """Server-side login session. The cookie holds a random token; only its sha256 is stored."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[SessionState] = mapped_column(pg_enum(SessionState, "session_state"), nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()
    last_seen_at: Mapped[datetime] = utcnow_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)  # sliding (idle timeout)
    absolute_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # admins: hard cap
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(256))  # truncated
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_sessions_user_live", "user_id", postgresql_where=text("revoked_at IS NULL")),)


class AuthFailure(Base):
    """One failed login step. Brute-force limits count these; the reaper prunes old rows."""

    __tablename__ = "auth_failures"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[Optional[str]] = mapped_column(String(64))  # as typed (normalised); null for QR attempts
    ip: Mapped[str] = mapped_column(String(45), nullable=False)
    kind: Mapped[AuthFailureKind] = mapped_column(pg_enum(AuthFailureKind, "auth_failure_kind"), nullable=False)
    at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        Index("ix_auth_failures_login_at", "login", "at"),
        Index("ix_auth_failures_ip_at", "ip", "at"),
    )


class AuthLockout(Base):
    """A login or an IP blocked until `until`. Unlocking sets `cleared_at`; failures before it no longer count."""

    __tablename__ = "auth_lockouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[Optional[str]] = mapped_column(String(64))
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()
    cleared_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cleared_by: Mapped[Optional[str]] = mapped_column(String(64))  # "user:<id>", "cli:<unix user>", "password_reset"

    __table_args__ = (
        CheckConstraint("(login IS NULL) <> (ip IS NULL)", name="one_subject"),
        Index("ix_auth_lockouts_login_until", "login", "until"),
        Index("ix_auth_lockouts_ip_until", "ip", "until"),
    )


class LoginToken(Base):
    """One-time QR login token of a student (sha256 stored). A new token revokes the previous ones."""

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_login_tokens_user_live", "user_id", postgresql_where=text("used_at IS NULL AND revoked_at IS NULL")),
    )


# --------------------------------------------------------------------------- blobs
class Blob(Base):
    """Content-addressed file. Bytes are stored on disk under a path derived from sha256."""

    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = utcnow_column()


# --------------------------------------------------------------------------- languages
class Language(Base):
    """Language profile. Adding a language means adding a row, not changing code."""

    __tablename__ = "languages"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # python, c, cpp
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # {"extensions": [".cpp"], "compile": ["g++", "-O2", ...], "run": ["{exe}"],
    #  "time_multiplier": 1.0, "memory_multiplier": 1.0, "compile_limits": {...}}
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


# --------------------------------------------------------------------------- problems
class Problem(Base):
    __tablename__ = "problems"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: imported from the console
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = utcnow_column()

    versions: Mapped[list[ProblemVersion]] = relationship(back_populates="problem", order_by="ProblemVersion.version")


class ProblemVersion(Base):
    __tablename__ = "problem_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        pg_enum(VersionStatus, "version_status"), nullable=False, default=VersionStatus.pending_check
    )
    type: Mapped[ProblemType] = mapped_column(pg_enum(ProblemType, "problem_type"), nullable=False)
    titles: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)  # {"ru": "..."}
    statements: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)  # {"ru": "markdown"}
    # normalized problem.yaml (limits, io, checker, feedback, questions, answers, ...)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    package_sha256: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.sha256"))  # the uploaded zip, if any
    # digest of the worker manifest (protocol/manifest.py), the worker's cache key; a version never changes,
    # so it is stored once
    manifest_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    # identity of the content (R29): sha256 over the sorted "path + sha256" of every package file, junk excluded.
    # Null for versions made by dev_seed before session 4a.2 (no file list).
    content_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    files: Mapped[Optional[dict[str, str]]] = mapped_column(JSONB)  # {path: sha256} of every package file
    imported_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: console or dev_seed
    check_report: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)  # the worker check (session 4a.4)
    created_at: Mapped[datetime] = utcnow_column()  # = imported at

    problem: Mapped[Problem] = relationship(back_populates="versions")
    groups: Mapped[list[TestGroup]] = relationship(
        back_populates="version", order_by="TestGroup.position", cascade="all, delete-orphan"
    )
    programs: Mapped[list[Program]] = relationship(back_populates="version", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("problem_id", "version", name="uq_problem_versions_problem_version"),
        UniqueConstraint("problem_id", "content_sha256", name="uq_problem_versions_problem_content"),
    )


class TestGroup(Base):
    __tablename__ = "test_groups"
    __test__ = False  # not a pytest class

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    scoring: Mapped[Scoring] = mapped_column(pg_enum(Scoring, "scoring"), nullable=False, default=Scoring.sum)
    visibility: Mapped[Visibility] = mapped_column(
        VISIBILITY_T, nullable=False, default=Visibility.hidden
    )
    feedback: Mapped[Optional[Feedback]] = mapped_column(FEEDBACK_T)  # overrides problem level
    show_in_statement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    depends_on: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)  # group names

    version: Mapped[ProblemVersion] = relationship(back_populates="groups")
    tests: Mapped[list[TestCase]] = relationship(
        back_populates="group", order_by="TestCase.position", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("version_id", "name", name="uq_test_groups_version_name"),)


class TestCase(Base):
    __tablename__ = "test_cases"
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("test_groups.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)  # file stem, e.g. "small03"
    input_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"), nullable=False)
    answer_sha256: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.sha256"))  # absent for custom checkers
    visibility: Mapped[Optional[Visibility]] = mapped_column(VISIBILITY_T)  # overrides group

    group: Mapped[TestGroup] = relationship(back_populates="tests")

    __table_args__ = (UniqueConstraint("group_id", "position", name="uq_test_cases_group_position"),)


class Program(Base):
    """Jury program attached to a problem version: checker, interactor, validator, generator, solution."""

    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[ProgramRole] = mapped_column(pg_enum(ProgramRole, "program_role"), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    language_key: Mapped[str] = mapped_column(ForeignKey("languages.key"), nullable=False)
    source_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256"), nullable=False)
    expected_verdict: Mapped[Optional[Verdict]] = mapped_column(VERDICT_T)  # for solutions

    version: Mapped[ProblemVersion] = relationship(back_populates="programs")


# --------------------------------------------------------------------------- contests
class Contest(Base):
    """A contest (R19-R21). A practice archive is a contest without ends_at. `state` and `invite_code` are managed
    by commands, not by contest.yaml (docs/CONTESTS.md)."""

    __tablename__ = "contests"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[ContestState] = mapped_column(pg_enum(ContestState, "contest_state"), nullable=False,
                                                default=ContestState.draft)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # null: no end (practice)
    timing_mode: Mapped[TimingMode] = mapped_column(pg_enum(TimingMode, "timing_mode"), nullable=False,
                                                    default=TimingMode.fixed)
    personal_duration: Mapped[Optional[timedelta]] = mapped_column(Interval)  # only with timing_mode = personal
    freeze_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # leaderboard freeze; not personal
    scoring_mode: Mapped[ScoringMode] = mapped_column(
        pg_enum(ScoringMode, "scoring_mode"), nullable=False, default=ScoringMode.points
    )
    icpc_penalty_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    leaderboard: Mapped[LeaderboardMode] = mapped_column(pg_enum(LeaderboardMode, "leaderboard_mode"), nullable=False,
                                                         default=LeaderboardMode.hidden)
    join_policy: Mapped[JoinPolicy] = mapped_column(pg_enum(JoinPolicy, "join_policy"), nullable=False,
                                                    default=JoinPolicy.code)
    invite_code: Mapped[Optional[str]] = mapped_column(String(32), unique=True)
    allow_upsolving: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    languages: Mapped[Optional[list[str]]] = mapped_column(JSONB)  # null: every enabled language
    submit_cooldown_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    max_submissions_per_problem: Mapped[Optional[int]] = mapped_column(Integer)
    tests_after_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # show all tests after end
    plagiarism_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # reserved for a hook
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: created from the console
    created_at: Mapped[datetime] = utcnow_column()

    problems: Mapped[list[ContestProblem]] = relationship(
        back_populates="contest", order_by="ContestProblem.position", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("(timing_mode = 'personal') = (personal_duration IS NOT NULL)", name="personal_duration"),
        CheckConstraint("personal_duration IS NULL OR personal_duration > interval '0'",
                        name="personal_duration_positive"),
        CheckConstraint("timing_mode <> 'personal' OR freeze_at IS NULL", name="no_freeze_with_personal"),
        CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="window"),
        CheckConstraint("join_policy <> 'code' OR invite_code IS NOT NULL", name="invite_code"),
        CheckConstraint("submit_cooldown_sec >= 0 AND icpc_penalty_minutes >= 0", name="non_negative"),
        CheckConstraint("max_submissions_per_problem IS NULL OR max_submissions_per_problem > 0",
                        name="max_submissions"),
    )


class ContestProblem(Base):
    """A problem inside a contest, pinned to a concrete version (R22).

    It is identified by the problem; the label is an attribute that may change. A problem removed from
    contest.yaml while it has submissions stays as a hidden row (students do not see it, the leaderboard
    ignores it); uniqueness of the problem and of the label holds among the rows that are not hidden.
    """

    __tablename__ = "contest_problems"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), nullable=False)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), nullable=False)
    problem_version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(8), nullable=False)  # A, B, C ...
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))  # overrides the problem total; groups scale
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))

    contest: Mapped[Contest] = relationship(back_populates="problems")
    problem_version: Mapped[ProblemVersion] = relationship()

    __table_args__ = (
        Index("uq_contest_problems_contest_label", "contest_id", "label", unique=True,
              postgresql_where=text("NOT hidden")),
        Index("uq_contest_problems_contest_problem", "contest_id", "problem_id", unique=True,
              postgresql_where=text("NOT hidden")),
        CheckConstraint("points IS NULL OR points > 0", name="points_positive"),
    )


class Participation(Base):
    __tablename__ = "participations"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    joined_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # personal timing: "start"

    __table_args__ = (UniqueConstraint("contest_id", "user_id", name="uq_participations_contest_user"),)


# --------------------------------------------------------------------------- submissions
class Submission(Base):
    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    contest_problem_id: Mapped[int] = mapped_column(ForeignKey("contest_problems.id"), nullable=False)
    language_key: Mapped[Optional[str]] = mapped_column(ForeignKey("languages.key"))  # null for choice/text_answer
    source_sha256: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.sha256"))
    is_archive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entry_file: Mapped[Optional[str]] = mapped_column(String(255))
    answers: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)  # for choice / text_answer
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    created_at: Mapped[datetime] = utcnow_column()

    judgements: Mapped[list[Judgement]] = relationship(back_populates="submission", order_by="Judgement.id")

    __table_args__ = (
        Index("ix_submissions_user_created", "user_id", "created_at"),
        Index("ix_submissions_contest_problem_user_created", "contest_problem_id", "user_id", "created_at"),
    )


class Judgement(Base):
    """One attempt to check a submission (module 3: queue semantics).

    * status: queued -> compiling -> running -> finished | internal_error. The queue is `status = 'queued'`.
    * attempt: how many times a worker claimed it; it is the fencing token of a lease, a result is
      accepted only from the worker and attempt that currently own the row.
    * failures: attempts that went wrong (worker lost, internal error); at max_attempts the judgement
      ends as internal_error instead of looping forever on a "poison" submission.
    * priority: lower goes first (fresh submissions 0, rejudges 10).
    * requires: capabilities a worker must have (["cpu"], later ["gpu"]).
    * is_current: the judgement the student sees. A rejudge starts as is_current = false and takes over
      only when it finishes without an internal error, so the old verdict stays visible meanwhile.
    """

    __tablename__ = "judgements"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False)
    problem_version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id"), nullable=False)
    reason: Mapped[JudgementReason] = mapped_column(
        pg_enum(JudgementReason, "judgement_reason"), nullable=False, default=JudgementReason.initial
    )
    status: Mapped[JudgementStatus] = mapped_column(
        pg_enum(JudgementStatus, "judgement_status"), nullable=False, default=JudgementStatus.queued
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requires: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["cpu"])
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verdict: Mapped[Optional[Verdict]] = mapped_column(VERDICT_T)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    compile_log: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)  # internal error text: teachers only
    summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)  # {"groups": [...], "first_fail": ...} / {"quiz": ...}
    tests_done: Mapped[Optional[int]] = mapped_column(Integer)  # live progress while running
    tests_total: Mapped[Optional[int]] = mapped_column(Integer)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id"))
    requested_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # who asked for a rejudge
    created_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    submission: Mapped[Submission] = relationship(back_populates="judgements")
    results: Mapped[list[TestResult]] = relationship(
        back_populates="judgement", order_by="TestResult.id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # the queue: only rows waiting for a worker, in claim order
        Index("ix_judgements_queue", "priority", "created_at", postgresql_where=text("status = 'queued'")),
        # rows currently held by a worker (reaper, load counters)
        Index("ix_judgements_inflight", "worker_id", postgresql_where=text("status IN ('compiling', 'running')")),
        # at most one current judgement per submission
        Index("uq_judgements_current", "submission_id", unique=True, postgresql_where=text("is_current")),
        # at most one unfinished judgement per submission (no double queueing, rejudge never races itself)
        Index(
            "uq_judgements_active",
            "submission_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'compiling', 'running')"),
        ),
    )


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False

    id: Mapped[int] = mapped_column(primary_key=True)
    judgement_id: Mapped[int] = mapped_column(ForeignKey("judgements.id", ondelete="CASCADE"), nullable=False)
    test_case_id: Mapped[int] = mapped_column(ForeignKey("test_cases.id"), nullable=False)
    verdict: Mapped[Verdict] = mapped_column(VERDICT_T, nullable=False)
    time_ms: Mapped[Optional[int]] = mapped_column(Integer)
    memory_kb: Mapped[Optional[int]] = mapped_column(Integer)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer)
    signal: Mapped[Optional[int]] = mapped_column(Integer)
    tl_kind: Mapped[Optional[str]] = mapped_column(String(8))  # TL only: "cpu" or "wall" (validated by the worker API)
    stderr_excerpt: Mapped[Optional[str]] = mapped_column(Text)  # teachers only (<= 4 KB)
    output_excerpt: Mapped[Optional[str]] = mapped_column(Text)  # the program's own output, teachers / open tests
    checker_message: Mapped[Optional[str]] = mapped_column(Text)  # always stored; shown per feedback policy
    visible_to_student: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    judgement: Mapped[Judgement] = relationship(back_populates="results")

    __table_args__ = (UniqueConstraint("judgement_id", "test_case_id", name="uq_test_results_judgement_test"),)


# --------------------------------------------------------------------------- infrastructure
class WorkerToken(Base):
    """Credential of a machine (or pool) of workers. Only sha256(token) is stored; tokens are 256-bit random."""

    __tablename__ = "worker_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["cpu"])  # allowed maximum
    created_at: Mapped[datetime] = utcnow_column()
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Worker(Base):
    """One worker process. Created by /register (upsert by name); `instance_id` changes at every start."""

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_id: Mapped[int] = mapped_column(ForeignKey("worker_tokens.id"), nullable=False)
    instance_id: Mapped[Optional[str]] = mapped_column(String(36))
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["cpu"])  # e.g. ["cpu", "gpu"]
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="idle")  # idle | busy | draining | stopped
    info: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # pid, cpus, boxes, backend
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)  # jobs done, cache hits, rss
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class QueueEvent(Base):
    """Short history of queue incidents: worker lost, internal error, rejudge, registration. Pruned by the reaper."""

    __tablename__ = "queue_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = utcnow_column()
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id", ondelete="SET NULL"))
    judgement_id: Mapped[Optional[int]] = mapped_column(ForeignKey("judgements.id", ondelete="SET NULL"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (Index("ix_queue_events_at", "at"),)


class AuditLog(Base):
    """Every administrative action, from the web (actor_user_id) or the console (actor_cli), and admin logins."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = utcnow_column()
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    actor_cli: Mapped[Optional[str]] = mapped_column(String(64))  # "cli:<unix user>"
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # user.create, user.reset_password, auth.login ...
    object_type: Mapped[Optional[str]] = mapped_column(String(32))  # user, contest, problem, submission ...
    object_id: Mapped[Optional[str]] = mapped_column(String(64))
    ip: Mapped[Optional[str]] = mapped_column(String(45))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_audit_log_actor_at", "actor_user_id", "at"),
        Index("ix_audit_log_object", "object_type", "object_id", "at"),
        Index("ix_audit_log_at", "at"),
    )
