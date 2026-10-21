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
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
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
    teacher = "teacher"  # administrator and teacher are one role
    student = "student"


class VersionStatus(str, enum.Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


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


class ContestMode(str, enum.Enum):
    practice = "practice"  # open-ended, no timer
    timed = "timed"  # start/end window and/or individual timer


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
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(64), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # argon2
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[Role] = mapped_column(pg_enum(Role, "role"), nullable=False, default=Role.student)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (
        # logins are case-insensitive
        Index("uq_users_login_lower", text("lower(login)"), unique=True),
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
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()

    versions: Mapped[list[ProblemVersion]] = relationship(back_populates="problem", order_by="ProblemVersion.version")


class ProblemVersion(Base):
    __tablename__ = "problem_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        pg_enum(VersionStatus, "version_status"), nullable=False, default=VersionStatus.draft
    )
    type: Mapped[ProblemType] = mapped_column(pg_enum(ProblemType, "problem_type"), nullable=False)
    titles: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)  # {"ru": "..."}
    statements: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)  # {"ru": "markdown"}
    # normalized problem.yaml (limits, io, checker, feedback, questions, answers, ...)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    package_sha256: Mapped[Optional[str]] = mapped_column(ForeignKey("blobs.sha256"))
    created_at: Mapped[datetime] = utcnow_column()
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    problem: Mapped[Problem] = relationship(back_populates="versions")
    groups: Mapped[list[TestGroup]] = relationship(
        back_populates="version", order_by="TestGroup.position", cascade="all, delete-orphan"
    )
    programs: Mapped[list[Program]] = relationship(back_populates="version", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("problem_id", "version", name="uq_problem_versions_problem_version"),)


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
    __tablename__ = "contests"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[ContestMode] = mapped_column(pg_enum(ContestMode, "contest_mode"), nullable=False)
    scoring_mode: Mapped[ScoringMode] = mapped_column(
        pg_enum(ScoringMode, "scoring_mode"), nullable=False, default=ScoringMode.points
    )
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    duration_minutes: Mapped[Optional[int]] = mapped_column(Integer)  # individual timer from first entry
    leaderboard_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    freeze_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))  # leaderboard freeze
    icpc_penalty_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    join_code: Mapped[Optional[str]] = mapped_column(String(32))  # students join with it; null = open to any account
    plagiarism_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # reserved for later
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = utcnow_column()

    problems: Mapped[list[ContestProblem]] = relationship(
        back_populates="contest", order_by="ContestProblem.position", cascade="all, delete-orphan"
    )


class ContestProblem(Base):
    """A problem inside a contest, pinned to a concrete version."""

    __tablename__ = "contest_problems"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), nullable=False)
    problem_version_id: Mapped[int] = mapped_column(ForeignKey("problem_versions.id"), nullable=False)
    label: Mapped[str] = mapped_column(String(8), nullable=False)  # A, B, C ...
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    contest: Mapped[Contest] = relationship(back_populates="problems")
    problem_version: Mapped[ProblemVersion] = relationship()

    __table_args__ = (UniqueConstraint("contest_id", "label", name="uq_contest_problems_contest_label"),)


class Participation(Base):
    __tablename__ = "participations"

    id: Mapped[int] = mapped_column(primary_key=True)
    contest_id: Mapped[int] = mapped_column(ForeignKey("contests.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    started_at: Mapped[datetime] = utcnow_column()  # start of the individual timer

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
        Index("ix_submissions_contest_problem", "contest_problem_id"),
    )


class Judgement(Base):
    __tablename__ = "judgements"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[JudgementReason] = mapped_column(
        pg_enum(JudgementReason, "judgement_reason"), nullable=False, default=JudgementReason.initial
    )
    status: Mapped[JudgementStatus] = mapped_column(
        pg_enum(JudgementStatus, "judgement_status"), nullable=False, default=JudgementStatus.queued
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verdict: Mapped[Optional[Verdict]] = mapped_column(VERDICT_T)
    score: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    compile_log: Mapped[Optional[str]] = mapped_column(Text)
    worker_id: Mapped[Optional[int]] = mapped_column(ForeignKey("workers.id"))
    created_at: Mapped[datetime] = utcnow_column()
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    submission: Mapped[Submission] = relationship(back_populates="judgements")
    results: Mapped[list[TestResult]] = relationship(
        back_populates="judgement", order_by="TestResult.id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # the queue: only rows waiting for a worker
        Index("ix_judgements_queue", "created_at", postgresql_where=text("status = 'queued'")),
        # at most one current judgement per submission
        Index("uq_judgements_current", "submission_id", unique=True, postgresql_where=text("is_current")),
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
    checker_message: Mapped[Optional[str]] = mapped_column(Text)  # always stored; shown per feedback policy
    visible_to_student: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    judgement: Mapped[Judgement] = relationship(back_populates="results")

    __table_args__ = (UniqueConstraint("judgement_id", "test_case_id", name="uq_test_results_judgement_test"),)


# --------------------------------------------------------------------------- infrastructure
class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)  # worker authenticates to the API
    capabilities: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)  # e.g. ["cpu"], ["gpu"]
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = utcnow_column()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(64), nullable=False)  # login, submit, view_tests, rejudge ...
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = utcnow_column()

    __table_args__ = (Index("ix_audit_log_user_created", "user_id", "created_at"),)
