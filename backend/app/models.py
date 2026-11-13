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
    failed = "failed"  # the check found a problem (check_report); new pins refused, earlier pins stay (4a.4)


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


class AccessMode(str, enum.Enum):
    """Who may enter a contest (D-021; the rules are in contests/access.py)."""

    open = "open"  # any student, with a button
    code = "code"  # with invite_code (or the link /join/<code>)
    groups = "groups"  # students currently in a group attached to the contest, without a code
    code_or_groups = "code_or_groups"  # either way


class JoinedVia(str, enum.Enum):
    """How a student first entered a contest. Only `code` keeps giving access by itself; `group` lasts while the
    student stays in an attached group, `open` while the contest is open."""

    code = "code"
    group = "group"
    open = "open"


class AfterDueStatement(str, enum.Enum):
    visible = "visible"
    hidden = "hidden"


class AfterDueSubmit(str, enum.Enum):
    no = "no"  # refused: deadline_passed
    late = "late"  # accepted and marked late
    yes = "yes"  # accepted as usual


class FeedItemKind(str, enum.Enum):
    """What a class feed item is (D-059, docs/CLASSROOM.md)."""

    contest = "contest"  # a link to an existing contest; access is the contest's own
    assignment = "assignment"  # a manual assignment: submitted by students, graded by a teacher
    material = "material"  # a material of the library given to groups and students (4v.4, material_id)


class FeedItemState(str, enum.Enum):
    draft = "draft"  # admins only
    published = "published"  # students of the audience, from available_from on
    archived = "archived"  # gone from the feed; students keep their own work


class GradingScale(str, enum.Enum):
    points = "points"  # 0..max_points
    pass_fail = "pass_fail"  # зачёт / незачёт
    none = "none"  # no mark, only the status


class AnswerKind(str, enum.Enum):
    """What a student hands in (docs/CLASSROOM.md §10: the matrix of answer kind, text, files and links)."""

    mark = "mark"  # just "done" (work in a notebook)
    text = "text"  # a text answer, stored as the file answer.md
    files = "files"  # at least one file or link, no text (4v.3)
    text_or_files = "text_or_files"  # text, files and links, at least one of them (4v.3)


class SubmissionFileKind(str, enum.Enum):
    """A file of a hand-in: the text answer or an upload (4v.3). A student's upload named answer.md is a `file` and
    does not meet the text answer."""

    text = "text"  # the text answer, the file answer.md; at most one per hand-in
    file = "file"  # a file the student uploaded


class AttachmentKind(str, enum.Enum):
    """A teacher's attachment of a feed item (4v.3): a file (a blob) or a link (data, never opened by the server)."""

    file = "file"
    link = "link"


class MaterialLibrary(str, enum.Enum):
    """Who sees a material in the library («Справочник», 4v.4, docs/CLASSROOM.md §11)."""

    none = "none"  # nobody: the material is opened only by the feed and by links
    all_students = "all_students"
    groups = "groups"  # the students of the groups in material_groups (current members of groups not archived)


class GradesRelease(str, enum.Enum):
    immediate = "immediate"  # a student sees a grade at once
    manual = "manual"  # a student sees a grade once a teacher releases it (default)


class WorkStatus(str, enum.Enum):
    """The current status of a student's work, a cache of its history (submissions and reviews). "assigned" is not
    stored: a student without a work row has not handed anything in."""

    submitted = "submitted"
    graded = "graded"
    returned = "returned"  # back for rework, the student may hand in again


class ReviewAction(str, enum.Enum):
    graded = "graded"  # a grade (score / passed / none); the current grade is the last one
    returned = "returned"  # back for rework, seen by the student at once
    released = "released"  # a graded row (release_of) is now visible to the student (grades_release = manual)


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


class CheckReason(str, enum.Enum):
    """Why a problem version is checked by a worker (session 4a.4)."""

    import_ = "import"  # the import created the version
    recheck = "recheck"  # an admin asked again (e.g. after a compiler change)


class CheckStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    done = "done"


class CheckOutcome(str, enum.Enum):
    """The verdict of a finished check; it becomes the version status."""

    ready = "ready"
    failed = "failed"


def pg_enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


# Enums used by several columns are created once, so Alembic emits a single CREATE TYPE.
VERDICT_T = pg_enum(Verdict, "verdict")
VISIBILITY_T = pg_enum(Visibility, "visibility")
FEEDBACK_T = pg_enum(Feedback, "feedback")
AFTER_DUE_STATEMENT_T = pg_enum(AfterDueStatement, "after_due_statement")  # contest problems and feed items
AFTER_DUE_SUBMIT_T = pg_enum(AfterDueSubmit, "after_due_submit")


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
    timezone: Mapped[Optional[str]] = mapped_column(String(64))  # IANA name for the UI (D-024); null: the browser's

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
    access_mode: Mapped[AccessMode] = mapped_column(pg_enum(AccessMode, "access_mode"), nullable=False,
                                                    default=AccessMode.code)
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
        CheckConstraint("access_mode NOT IN ('code', 'code_or_groups') OR invite_code IS NOT NULL",
                        name="invite_code"),
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
    # deadlines (D-023): before available_from the problem does not exist for students; after the effective due
    # moment (due_at or a later extension) the statement and submitting follow the after_due_* policies
    available_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    after_due_statement: Mapped[AfterDueStatement] = mapped_column(
        AFTER_DUE_STATEMENT_T, nullable=False, default=AfterDueStatement.visible,
        server_default=AfterDueStatement.visible.value)
    after_due_submit: Mapped[AfterDueSubmit] = mapped_column(
        AFTER_DUE_SUBMIT_T, nullable=False, default=AfterDueSubmit.late,
        server_default=AfterDueSubmit.late.value)
    note_md: Mapped[Optional[str]] = mapped_column(Text)  # the teacher's note, shown to students with access

    contest: Mapped[Contest] = relationship(back_populates="problems")
    problem_version: Mapped[ProblemVersion] = relationship()

    __table_args__ = (
        Index("uq_contest_problems_contest_label", "contest_id", "label", unique=True,
              postgresql_where=text("NOT hidden")),
        Index("uq_contest_problems_contest_problem", "contest_id", "problem_id", unique=True,
              postgresql_where=text("NOT hidden")),
        CheckConstraint("points IS NULL OR points > 0", name="points_positive"),
        CheckConstraint("available_from IS NULL OR due_at IS NULL OR available_from < due_at", name="deadline"),
    )


class DeadlineExtension(Base):
    """A later due moment of one contest problem or one class feed item (exactly one target, D-059) for one student
    or for one group (D-023). Only later than due_at; a student's effective due moment is the latest of due_at,
    their own extension and those of the groups they are in now (app/extensions.py)."""

    __tablename__ = "deadline_extensions"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_problem_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contest_problems.id", ondelete="CASCADE"))
    feed_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="RESTRICT"))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(200))
    created_at: Mapped[datetime] = utcnow_column()
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: from the console

    __table_args__ = (
        CheckConstraint("(user_id IS NULL) <> (group_id IS NULL)", name="one_subject"),
        CheckConstraint("(contest_problem_id IS NULL) <> (feed_item_id IS NULL)", name="one_target"),
        Index("uq_deadline_extensions_user", "contest_problem_id", "user_id", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        Index("uq_deadline_extensions_group", "contest_problem_id", "group_id", unique=True,
              postgresql_where=text("group_id IS NOT NULL")),
        Index("uq_deadline_extensions_item_user", "feed_item_id", "user_id", unique=True,
              postgresql_where=text("feed_item_id IS NOT NULL AND user_id IS NOT NULL")),
        Index("uq_deadline_extensions_item_group", "feed_item_id", "group_id", unique=True,
              postgresql_where=text("feed_item_id IS NOT NULL AND group_id IS NOT NULL")),
    )


class Participation(Base):
    __tablename__ = "participations"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    joined_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # personal timing: "start"
    joined_via: Mapped[JoinedVia] = mapped_column(pg_enum(JoinedVia, "joined_via"), nullable=False)

    __table_args__ = (
        UniqueConstraint("contest_id", "user_id", name="uq_participations_contest_user"),
        Index("ix_participations_user", "user_id"),  # "my contests" of a student (4b.1)
    )


# --------------------------------------------------------------------------- groups (classes, D-020)
class Group(Base):
    """A class. Groups are archived, never deleted; the name is unique (ignoring case) among groups not archived."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utcnow_column()
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: from the console

    __table_args__ = (
        Index("uq_groups_live_name", text("lower(name)"), unique=True, postgresql_where=text("archived_at IS NULL")),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    )


class GroupMember(Base):
    """A student in a group. Removal sets removed_at and keeps the row (who was in the class); adding the student
    again makes a new row. At most one current row per group and student."""

    __tablename__ = "group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    added_at: Mapped[datetime] = utcnow_column()
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    removed_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (
        Index("uq_group_members_live", "group_id", "user_id", unique=True, postgresql_where=text("removed_at IS NULL")),
        Index("ix_group_members_user_live", "user_id", postgresql_where=text("removed_at IS NULL")),
    )


class ContestGroup(Base):
    """A group a contest is addressed to (access_mode groups / code_or_groups)."""

    __tablename__ = "contest_groups"

    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), primary_key=True)
    added_at: Mapped[datetime] = utcnow_column()
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (Index("ix_contest_groups_group", "group_id"),)


# --------------------------------------------------------------------------- class feed (module 4v, D-059)
class FeedItem(Base):
    """An item of the class feed: a link to a contest, a manual assignment or a material of the library (4v.4).

    Its audience (feed_item_audience) is groups and/or single students; the feed of a group is the items addressed
    to it. available_from is the delayed publication: before it the item does not exist for students. Deadline
    columns mean the same as on contest_problems and use the same rules (app/deadline_rules.py). Contests, groups
    and users are archived, never deleted, so their foreign keys restrict. docs/CLASSROOM.md.
    """

    __tablename__ = "feed_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[FeedItemKind] = mapped_column(pg_enum(FeedItemKind, "feed_item_kind"), nullable=False)
    state: Mapped[FeedItemState] = mapped_column(pg_enum(FeedItemState, "feed_item_state"), nullable=False,
                                                 default=FeedItemState.draft)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="")  # the assignment; a note on a contest
    contest_id: Mapped[Optional[int]] = mapped_column(ForeignKey("contests.id", ondelete="RESTRICT"))
    # kind material only (4v.4); materials are archived, never deleted
    material_id: Mapped[Optional[int]] = mapped_column(ForeignKey("materials.id", ondelete="RESTRICT"))
    available_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # assignments only
    after_due_statement: Mapped[AfterDueStatement] = mapped_column(
        AFTER_DUE_STATEMENT_T, nullable=False, default=AfterDueStatement.visible,
        server_default=AfterDueStatement.visible.value)
    after_due_submit: Mapped[AfterDueSubmit] = mapped_column(
        AFTER_DUE_SUBMIT_T, nullable=False, default=AfterDueSubmit.late, server_default=AfterDueSubmit.late.value)
    # assignments only (null for the other kinds)
    grading: Mapped[Optional[GradingScale]] = mapped_column(pg_enum(GradingScale, "grading_scale"))
    max_points: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))  # grading = points only
    answer_kind: Mapped[Optional[AnswerKind]] = mapped_column(pg_enum(AnswerKind, "answer_kind"))
    grades_release: Mapped[Optional[GradesRelease]] = mapped_column(pg_enum(GradesRelease, "grades_release"))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column()
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # the first publication
    # students may write in the shared comment thread (4v.2; never on a contest item); teachers always can
    comments_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                                   server_default=text("false"))

    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="title_not_blank"),
        CheckConstraint("(kind = 'contest') = (contest_id IS NOT NULL)", name="contest_link"),
        CheckConstraint("(kind = 'material') = (material_id IS NOT NULL)", name="material_link"),
        CheckConstraint("(kind = 'assignment') = (grading IS NOT NULL) AND (grading IS NULL) = (answer_kind IS NULL) "
                        "AND (grading IS NULL) = (grades_release IS NULL)", name="assignment_settings"),
        CheckConstraint("kind = 'assignment' OR due_at IS NULL", name="due_only_assignment"),
        CheckConstraint("COALESCE(grading = 'points', false) = (max_points IS NOT NULL)", name="max_points"),
        CheckConstraint("max_points IS NULL OR max_points > 0", name="max_points_positive"),
        CheckConstraint("available_from IS NULL OR due_at IS NULL OR available_from < due_at", name="deadline"),
        CheckConstraint("state = 'draft' OR published_at IS NOT NULL", name="published_at"),
        Index("ix_feed_items_state_available", "state", "available_from"),
        Index("ix_feed_items_contest", "contest_id", postgresql_where=text("contest_id IS NOT NULL")),
        Index("ix_feed_items_material", "material_id", postgresql_where=text("material_id IS NOT NULL")),
    )


class FeedItemAudience(Base):
    """Who a feed item is addressed to: one group or one student per row. A group gives the item to the students
    who are in it now (as contest groups do); an archived group gives nothing."""

    __tablename__ = "feed_item_audience"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("feed_items.id", ondelete="CASCADE"), nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"))
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    added_at: Mapped[datetime] = utcnow_column()
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console

    __table_args__ = (
        CheckConstraint("(group_id IS NULL) <> (user_id IS NULL)", name="one_subject"),
        Index("uq_feed_item_audience_group", "item_id", "group_id", unique=True,
              postgresql_where=text("group_id IS NOT NULL")),
        Index("uq_feed_item_audience_user", "item_id", "user_id", unique=True,
              postgresql_where=text("user_id IS NOT NULL")),
        Index("ix_feed_item_audience_group", "group_id", postgresql_where=text("group_id IS NOT NULL")),
        Index("ix_feed_item_audience_user", "user_id", postgresql_where=text("user_id IS NOT NULL")),
    )


class AssignmentWork(Base):
    """A student's work on an assignment: created by the first submission or the first grade. Its row is locked
    (SELECT ... FOR UPDATE) by every change - a submission, a grade, a return, a release - so they go one after
    another. `status` is a cache of the history (assignment_submissions and assignment_reviews), written in the same
    transaction; the history is the truth."""

    __tablename__ = "assignment_works"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("feed_items.id", ondelete="RESTRICT"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[WorkStatus] = mapped_column(pg_enum(WorkStatus, "work_status"), nullable=False)
    status_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        UniqueConstraint("item_id", "user_id", name="uq_assignment_works_item_user"),
        Index("ix_assignment_works_item_status", "item_id", "status"),
        Index("ix_assignment_works_user", "user_id"),
    )


class AssignmentSubmission(Base):
    """One hand-in of a work, never changed; the latest one counts, the others are its history. `late` is fixed at
    receipt (as for contest submissions). Never a judgement: manual work does not reach the workers' queue."""

    __tablename__ = "assignment_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("assignment_works.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()  # the moment the request was received
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    ip: Mapped[Optional[str]] = mapped_column(String(45))

    __table_args__ = (Index("ix_assignment_submissions_work_created", "work_id", "created_at"),)


# The limits the CHECKs below keep, the same numbers as the rules (classroom/attachment_rules.py; a unit test compares
# them): the rules refuse more, these only make sure nothing longer is ever stored.
ATTACHMENT_NAME_MAX_CHARS = 150  # content_files.NAME_MAX
LINK_URL_MAX_CHARS = 2000
LINK_TITLE_MAX_CHARS = 200
_LINK_URL_CHECK = f"char_length(url) <= {LINK_URL_MAX_CHARS} AND url ~* '^https?://'"
_LINK_TITLE_CHECK = f"title IS NULL OR char_length(title) <= {LINK_TITLE_MAX_CHARS}"


class AssignmentSubmissionFile(Base):
    """A file of a hand-in, by content (blobs): the text answer (kind text, answer.md, at most one) and the files the
    student uploaded (kind file, 4v.3; names unique in the hand-in ignoring case, never empty)."""

    __tablename__ = "assignment_submission_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    # named by hand: the conventional name is longer than the 63 characters PostgreSQL keeps
    submission_id: Mapped[int] = mapped_column(ForeignKey("assignment_submissions.id", ondelete="CASCADE",
                                                          name="fk_assignment_submission_files_submission"),
                                               nullable=False)
    kind: Mapped[SubmissionFileKind] = mapped_column(pg_enum(SubmissionFileKind, "submission_file_kind"),
                                                     nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    blob_sha256: Mapped[str] = mapped_column(ForeignKey("blobs.sha256", ondelete="RESTRICT"), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("btrim(path) <> ''", name="path_not_blank"),
        CheckConstraint("size >= 0 AND position >= 0", name="non_negative"),
        CheckConstraint("kind <> 'text' OR path = 'answer.md'", name="text_is_answer_md"),
        CheckConstraint("kind <> 'file' OR size > 0", name="file_not_empty"),
        Index("ix_assignment_submission_files_submission", "submission_id", "position", "id"),
        Index("uq_assignment_submission_files_text", "submission_id", unique=True,
              postgresql_where=text("kind = 'text'")),
        # lower() folds Cyrillic in the C.UTF-8 databases of the installation; the rules compare by casefold first
        Index("uq_assignment_submission_files_name", "submission_id", text("lower(path)"), unique=True,
              postgresql_where=text("kind = 'file'")),
    )


class AssignmentSubmissionLink(Base):
    """A link of a hand-in (4v.3): data, stored as sent after strip; the server never opens it."""

    __tablename__ = "assignment_submission_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("assignment_submissions.id", ondelete="CASCADE",
                                                          name="fk_assignment_submission_links_submission"),
                                               nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(_LINK_URL_CHECK, name="url_valid"),
        CheckConstraint(_LINK_TITLE_CHECK, name="title_length"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_assignment_submission_links_submission", "submission_id", "position", "id"),
    )


class AssignmentReview(Base):
    """What a teacher did with a work, rows only added: the history of grades. The current grade is the last
    `graded` row; with grades_release = manual a student sees the last graded row that has a `released` row
    (release_of), a `returned` row at once (docs/CLASSROOM.md)."""

    __tablename__ = "assignment_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("assignment_works.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[ReviewAction] = mapped_column(pg_enum(ReviewAction, "review_action"), nullable=False)
    score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))  # grading = points
    passed: Mapped[Optional[bool]] = mapped_column(Boolean)  # grading = pass_fail
    comment: Mapped[Optional[str]] = mapped_column(Text)  # plain text, untrusted (ADMIN_API.md 4.1)
    reviewer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    at: Mapped[datetime] = utcnow_column()
    release_of: Mapped[Optional[int]] = mapped_column(ForeignKey("assignment_reviews.id", ondelete="RESTRICT"))

    __table_args__ = (
        CheckConstraint("(action = 'released') = (release_of IS NOT NULL)", name="release_of"),
        CheckConstraint("action = 'graded' OR (score IS NULL AND passed IS NULL)", name="mark_only_graded"),
        CheckConstraint("score IS NULL OR passed IS NULL", name="one_mark"),
        CheckConstraint("score IS NULL OR score >= 0", name="score_non_negative"),
        CheckConstraint("comment IS NULL OR char_length(comment) <= 2000", name="comment_length"),
        Index("ix_assignment_reviews_work", "work_id", "id"),
        Index("uq_assignment_reviews_release_of", "release_of", unique=True,
              postgresql_where=text("release_of IS NOT NULL")),
    )


# --------------------------------------------------------------------------- attachments (module 4v.3, D-078)
class Attachment(Base):
    """A teacher's file or link, in its owner's order: a feed item (an assignment, a contest item, a material item)
    or a version of a material (4v.4) - exactly one of them. A file is a blob with its name and size; a link is data
    (the server never opens it). From a feed item it is removed by deleting the row (the blob stays; the audit keeps
    name, sha and size) and goes with the item, which is deleted only as a draft. A version of a material never
    changes: a new version copies the rows it keeps."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="CASCADE"))
    material_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("material_versions.id",
                                                                          ondelete="RESTRICT"))
    kind: Mapped[AttachmentKind] = mapped_column(pg_enum(AttachmentKind, "attachment_kind"), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(Text)  # file
    blob_sha256: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.sha256", ondelete="RESTRICT"))  # file
    size: Mapped[Optional[int]] = mapped_column(BigInteger)  # file
    url: Mapped[Optional[str]] = mapped_column(Text)  # link
    title: Mapped[Optional[str]] = mapped_column(Text)  # link, optional
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        CheckConstraint("(kind = 'file') = (blob_sha256 IS NOT NULL) AND (kind = 'file') = (name IS NOT NULL) "
                        "AND (kind = 'file') = (size IS NOT NULL) AND (kind = 'link') = (url IS NOT NULL) "
                        "AND (kind = 'link' OR title IS NULL)", name="kind_shape"),
        CheckConstraint(f"name IS NULL OR (btrim(name, E' \\t\\r\\n') <> '' "
                        f"AND char_length(name) <= {ATTACHMENT_NAME_MAX_CHARS})", name="name_valid"),
        CheckConstraint("size IS NULL OR size > 0", name="size_positive"),
        CheckConstraint(f"url IS NULL OR ({_LINK_URL_CHECK})", name="url_valid"),
        CheckConstraint(_LINK_TITLE_CHECK, name="title_length"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint("num_nonnulls(item_id, material_version_id) = 1", name="one_owner"),
        Index("ix_attachments_item", "item_id", "position", "id"),
        Index("uq_attachments_item_name", "item_id", text("lower(name)"), unique=True,
              postgresql_where=text("kind = 'file'")),
        Index("ix_attachments_material_version", "material_version_id", "position", "id",
              postgresql_where=text("material_version_id IS NOT NULL")),
        Index("uq_attachments_material_version_name", "material_version_id", text("lower(name)"), unique=True,
              postgresql_where=text("kind = 'file' AND material_version_id IS NOT NULL")),
    )


# --------------------------------------------------------------------------- materials (module 4v.4, D-083)
# the limits the CHECKs below keep, the same numbers as the rules (materials/rules.py; a unit test compares them)
MATERIAL_SLUG_PATTERN = "^[a-z0-9][a-z0-9-]{1,62}$"
MATERIAL_TITLE_MAX_CHARS = 200
MATERIAL_BODY_MAX_CHARS = 100_000


class Material(Base):
    """A material of the library: a memo, a reference, notes. The slug never changes once created (links
    [[памятка:slug]] in immutable problem versions cannot be corrected; the services refuse, slug_immutable); the
    title is changed in place; the content is in immutable versions and everyone sees the latest one. Archived,
    never deleted: an archived material leaves the library but still opens by the feed and by links. `library`:
    who sees it in the library («Справочник»); the groups of `groups` are material_groups. docs/CLASSROOM.md §11."""

    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(MATERIAL_TITLE_MAX_CHARS), nullable=False)
    library: Mapped[MaterialLibrary] = mapped_column(pg_enum(MaterialLibrary, "material_library"), nullable=False,
                                                     default=MaterialLibrary.none,
                                                     server_default=MaterialLibrary.none.value)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    created_at: Mapped[datetime] = utcnow_column()
    updated_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        CheckConstraint(f"slug ~ '{MATERIAL_SLUG_PATTERN}'", name="slug_format"),
        CheckConstraint("btrim(title) <> ''", name="title_not_blank"),
    )


class MaterialVersion(Base):
    """One version of a material, never changed or deleted: the Markdown (untrusted, rendered only through a
    sanitizer) and its attachments (attachments.material_version_id). Numbered from 1 per material."""

    __tablename__ = "material_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="RESTRICT"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        UniqueConstraint("material_id", "version", name="uq_material_versions_material_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(f"char_length(body_md) <= {MATERIAL_BODY_MAX_CHARS}", name="body_length"),
    )


class MaterialGroup(Base):
    """A group the library shows a material to (library = groups): its current members, while the group is not
    archived (as the audience of feed items)."""

    __tablename__ = "material_groups"

    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="RESTRICT"), primary_key=True)
    added_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    added_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (Index("ix_material_groups_group", "group_id"),)


class LibrarySection(Base):
    """A section of the library («Справочник»): top level (parent_id null) or a subsection of a top-level one - two
    levels at most, kept by the services (materials/rules.check_section_place). A section with subsections is not
    deleted (RESTRICT); its materials are only taken out of it."""

    __tablename__ = "library_sections"

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("library_sections.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(String(MATERIAL_TITLE_MAX_CHARS), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("btrim(title) <> ''", name="title_not_blank"),
        CheckConstraint("position >= 0", name="position_non_negative"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_library_sections_parent", "parent_id", "position", "id"),
    )


class MaterialSection(Base):
    """A material in a section of the library, in the teacher's order; a material may be in several sections."""

    __tablename__ = "material_sections"

    section_id: Mapped[int] = mapped_column(ForeignKey("library_sections.id", ondelete="CASCADE"), primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="RESTRICT"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_material_sections_material", "material_id"),
    )


class MaterialRef(Base):
    """A link [[памятка:slug]] from a text to a material, derived from the text when it is saved (and by the
    migration of 4v.4 for the texts before it): exactly one source - a version of a problem (its statements), a feed
    item (its body) or a version of a material (its body). The slug, not the material: a link to a slug nobody has
    yet works once the material is created. Goes with its source (CASCADE). A link from a problem or a feed item may
    open the material to a student; one from a material only resolves (docs/CLASSROOM.md §11)."""

    __tablename__ = "material_refs"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    problem_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("problem_versions.id", ondelete="CASCADE"))
    feed_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("feed_items.id", ondelete="CASCADE"))
    material_version_id: Mapped[Optional[int]] = mapped_column(ForeignKey("material_versions.id",
                                                                          ondelete="CASCADE"))

    __table_args__ = (
        CheckConstraint("num_nonnulls(problem_version_id, feed_item_id, material_version_id) = 1",
                        name="one_source"),
        CheckConstraint(f"slug ~ '{MATERIAL_SLUG_PATTERN}'", name="slug_format"),
        Index("ix_material_refs_slug", "slug"),
        Index("uq_material_refs_problem_version", "problem_version_id", "slug", unique=True,
              postgresql_where=text("problem_version_id IS NOT NULL")),
        Index("uq_material_refs_feed_item", "feed_item_id", "slug", unique=True,
              postgresql_where=text("feed_item_id IS NOT NULL")),
        Index("uq_material_refs_material_version", "material_version_id", "slug", unique=True,
              postgresql_where=text("material_version_id IS NOT NULL")),
    )


# --------------------------------------------------------------------------- comments (module 4v.2, D-072)
COMMENT_MAX_CHARS = 4000  # the CHECK below; the rules (classroom/comment_rules.py) refuse more, and more blank text


class CommentThread(Base):
    """A comment thread of a feed item: the shared one (student_id null, one per item) or the private one of a
    student and the teachers (one per item and student; assignments only, the service checks the kind). Created by
    its first comment, never deleted: a thread always has comments. Keyed by the pair (item, student), not by the
    work: a question comes before the first hand-in, and a work exists only from then (docs/CLASSROOM.md §9)."""

    __tablename__ = "comment_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("feed_items.id", ondelete="RESTRICT"), nullable=False)
    student_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: shared
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        Index("uq_comment_threads_shared", "item_id", unique=True, postgresql_where=text("student_id IS NULL")),
        Index("uq_comment_threads_private", "item_id", "student_id", unique=True,
              postgresql_where=text("student_id IS NOT NULL")),
        Index("ix_comment_threads_student", "student_id", postgresql_where=text("student_id IS NOT NULL")),
    )


class Comment(Base):
    """One comment, never edited; rows are only added. Deletion is soft: deleted_at / deleted_by (null with
    deleted_at: the console) and the reason; teachers still see the text, students do not see the comment at all.
    The body is untrusted Markdown, stored as sent (rendered only through a sanitizer, without raw HTML)."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("comment_threads.id", ondelete="RESTRICT"), nullable=False)
    author_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = utcnow_column()  # the moment the request was received
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))  # null: console
    delete_reason: Mapped[Optional[str]] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(f"char_length(body) <= {COMMENT_MAX_CHARS}", name="body_length"),
        CheckConstraint("btrim(body, E' \\t\\r\\n') <> ''", name="body_not_blank"),
        CheckConstraint("deleted_at IS NOT NULL OR (deleted_by IS NULL AND delete_reason IS NULL)",
                        name="deleted_fields"),
        Index("ix_comments_thread", "thread_id", "id"),
        Index("ix_comments_author_created", "author_id", "created_at", postgresql_where=text("author_id IS NOT NULL")),
    )


class ThreadRead(Base):
    """How far a user has read a thread: the last comment read (only grows; the service keeps it in the thread).
    Unread = comments of others, not deleted, after it. Per user, teachers included (each has their own)."""

    __tablename__ = "thread_reads"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("comment_threads.id", ondelete="RESTRICT"), primary_key=True)
    last_read_comment_id: Mapped[int] = mapped_column(ForeignKey("comments.id", ondelete="RESTRICT"), nullable=False)
    updated_at: Mapped[datetime] = utcnow_column()


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
    late: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                       server_default=text("false"))  # fixed at receipt, never recomputed

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
    # the most this judgement could give, in the points of the problem version (sum of the groups / of the quiz
    # questions); null only while it is not finished (D-090). Points of a contest: scores.contest_score, never here
    max_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 2))
    compile_log: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)  # internal error text: teachers only
    # the server ended it itself (gave up after max_attempts, D-069, D-090): a late result of its holder is refused.
    # Written only by queue._fail_attempt, read through queue.server_gave_up
    server_gave_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint("max_score IS NULL OR max_score >= 0", name="max_score_non_negative"),
        CheckConstraint("server_gave_up_at IS NULL OR status = 'internal_error'", name="gave_up_is_internal_error"),
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


class VersionCheck(Base):
    """A worker checks a problem version (session 4a.4, docs/QUEUE.md "Проверки задач"): it compiles the checker
    and the interactor and runs the reference solutions against their expected verdicts.

    Not a judgement: checks are outside the fair queue and the students' load indicator. A worker takes one only
    when no submission is waiting for it, and at most `JUDGE_MAX_CONCURRENT_CHECKS` run at once.

    * status: queued -> running -> done. The outcome (ready / failed) of a done check becomes the version status
      and its report is copied to problem_versions.check_report; the rows here are the history.
    * attempt is the fencing token (as in judgements); failures counts lost workers and internal errors, at
      max_attempts the check ends failed.
    * heartbeat_at: the last heartbeat about this check.
    """

    __tablename__ = "version_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id"), nullable=False)
    reason: Mapped[CheckReason] = mapped_column(pg_enum(CheckReason, "check_reason"), nullable=False)
    status: Mapped[CheckStatus] = mapped_column(
        pg_enum(CheckStatus, "check_status"), nullable=False, default=CheckStatus.queued
    )
    outcome: Mapped[Optional[CheckOutcome]] = mapped_column(pg_enum(CheckOutcome, "check_outcome"))
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id"))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    progress_done: Mapped[Optional[int]] = mapped_column(Integer)  # reference solutions run so far
    progress_total: Mapped[Optional[int]] = mapped_column(Integer)
    report: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)  # the check_report (docs/WORKER_PROTOCOL.md)
    error: Mapped[Optional[str]] = mapped_column(Text)  # the last internal error or why the check gave up
    requested_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))  # null: console import or recheck
    created_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    version: Mapped[ProblemVersion] = relationship()

    __table_args__ = (
        # at most one unfinished check per version (a recheck never doubles it)
        Index("uq_version_checks_active", "problem_version_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')")),
        # the checks waiting for a worker, oldest first
        Index("ix_version_checks_queue", "created_at", "id", postgresql_where=text("status = 'queued'")),
        # the history of a version
        Index("ix_version_checks_version", "problem_version_id", "id"),
        CheckConstraint("(status = 'done') = (outcome IS NOT NULL)", name="outcome_when_done"),
    )


# --------------------------------------------------------------------------- infrastructure
class WorkerToken(Base):
    """Credential of a machine (or pool) of workers. Only sha256(token) is stored; tokens are 256-bit random."""

    __tablename__ = "worker_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # allowed maximum; "problem_check" lets workers take checks of problem versions (4a.4)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=lambda: ["cpu", "problem_check"])
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
