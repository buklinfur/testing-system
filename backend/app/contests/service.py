"""Contests: apply from contest.yaml, pin, publish/archive, invite codes, entry, personal start (docs/CONTESTS.md).

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

from .. import groups as g
from .. import problem_checks as checks
from .. import queue as q
from ..auth import audit
from ..models import (AccessMode, AfterDueStatement, AfterDueSubmit, Contest, ContestGroup, ContestProblem,
                      ContestState, DeadlineExtension, Group, GroupMember, JoinedVia, Language,
                      LeaderboardMode, Participation, Problem, ProblemVersion, ScoringMode, Submission, TimingMode,
                      User, VersionStatus)
from . import access
from .spec import ContestSpec

# invite codes: 6 characters without look-alikes (no 0/O, 1/I/L, 2/Z, 5/S, 8/B), typed in any case
INVITE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY3479"
INVITE_LENGTH = 6
CODE_MODES = (AccessMode.code, AccessMode.code_or_groups)


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


def newer_not_ready(db: Session, problem_id: int, than: int) -> list[ProblemVersion]:
    """Versions newer than `than` that are not ready (pending_check / failed), newest first."""
    return list(db.execute(select(ProblemVersion).where(ProblemVersion.problem_id == problem_id,
                                                        ProblemVersion.version > than,
                                                        ProblemVersion.status != VersionStatus.ready)
                           .order_by(ProblemVersion.version.desc())).scalars())


def _statuses(db: Session, problem_id: int) -> str:
    rows = db.execute(select(ProblemVersion.version, ProblemVersion.status).where(
        ProblemVersion.problem_id == problem_id).order_by(ProblemVersion.version)).all()
    return ", ".join(f"v{n} {st.value}" for n, st in rows) or "no versions"


def _version_no(db: Session, version_id: int) -> int:
    return db.get(ProblemVersion, version_id).version


def _resolve_groups(db: Session, spec: ContestSpec, problems: list) -> list[Group]:
    """The groups of access.groups by name (ignoring case): a group not archived, or an archived one this contest
    already has (it stays attached and gives no access, K6); an archived group cannot be attached anew."""
    found = []
    attached = {}
    if spec.groups:
        rows = db.execute(select(Group).join(ContestGroup, ContestGroup.group_id == Group.id)
                          .join(Contest, Contest.id == ContestGroup.contest_id).where(Contest.slug == spec.slug)).scalars()
        attached = {grp.name.lower(): grp for grp in rows}
    for name in spec.groups:
        live = db.execute(select(Group).where(func.lower(Group.name) == name.lower(),
                                              Group.archived_at.is_(None))).scalar_one_or_none()
        group = live or attached.get(name.lower())
        if group is not None:
            found.append(group)
        elif db.execute(select(Group.id).where(func.lower(Group.name) == name.lower())).first() is not None:
            problems.append(f"group {name!r} is archived: an archived group cannot be attached to a contest")
        else:
            problems.append(f"no group {name!r} (judgectl group create {name!r})")
    return found


def _resolve(db: Session, spec: ContestSpec) -> tuple:
    """(slug -> (problem, the version asked for or None), the groups); ContestError with every problem."""
    problems, out = [], {}
    wanted_groups = _resolve_groups(db, spec, problems)
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
                problems.append(f"{entry.label}: {checks.not_ready_reason(entry.slug, version)}")
                continue
        elif latest_ready(db, problem.id) is None:
            problems.append(f"{entry.label}: {entry.slug} has no ready version ({_statuses(db, problem.id)}; "
                            f"reports: judgectl problem versions {entry.slug})")
            continue
        out[entry.slug] = (problem, version)
    if problems:
        raise ContestError(problems)
    return out, wanted_groups


def _apply_groups(db: Session, actor: audit.Actor, contest: Contest, wanted: list, now: datetime,
                  changes: list, warnings: list) -> None:
    current = {cg.group_id: cg for cg in db.execute(select(ContestGroup)
                                                     .where(ContestGroup.contest_id == contest.id)).scalars()}
    for group in wanted:
        if group.id not in current:
            db.add(ContestGroup(contest_id=contest.id, group_id=group.id, added_at=now, added_by=actor.user_id))
            changes.append(f"group {group.name} attached")
        if group.archived_at is not None:
            warnings.append(f"group {group.name} is archived: it gives no access to this contest")
    keep = {group.id for group in wanted}
    for group_id, row in current.items():
        if group_id not in keep:
            name = db.get(Group, group_id).name
            db.delete(row)
            changes.append(f"group {name} detached")
            warnings.append(f"group {name} detached: its students lose access to the contest unless they came in "
                            "with the invite code (their submissions and scores stay visible to them)")


def _fields(spec: ContestSpec) -> dict:
    return {
        "title": spec.title, "description_md": spec.description, "starts_at": spec.starts_at, "ends_at": spec.ends_at,
        "timing_mode": TimingMode(spec.timing_mode), "personal_duration": spec.personal_duration,
        "freeze_at": spec.freeze_at, "scoring_mode": ScoringMode(spec.scoring_mode),
        "icpc_penalty_minutes": spec.icpc_penalty_minutes, "leaderboard": LeaderboardMode(spec.leaderboard),
        "access_mode": AccessMode(spec.access_mode), "allow_upsolving": spec.allow_upsolving,
        "languages": spec.languages, "submit_cooldown_sec": spec.submit_cooldown_sec,
        "max_submissions_per_problem": spec.max_submissions_per_problem, "tests_after_end": spec.tests_after_end,
        "plagiarism_enabled": spec.plagiarism_enabled,
    }


def _show(value) -> str:
    return "-" if value is None else str(getattr(value, "value", value))


def _apply_deadlines(db: Session, row: ContestProblem, entry, changes: list, warnings: list) -> None:
    """available_from, due_at, after-due policies and the note of one contest problem (D-023). Extensions are not
    in the file; apply warns when a new due_at leaves some of them without effect."""
    where = f"{entry.label} ({entry.slug})"
    if entry.due_at_minute_59:
        warnings.append(f"{where}: due_at at minute 59 without seconds is HH:59:00, so a submission at HH:59:30 is "
                        "late; for the end of that minute write HH:59:59 (or the next full hour)")
    old_due = row.due_at
    for name, value in (("available_from", entry.available_from), ("due_at", entry.due_at),
                        ("after_due_statement", AfterDueStatement(entry.after_due_statement)),
                        ("after_due_submit", AfterDueSubmit(entry.after_due_submit))):
        if getattr(row, name) != value:
            changes.append(f"{where}: {name} {_show(getattr(row, name))} -> {_show(value)}")
            setattr(row, name, value)
    if row.note_md != entry.note_md:
        changes.append(f"{where}: note " + ("removed" if entry.note_md is None else "set" if row.note_md is None
                                            else "changed"))
        row.note_md = entry.note_md
    if old_due == row.due_at or row.id is None:
        return
    extensions = list(db.execute(select(DeadlineExtension.due_at)
                                 .where(DeadlineExtension.contest_problem_id == row.id)).scalars())
    if not extensions:
        return
    if row.due_at is None:
        warnings.append(f"{where}: the deadline is removed, so its {len(extensions)} extension(s) mean nothing now "
                        f"(they stay; judgectl extension list)")
    else:
        earlier = sum(1 for due in extensions if due <= row.due_at)
        if earlier:
            warnings.append(f"{where}: the new due_at is not earlier than {earlier} extension(s): they give "
                            "nothing more now (judgectl extension list)")


def apply(db: Session, actor: audit.Actor, spec: ContestSpec, now: datetime) -> ApplyResult:
    """Make the contest what the file says (R35), idempotently: a second apply of the same file changes nothing.

    Problems are matched by their problem, labels may change. A problem gone from the file is hidden if it has
    submissions, else removed. A problem coming back shows its newest hidden row again when that row's version is
    the one in the file (or the file names none), else it gets a new row. A pinned version that differs from the
    file is changed only while the problem has no submissions; otherwise `contest pin` decides about rejudging.
    """
    resolved, wanted_groups = _resolve(db, spec)
    contest = db.execute(select(Contest).where(Contest.slug == spec.slug).with_for_update()).scalar_one_or_none()
    created = contest is None
    changes, warnings = [], []
    if created:
        contest = Contest(slug=spec.slug, state=ContestState.draft, created_by=actor.user_id, **_fields(spec))
        db.add(contest)
        changes.append("contest created (draft)")
    else:
        new_mode = AccessMode(spec.access_mode)
        if contest.access_mode not in CODE_MODES and new_mode in CODE_MODES and contest.invite_code is not None:
            warnings.append(f"access mode {contest.access_mode.value} -> {new_mode.value}: the invite code this "
                            "contest had before is valid again; if it may have spread, give a new one: "
                            f"judgectl contest code regenerate {contest.slug}")
        for name, value in _fields(spec).items():
            old = getattr(contest, name)
            if old != value:
                setattr(contest, name, value)
                changes.append(f"{name}: {_show(old)} -> {_show(value)}")
    if contest.access_mode in CODE_MODES and contest.invite_code is None:
        contest.invite_code = new_invite_code(db)
        changes.append("invite code created")
    db.flush()
    _apply_groups(db, actor, contest, wanted_groups, now, changes, warnings)

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
                if version is None:
                    for newer in newer_not_ready(db, problem.id, target.version):
                        warnings.append(f"{entry.label}: {entry.slug} v{newer.version} is {newer.status.value} "
                                        f"(report: {checks.where_report(entry.slug, newer.version)}); pinned "
                                        f"v{target.version}, the newest ready version")
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
        _apply_deadlines(db, row, entry, changes, warnings)
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
        raise ContestError(f"{label}: {checks.not_ready_reason(db.get(Problem, row.problem_id).slug, version)}",
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
    if contest.access_mode not in CODE_MODES:
        raise ContestError(f"contest {contest.slug} has no invite code (access mode {contest.access_mode.value})",
                           "contest_is_open")
    contest.invite_code = new_invite_code(db)
    audit.record(db, actor, "contest.code_regenerate", now, object_type="contest", object_id=contest.id,
                 details={"slug": contest.slug})
    return contest.invite_code


def participation_of(db: Session, contest: Contest, user: User, lock: bool = False) -> Optional[Participation]:
    query = select(Participation).where(Participation.contest_id == contest.id, Participation.user_id == user.id)
    return db.execute(query.with_for_update() if lock else query).scalar_one_or_none()


def access_inputs(db: Session, contest: Contest, user: User, lock: bool = False) -> tuple:
    """(participation, in_group): what access.py needs about this student and this contest, from the database."""
    return participation_of(db, contest, user, lock), g.in_contest_group(db, contest, user)


def enter(db: Session, user: User, contest: Contest, now: datetime, code: Optional[str] = None,
          ip: Optional[str] = None) -> Participation:
    """R23, D-021: the first entry of a student creates the participation with joined_via (access.can_enter).
    A participant who did not come in with the code and types the right one gets joined_via = code (В6), audited
    as contest.rejoin_with_code. `user` is the student of the session, never an id from a request."""
    existing, in_group = access_inputs(db, contest, user, lock=True)
    decision = access.can_enter(user, contest, existing, now, code, in_group=in_group)
    if not decision:
        raise ContestError(decision.reason, decision.reason)
    if existing is not None:  # rejoin_with_code
        old = existing.joined_via.value
        existing.joined_via = JoinedVia.code
        db.flush()
        audit.record(db, audit.Actor(user_id=user.id, ip=ip), "contest.rejoin_with_code", now,
                     object_type="participation", object_id=existing.id,
                     details={"contest": contest.slug, "from": old, "to": "code"})
        return existing
    row = Participation(contest_id=contest.id, user_id=user.id, joined_at=now,
                        joined_via=JoinedVia(decision.joined_via))
    db.add(row)
    db.flush()
    return row


def start(db: Session, user: User, contest: Contest, now: datetime) -> Participation:
    """R21: the personal start, once, only with access."""
    row, in_group = access_inputs(db, contest, user, lock=True)
    decision = access.can_start(user, contest, row, now, in_group=in_group)
    if not decision:
        raise ContestError(decision.reason, decision.reason)
    row.started_at = now
    db.flush()
    return row


def participants(db: Session, contest: Contest) -> list:
    return list(db.execute(select(Participation, User).join(User, User.id == Participation.user_id)
                           .where(Participation.contest_id == contest.id).order_by(Participation.joined_at)).all())


def participant_out(contest: Contest, participation: Participation, user: User, member_ids: set) -> dict:
    """One participant for judgectl and the admin API: how they came in (joined_via) and whether that still gives
    access now (access_now: the mode and the current membership, access.has_access)."""
    return {"user_id": user.id, "login": user.login, "display_name": user.display_name,
            "joined_at": participation.joined_at, "started_at": participation.started_at,
            "joined_via": participation.joined_via,
            "access_now": access.has_access(user, contest, participation, user.id in member_ids)}


def counted_submissions(db: Session, contest_problem_id: int, user_id: int, since: datetime,
                        until: Optional[datetime]) -> int:
    """Submissions of a user for a problem inside a window: the ones the per-problem limit applies to."""
    query = select(func.count()).select_from(Submission).where(Submission.contest_problem_id == contest_problem_id,
                                                               Submission.user_id == user_id,
                                                               Submission.created_at >= since)
    if until is not None:
        query = query.where(Submission.created_at < until)
    return db.execute(query).scalar_one()


def extensions_for(db: Session, contest_problem: ContestProblem, user: User) -> list[datetime]:
    """access.py's `extensions`: the student's own extension and those of the groups (not archived) they are in
    now (deadlines.py gives and removes them)."""
    own = select(DeadlineExtension.due_at).where(DeadlineExtension.contest_problem_id == contest_problem.id,
                                                 DeadlineExtension.user_id == user.id)
    by_group = (select(DeadlineExtension.due_at)
                .join(GroupMember, GroupMember.group_id == DeadlineExtension.group_id)
                .join(Group, Group.id == DeadlineExtension.group_id)
                .where(DeadlineExtension.contest_problem_id == contest_problem.id, GroupMember.user_id == user.id,
                       GroupMember.removed_at.is_(None), Group.archived_at.is_(None)))
    return list(db.execute(own.union_all(by_group)).scalars())


def submission_decision(db: Session, user: User, contest: Contest, contest_problem: ContestProblem, now: datetime,
                        language: Optional[str] = None) -> access.Decision:
    """The one way to decide on a submission (dev_seed now, the public API of 4b later): everything access.can_submit
    needs, from the database. `now` is the moment the request was received, taken once by the caller before the
    source is read (a large upload must not become late while it arrives); the submission gets
    created_at = now and late = decision.late, never recomputed. `user` is the student of the session.

    The participation row is locked (SELECT ... FOR UPDATE) before the limit and the cooldown are counted, and the
    caller writes the submission in the same transaction: two requests at once wait for each other, so the second
    sees the first and cannot slip past the cooldown or the limit."""
    participation, in_group = access_inputs(db, contest, user, lock=True)
    last = db.execute(select(func.max(Submission.created_at)).where(
        Submission.user_id == user.id, Submission.contest_problem_id == contest_problem.id)).scalar()
    window = access.contest_window_for(contest, participation)
    counted = counted_submissions(db, contest_problem.id, user.id, window.start, window.end) if window else 0
    return access.can_submit(user, contest, contest_problem, participation, now, language=language,
                             last_submission_at=last, counted_submissions=counted, in_group=in_group,
                             extensions=extensions_for(db, contest_problem, user))


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


def _problem_spec(cp: ContestProblem, slug: str, version: int) -> dict:
    out = {"label": cp.label, "problem": slug, "version": version}
    if cp.points is not None:
        out["points"] = float(cp.points)
    for name in ("available_from", "due_at", "note_md"):
        if getattr(cp, name) is not None:
            out[name] = getattr(cp, name)
    after_due = {key: value.value for key, value in (("statement", cp.after_due_statement),
                                                     ("submit", cp.after_due_submit))
                 if value.value != {"statement": "visible", "submit": "late"}[key]}
    if after_due:
        out["after_due"] = after_due
    return out


def contest_detail(db: Session, c: Contest) -> dict:
    """The contest in the shape of contest.yaml (so it can be sent back to apply unchanged: `spec`), plus what the
    file does not hold: state, invite code, every contest problem row (hidden ones too) and the participant count."""
    rows = db.execute(select(ContestProblem, Problem.slug, ProblemVersion.version, ProblemVersion.status,
                             ProblemVersion.check_report)
                      .join(Problem, Problem.id == ContestProblem.problem_id)
                      .join(ProblemVersion, ProblemVersion.id == ContestProblem.problem_version_id)
                      .where(ContestProblem.contest_id == c.id)
                      .order_by(ContestProblem.hidden, ContestProblem.position, ContestProblem.id)).all()
    names = [grp.name for grp in g.contest_groups(db, c)]
    access_spec = {"mode": c.access_mode.value, **({"groups": names} if names else {})}
    spec = {"format_version": 1, "slug": c.slug, "title": c.title, "description": c.description_md,
            "starts_at": c.starts_at, "ends_at": c.ends_at, "timing_mode": c.timing_mode.value,
            "scoring_mode": c.scoring_mode.value, "icpc_penalty_minutes": c.icpc_penalty_minutes,
            "leaderboard": c.leaderboard.value, "access": access_spec,
            "allow_upsolving": c.allow_upsolving, "submit_cooldown_sec": c.submit_cooldown_sec,
            "max_submissions_per_problem": c.max_submissions_per_problem, "tests_after_end": c.tests_after_end,
            "plagiarism_enabled": c.plagiarism_enabled,
            "problems": [_problem_spec(cp, slug, version) for cp, slug, version, _status, _r in rows if not cp.hidden]}
    if c.personal_duration is not None:
        spec["personal_duration"] = format_duration(c.personal_duration)
    if c.freeze_at is not None:
        spec["freeze_at"] = c.freeze_at
    if c.languages is not None:
        spec["languages"] = c.languages
    return {"spec": spec, "state": c.state, "created_at": c.created_at,
            "invite_code": c.invite_code if c.access_mode in CODE_MODES else None,
            "participants": db.execute(select(func.count()).select_from(Participation)
                                       .where(Participation.contest_id == c.id)).scalar_one(),
            "rows": [{"id": cp.id, "label": cp.label, "problem": slug, "version": version, "status": status,
                      "points": cp.points, "hidden": cp.hidden,
                      "warning": checks.failed_pin_warning(slug, version, (report or {}).get("reason"))
                      if status == VersionStatus.failed else None}
                     for cp, slug, version, status, report in rows]}
