"""Contests: apply from contest.yaml, pin, publish/archive, invite codes, join, personal start (docs/CONTESTS.md).

Functions flush but never commit; the caller owns the transaction (judgectl: one per command). Every change is
written to audit_log. Who may do what is decided by access.py; these functions only carry it out.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from .. import queue as q
from ..auth import audit
from ..models import (Contest, ContestProblem, ContestState, JoinPolicy, Language, LeaderboardMode, Participation,
                      Problem, ProblemVersion, ScoringMode, Submission, TimingMode, User, VersionStatus)
from . import access
from .spec import ContestSpec

# invite codes: 6 characters without look-alikes (no 0/O, 1/I/L, 2/Z, 5/S, 8/B), typed in any case
INVITE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY3479"
INVITE_LENGTH = 6


class ContestError(ValueError):
    """A refusal with every reason (`problems`) and a stable `code` for the API (docs/ADMIN_API.md)."""

    def __init__(self, problems, code: str = "contest_conflict"):
        self.code = code
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__("; ".join(self.problems) if len(self.problems) == 1
                         else "\n  - " + "\n  - ".join(self.problems))


@dataclass
class ApplyResult:
    contest: Contest
    created: bool
    changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)  # nothing wrong, but the operator should know


def get_contest(db: Session, slug: str) -> Contest:
    contest = db.execute(select(Contest).where(Contest.slug == slug)).scalar_one_or_none()
    if contest is None:
        raise ContestError(f"no contest {slug!r}", "contest_not_found")
    return contest


def new_invite_code(db: Session) -> str:
    while True:
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))
        with db.no_autoflush:  # a pending contest without its code must not be flushed by this query
            if db.execute(select(Contest.id).where(Contest.invite_code == code)).first() is None:
                return code


def has_submissions(db: Session, contest_problem_id: int) -> bool:
    return db.execute(select(exists().where(Submission.contest_problem_id == contest_problem_id))).scalar()


def latest_ready(db: Session, problem_id: int) -> Optional[ProblemVersion]:
    return db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem_id,
                                                   ProblemVersion.status == VersionStatus.ready)
                      .order_by(ProblemVersion.version.desc()).limit(1)).scalar_one_or_none()


def _version_no(db: Session, version_id: int) -> int:
    return db.get(ProblemVersion, version_id).version


def _resolve(db: Session, spec: ContestSpec) -> dict:
    """slug -> (problem, the version asked for or None); ContestError with every problem."""
    problems, out = [], {}
    if spec.languages:
        enabled = set(db.execute(select(Language.key).where(Language.enabled.is_(True))).scalars())
        unknown = sorted(set(spec.languages) - enabled)
        if unknown:
            problems.append(f"languages not enabled in the system: {', '.join(unknown)}")
    for entry in spec.problems:
        problem = db.execute(select(Problem).where(Problem.slug == entry.slug)).scalar_one_or_none()
        if problem is None:
            problems.append(f"{entry.label}: no problem {entry.slug!r} (import it first)")
            continue
        if problem.archived:
            problems.append(f"{entry.label}: problem {entry.slug} is archived")
            continue
        version = None
        if entry.version is not None:
            version = db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem.id,
                                                              ProblemVersion.version == entry.version)).scalar_one_or_none()
            if version is None:
                problems.append(f"{entry.label}: {entry.slug} has no version {entry.version}")
                continue
            if version.status != VersionStatus.ready:
                problems.append(f"{entry.label}: {entry.slug} v{entry.version} is {version.status.value}; "
                                "only ready versions can be pinned")
                continue
        elif latest_ready(db, problem.id) is None:
            problems.append(f"{entry.label}: {entry.slug} has no ready version")
            continue
        out[entry.slug] = (problem, version)
    if problems:
        raise ContestError(problems)
    return out


def _fields(spec: ContestSpec) -> dict:
    return {
        "title": spec.title, "description_md": spec.description, "starts_at": spec.starts_at, "ends_at": spec.ends_at,
        "timing_mode": TimingMode(spec.timing_mode), "personal_duration": spec.personal_duration,
        "freeze_at": spec.freeze_at, "scoring_mode": ScoringMode(spec.scoring_mode),
        "icpc_penalty_minutes": spec.icpc_penalty_minutes, "leaderboard": LeaderboardMode(spec.leaderboard),
        "join_policy": JoinPolicy(spec.join_policy), "allow_upsolving": spec.allow_upsolving,
        "languages": spec.languages, "submit_cooldown_sec": spec.submit_cooldown_sec,
        "max_submissions_per_problem": spec.max_submissions_per_problem, "tests_after_end": spec.tests_after_end,
        "plagiarism_enabled": spec.plagiarism_enabled,
    }


def _show(value) -> str:
    return "-" if value is None else str(getattr(value, "value", value))


def apply(db: Session, actor: audit.Actor, spec: ContestSpec, now: datetime) -> ApplyResult:
    """Make the contest what the file says (R35), idempotently: a second apply of the same file changes nothing.

    Problems are matched by their problem, labels may change. A problem gone from the file is hidden if it has
    submissions, else removed. A problem coming back shows its newest hidden row again when that row's version is
    the one in the file (or the file names none), else it gets a new row. A pinned version that differs from the
    file is changed only while the problem has no submissions; otherwise `contest pin` decides about rejudging.
    """
    resolved = _resolve(db, spec)
    contest = db.execute(select(Contest).where(Contest.slug == spec.slug).with_for_update()).scalar_one_or_none()
    created = contest is None
    changes, warnings = [], []
    if created:
        contest = Contest(slug=spec.slug, state=ContestState.draft, created_by=actor.user_id, **_fields(spec))
        db.add(contest)
        changes.append("contest created (draft)")
    else:
        if contest.join_policy == JoinPolicy.open and spec.join_policy == "code" and contest.invite_code is not None:
            warnings.append(f"join_policy open -> code: the invite code this contest had before is valid again; "
                            f"if it may have spread, give a new one: judgectl contest code regenerate {contest.slug}")
        for name, value in _fields(spec).items():
            old = getattr(contest, name)
            if old != value:
                setattr(contest, name, value)
                changes.append(f"{name}: {_show(old)} -> {_show(value)}")
    if contest.join_policy == JoinPolicy.code and contest.invite_code is None:
        contest.invite_code = new_invite_code(db)
        changes.append("invite code created")
    db.flush()

    rows = list(db.execute(select(ContestProblem).where(ContestProblem.contest_id == contest.id)
                           .order_by(ContestProblem.id)).scalars())
    visible = {r.problem_id: r for r in rows if not r.hidden}
    wanted = {resolved[e.slug][0].id for e in spec.problems}
    old_labels = {r.id: r.label for r in rows}
    for problem_id, row in list(visible.items()):
        if problem_id in wanted:
            continue
        slug = db.get(Problem, problem_id).slug
        if has_submissions(db, row.id):
            row.hidden = True
            changes.append(f"{row.label} ({slug}): hidden (it has submissions, they stay)")
        else:
            db.delete(row)
            changes.append(f"{row.label} ({slug}): removed")
        del visible[problem_id]
    for row in visible.values():  # labels may be swapped or reused: park them first
        row.label = f"~{row.id}"[:8]
    db.flush()

    problems = []
    for position, entry in enumerate(spec.problems):
        problem, version = resolved[entry.slug]
        row = visible.get(problem.id)
        if row is not None:
            if version is not None and row.problem_version_id != version.id:
                current = _version_no(db, row.problem_version_id)
                if has_submissions(db, row.id):
                    problems.append(f"{entry.label} ({entry.slug}) is pinned to v{current} and has submissions: change "
                                    f"it with `judgectl contest pin {spec.slug} {entry.label} {version.version} "
                                    "--rejudge` (or --no-rejudge)")
                    continue
                row.problem_version_id = version.id
                changes.append(f"{entry.label} ({entry.slug}): v{current} -> v{version.version}")
        else:
            hidden = sorted((r for r in rows if r.hidden and r.problem_id == problem.id), key=lambda r: r.id)
            last = hidden[-1] if hidden else None
            if last is not None and (version is None or last.problem_version_id == version.id):
                row = last
                row.label = f"~{row.id}"[:8]
                row.hidden = False
                changes.append(f"{entry.label} ({entry.slug}): shown again with its submissions "
                               f"(v{_version_no(db, row.problem_version_id)})")
            else:
                target = version or latest_ready(db, problem.id)
                row = ContestProblem(contest_id=contest.id, problem_id=problem.id, problem_version_id=target.id,
                                     label=entry.label, position=position, points=entry.points)
                db.add(row)
                changes.append(f"{entry.label} ({entry.slug}): added, v{target.version}")
            db.flush()
        if old_labels.get(row.id, entry.label) != entry.label:
            changes.append(f"{entry.slug}: label {old_labels[row.id]} -> {entry.label}")
        row.label = entry.label
        if row.position != position:
            if row.id in old_labels:
                changes.append(f"{entry.label} ({entry.slug}): position {row.position + 1} -> {position + 1}")
            row.position = position
        if row.points != entry.points:  # Decimal compares numerically: 100.00 == 100
            changes.append(f"{entry.label} ({entry.slug}): points {_show(row.points)} -> {_show(entry.points)}")
            row.points = entry.points
    if problems:
        raise ContestError(problems)
    db.flush()
    if changes:
        audit.record(db, actor, "contest.apply", now, object_type="contest", object_id=contest.id,
                     details={"slug": contest.slug, "created": created, "changes": changes, "warnings": warnings})
    return ApplyResult(contest, created, changes, warnings)


def visible_row(db: Session, contest: Contest, label: str) -> ContestProblem:
    row = db.execute(select(ContestProblem).where(ContestProblem.contest_id == contest.id,
                                                  ContestProblem.label == label,
                                                  ContestProblem.hidden.is_(False))).scalar_one_or_none()
    if row is None:
        raise ContestError(f"contest {contest.slug} has no problem {label!r}", "contest_problem_not_found")
    return row


def pin(db: Session, actor: audit.Actor, contest: Contest, label: str, version_no: int, rejudge: bool,
        now: datetime) -> dict:
    """R22: pin another (ready) version; with rejudge every submission of the problem is judged again."""
    row = visible_row(db, contest, label)
    if db.get(Problem, row.problem_id).archived:
        raise ContestError(f"{label}: the problem is archived: unarchive it to pin another version", "problem_archived")
    version = db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == row.problem_id,
                                                      ProblemVersion.version == version_no)).scalar_one_or_none()
    if version is None:
        raise ContestError(f"{label}: the problem has no version {version_no}", "version_not_found")
    if version.status != VersionStatus.ready:
        raise ContestError(f"{label}: v{version_no} is {version.status.value}; only ready versions can be pinned",
                           "version_not_ready")
    old = _version_no(db, row.problem_version_id)
    result = {"label": label, "from": old, "to": version_no, "rejudge": None}
    if row.problem_version_id != version.id:
        row.problem_version_id = version.id
        db.flush()
    if rejudge:
        result["rejudge"] = q.rejudge_contest_problem(db, row.id, requested_by=actor.user_id)
    audit.record(db, actor, "contest.pin", now, object_type="contest_problem", object_id=row.id,
                 details={"contest": contest.slug, **result})
    return result


def set_state(db: Session, actor: audit.Actor, contest: Contest, state: ContestState, now: datetime) -> bool:
    if contest.state == state:
        return False
    old = contest.state
    contest.state = state
    audit.record(db, actor, f"contest.{'publish' if state == ContestState.published else state.value}", now,
                 object_type="contest", object_id=contest.id, details={"slug": contest.slug, "from": old.value})
    return True


def regenerate_code(db: Session, actor: audit.Actor, contest: Contest, now: datetime) -> str:
    if contest.join_policy != JoinPolicy.code:
        raise ContestError(f"contest {contest.slug} is open: it has no invite code", "contest_is_open")
    contest.invite_code = new_invite_code(db)
    audit.record(db, actor, "contest.code_regenerate", now, object_type="contest", object_id=contest.id,
                 details={"slug": contest.slug})
    return contest.invite_code


def participation_of(db: Session, contest: Contest, user: User, lock: bool = False) -> Optional[Participation]:
    query = select(Participation).where(Participation.contest_id == contest.id, Participation.user_id == user.id)
    return db.execute(query.with_for_update() if lock else query).scalar_one_or_none()


def join(db: Session, user: User, contest: Contest, now: datetime, code: Optional[str] = None) -> Participation:
    """R23: a student joins (open, or with the invite code)."""
    decision = access.can_join(user, contest, participation_of(db, contest, user), now, code)
    if not decision:
        raise ContestError(decision.reason, decision.reason)
    row = Participation(contest_id=contest.id, user_id=user.id, joined_at=now)
    db.add(row)
    db.flush()
    return row


def start(db: Session, user: User, contest: Contest, now: datetime) -> Participation:
    """R21: the personal start, once."""
    row = participation_of(db, contest, user, lock=True)
    decision = access.can_start(user, contest, row, now)
    if not decision:
        raise ContestError(decision.reason, decision.reason)
    row.started_at = now
    db.flush()
    return row


def participants(db: Session, contest: Contest) -> list:
    return list(db.execute(select(Participation, User).join(User, User.id == Participation.user_id)
                           .where(Participation.contest_id == contest.id).order_by(Participation.joined_at)).all())


def counted_submissions(db: Session, contest_problem_id: int, user_id: int, since: datetime,
                        until: Optional[datetime]) -> int:
    """Submissions of a user for a problem inside a window: the ones the per-problem limit applies to."""
    query = select(func.count()).select_from(Submission).where(Submission.contest_problem_id == contest_problem_id,
                                                               Submission.user_id == user_id,
                                                               Submission.created_at >= since)
    if until is not None:
        query = query.where(Submission.created_at < until)
    return db.execute(query).scalar_one()


# ----------------------------------------------------------------------------- what there is to see (judgectl, API)
def contests_query(state: Optional[ContestState] = None, include_archived: bool = True):
    query = select(Contest)
    if state is not None:
        query = query.where(Contest.state == state)
    elif not include_archived:
        query = query.where(Contest.state != ContestState.archived)
    return query


def contest_summary(db: Session, c: Contest) -> dict:
    problems = db.execute(select(func.count()).select_from(ContestProblem).where(
        ContestProblem.contest_id == c.id, ContestProblem.hidden.is_(False))).scalar_one()
    people = db.execute(select(func.count()).select_from(Participation)
                        .where(Participation.contest_id == c.id)).scalar_one()
    return {"id": c.id, "slug": c.slug, "title": c.title, "state": c.state, "timing_mode": c.timing_mode,
            "starts_at": c.starts_at, "ends_at": c.ends_at, "problems": problems, "participants": people}


def format_duration(value) -> Optional[str]:
    """timedelta -> "1h30m" / "90m"-style text that contest.yaml accepts back."""
    if value is None:
        return None
    minutes = int(value.total_seconds() // 60)
    hours, rest = divmod(minutes, 60)
    return (f"{hours}h" if hours else "") + (f"{rest}m" if rest or not hours else "")


def contest_detail(db: Session, c: Contest) -> dict:
    """The contest in the shape of contest.yaml (so it can be sent back to apply unchanged: `spec`), plus what the
    file does not hold: state, invite code, every contest problem row (hidden ones too) and the participant count."""
    rows = db.execute(select(ContestProblem, Problem.slug, ProblemVersion.version, ProblemVersion.status)
                      .join(Problem, Problem.id == ContestProblem.problem_id)
                      .join(ProblemVersion, ProblemVersion.id == ContestProblem.problem_version_id)
                      .where(ContestProblem.contest_id == c.id)
                      .order_by(ContestProblem.hidden, ContestProblem.position, ContestProblem.id)).all()
    spec = {"format_version": 1, "slug": c.slug, "title": c.title, "description": c.description_md,
            "starts_at": c.starts_at, "ends_at": c.ends_at, "timing_mode": c.timing_mode.value,
            "scoring_mode": c.scoring_mode.value, "icpc_penalty_minutes": c.icpc_penalty_minutes,
            "leaderboard": c.leaderboard.value, "join_policy": c.join_policy.value,
            "allow_upsolving": c.allow_upsolving, "submit_cooldown_sec": c.submit_cooldown_sec,
            "max_submissions_per_problem": c.max_submissions_per_problem, "tests_after_end": c.tests_after_end,
            "plagiarism_enabled": c.plagiarism_enabled,
            "problems": [{"label": cp.label, "problem": slug, "version": version,
                          **({"points": float(cp.points)} if cp.points is not None else {})}
                         for cp, slug, version, _status in rows if not cp.hidden]}
    if c.personal_duration is not None:
        spec["personal_duration"] = format_duration(c.personal_duration)
    if c.freeze_at is not None:
        spec["freeze_at"] = c.freeze_at
    if c.languages is not None:
        spec["languages"] = c.languages
    return {"spec": spec, "state": c.state, "created_at": c.created_at,
            "invite_code": c.invite_code if c.join_policy == JoinPolicy.code else None,
            "participants": db.execute(select(func.count()).select_from(Participation)
                                       .where(Participation.contest_id == c.id)).scalar_one(),
            "rows": [{"id": cp.id, "label": cp.label, "problem": slug, "version": version, "status": status,
                      "points": cp.points, "hidden": cp.hidden} for cp, slug, version, status in rows]}
