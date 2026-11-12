"""contest.yaml (R35): parsing and the checks the schema cannot express (docs/CONTESTS.md).

Dates are read as text (PyYAML would otherwise turn `2026-11-10 09:00:00` into a datetime and lose the zone);
a time without an offset is local time of JUDGE_TIMEZONE. Every problem is reported at once.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml

import protocol

from ..timeutil import parse_local

SCHEMA_PATH = Path(protocol.__file__).resolve().parent / "schemas" / "contest.schema.json"
DURATION_RE = re.compile(r"^(?:(\d{1,5})h)?(?:(\d{1,6})m)?$")  # bounded: 10**11 hours overflows timedelta (D-088)
MINUTE_59_RE = re.compile(r"[T ]\d{2}:59(?:Z|[+-]\d{2}:\d{2})?$")  # no seconds, minute 59


class ContestSpecError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("the contest file is not valid:\n  - " + "\n  - ".join(self.problems))


class _TextDates(yaml.SafeLoader):
    """SafeLoader without the timestamp resolver: dates stay strings."""


_TextDates.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


@dataclass
class ProblemEntry:
    label: str
    slug: str
    version: Optional[int]
    points: Optional[Decimal]
    available_from: Optional[datetime] = None
    due_at: Optional[datetime] = None
    after_due_statement: str = "visible"
    after_due_submit: str = "late"
    note_md: Optional[str] = None
    due_at_minute_59: bool = False  # written as HH:59 without seconds: probably meant the end of that minute


@dataclass
class ContestSpec:
    slug: str
    title: str
    description: str
    starts_at: datetime
    ends_at: Optional[datetime]
    timing_mode: str
    personal_duration: Optional[timedelta]
    freeze_at: Optional[datetime]
    scoring_mode: str
    icpc_penalty_minutes: int
    leaderboard: str
    access_mode: str
    groups: list  # names of the groups the contest is addressed to (groups / code_or_groups)
    allow_upsolving: bool
    languages: Optional[list]
    submit_cooldown_sec: int
    max_submissions_per_problem: Optional[int]
    tests_after_end: bool
    plagiarism_enabled: bool
    problems: list = field(default_factory=list)


def parse_duration(text: str) -> timedelta:
    m = DURATION_RE.match(text.strip())
    if not m or not (m.group(1) or m.group(2)):
        raise ValueError(f"not a duration like 90m, 1h30m, 2h: {text!r}")
    return timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))


def _time(data: dict, key: str, tz: tzinfo, problems: list, where: str = ""):
    """data[key] as an aware time, None when absent; a problem (not an exception) when it is not a real time: the
    schema checks only the shape, so 2026-02-30 or 9999-12-31 with an offset get here (D-088)."""
    if not data.get(key):
        return None
    try:
        return parse_local(data[key], tz)
    except ValueError as exc:
        problems.append(f"{where}{key}: {exc}")
        return None


def _schema_problems(data) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed: the contest file cannot be checked (pip install -r requirements.txt)"]
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))
    return [f"schema: {'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}"
            for err in sorted(validator.iter_errors(data), key=lambda e: list(e.path))]


def parse(text, tz: tzinfo) -> ContestSpec:
    """contest.yaml text -> ContestSpec. ContestSpecError lists every problem."""
    try:
        data = yaml.load(text, Loader=_TextDates)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise ContestSpecError([f"not valid YAML: {exc}"]) from exc
    return from_data(data, tz)


def from_data(data, tz: tzinfo) -> ContestSpec:
    """The same contest as already-parsed data (the JSON body of PUT /api/admin/contests/<slug>): one schema, one set
    of rules for the file and the API."""
    if not isinstance(data, dict):
        raise ContestSpecError(["the contest must be a mapping (an object) at the top level"])
    problems = []
    if "join_policy" in data:  # contest.yaml of 4a.2
        problems.append(f"join_policy is replaced by access: {{mode: {data['join_policy']}}}")
        data = {k: v for k, v in data.items() if k != "join_policy"}
    problems += _schema_problems(data)
    if problems:
        raise ContestSpecError(problems)

    when = {k: _time(data, k, tz, problems) for k in ("starts_at", "ends_at", "freeze_at")}
    starts, ends, freeze = when["starts_at"], when["ends_at"], when["freeze_at"]
    try:
        duration = parse_duration(data["personal_duration"]) if data.get("personal_duration") else None
    except ValueError as exc:
        problems.append(f"personal_duration: {exc}")
        duration = None
    if problems:  # a time that is not one: the checks below would compare with nothing
        raise ContestSpecError(problems)
    leaderboard = data.get("leaderboard", "hidden")
    if ends is not None and ends <= starts:
        problems.append("ends_at must be after starts_at")
    if duration is not None and duration <= timedelta(0):
        problems.append("personal_duration must be positive")
    if freeze is not None:
        if ends is None:
            problems.append("freeze_at needs ends_at: a contest without an end has nothing to freeze before")
        elif not starts < freeze < ends:
            problems.append("freeze_at must be after starts_at and before ends_at")
        if leaderboard == "hidden":
            problems.append("freeze_at with leaderboard: hidden makes no sense: nobody sees the leaderboard")
    access = data.get("access", {})
    mode = access.get("mode", "code")
    group_names = [" ".join(name.split()) for name in access.get("groups", [])]
    if mode in ("groups", "code_or_groups") and not group_names:
        problems.append(f"access mode {mode} needs at least one group in access.groups")
    if mode in ("open", "code") and group_names:
        problems.append(f"access.groups is used only with the modes groups and code_or_groups, not {mode}")
    if any(not name for name in group_names):
        problems.append("a group name in access.groups is blank")
    lowered = [name.lower() for name in group_names]
    twice = sorted({name for name in group_names if lowered.count(name.lower()) > 1})
    if twice:
        problems.append(f"each group may occur once (case does not matter): {', '.join(twice)}")
    entries = []
    for p in data["problems"]:
        available, due = (_time(p, k, tz, problems, f"{p['label']}: ") for k in ("available_from", "due_at"))
        if available is not None and due is not None and not available < due:
            problems.append(f"{p['label']}: available_from must be before due_at")
        after_due = p.get("after_due", {})
        entries.append(ProblemEntry(
            p["label"], p["problem"], p.get("version"),
            Decimal(str(p["points"])) if p.get("points") is not None else None,
            available_from=available, due_at=due, after_due_statement=after_due.get("statement", "visible"),
            after_due_submit=after_due.get("submit", "late"), note_md=p.get("note_md") or None,
            due_at_minute_59=isinstance(p.get("due_at"), str) and bool(MINUTE_59_RE.search(p["due_at"]))))
    labels = [p["label"] for p in data["problems"]]
    slugs = [p["problem"] for p in data["problems"]]
    for what, values in (("label", labels), ("problem", slugs)):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            problems.append(f"each {what} may occur once: {', '.join(twice)}")
    if problems:
        raise ContestSpecError(problems)
    return ContestSpec(
        slug=data["slug"], title=data["title"], description=data.get("description", ""),
        starts_at=starts, ends_at=ends, timing_mode=data.get("timing_mode", "fixed"), personal_duration=duration,
        freeze_at=freeze, scoring_mode=data.get("scoring_mode", "points"),
        icpc_penalty_minutes=data.get("icpc_penalty_minutes", 20), leaderboard=leaderboard,
        access_mode=mode, groups=group_names, allow_upsolving=data.get("allow_upsolving", True),
        languages=data.get("languages"), submit_cooldown_sec=data.get("submit_cooldown_sec", 15),
        max_submissions_per_problem=data.get("max_submissions_per_problem"),
        tests_after_end=data.get("tests_after_end", False), plagiarism_enabled=data.get("plagiarism_enabled", False),
        problems=entries,
    )
