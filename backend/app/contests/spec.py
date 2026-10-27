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
DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


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
    join_policy: str
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
    if not isinstance(data, dict):
        raise ContestSpecError(["the file must contain a mapping at the top level"])
    problems = _schema_problems(data)
    if problems:
        raise ContestSpecError(problems)

    when = {k: parse_local(data[k], tz) if data.get(k) else None for k in ("starts_at", "ends_at", "freeze_at")}
    starts, ends, freeze = when["starts_at"], when["ends_at"], when["freeze_at"]
    duration = parse_duration(data["personal_duration"]) if data.get("personal_duration") else None
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
        join_policy=data.get("join_policy", "code"), allow_upsolving=data.get("allow_upsolving", True),
        languages=data.get("languages"), submit_cooldown_sec=data.get("submit_cooldown_sec", 15),
        max_submissions_per_problem=data.get("max_submissions_per_problem"),
        tests_after_end=data.get("tests_after_end", False), plagiarism_enabled=data.get("plagiarism_enabled", False),
        problems=[ProblemEntry(p["label"], p["problem"], p.get("version"),
                               Decimal(str(p["points"])) if p.get("points") is not None else None)
                  for p in data["problems"]],
    )
