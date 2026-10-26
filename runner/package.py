from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class TestFile:
    __test__ = False
    group: str
    name: str
    input_path: Path
    answer_path: Optional[Path]


@dataclass
class Group:
    name: str
    points: Decimal
    scoring: str
    visibility: str
    depends_on: list
    feedback: Optional[str]
    show_in_statement: bool
    tests: list = field(default_factory=list)


@dataclass
class Problem:
    root: Path
    slug: str
    type: str
    titles: dict
    time_ms: Optional[int]
    wall_ms: Optional[int]  # None: derived from time_ms (see Judge._limits)
    memory_mb: Optional[int]
    output_kb: Optional[int]
    per_language: dict
    io_mode: str
    io_input: Optional[str]
    io_output: Optional[str]
    languages: Optional[list]
    checker_type: Optional[str]
    checker_eps: Optional[float]
    checker_path: Optional[Path]
    checker_language: str
    interactor_path: Optional[Path]
    interactor_language: str
    feedback: str
    groups: list
    questions: list
    answers: list
    solutions: list
    submission: dict

    @classmethod
    def load(cls, root) -> "Problem":
        root = Path(root)
        data = yaml.safe_load((root / "problem.yaml").read_text(encoding="utf-8"))
        limits = data.get("limits") or {}
        io = data.get("io") or {}
        checker = data.get("checker") or {}
        interactor = data.get("interactor")
        groups = []
        for raw in data.get("test_groups", []):
            group = Group(
                name=raw["name"],
                points=Decimal(str(raw.get("points", 0))),
                scoring=raw.get("scoring", "sum"),
                visibility=raw.get("visibility", "hidden"),
                depends_on=list(raw.get("depends_on", [])),
                feedback=raw.get("feedback"),
                show_in_statement=bool(raw.get("show_in_statement", False)),
            )
            patterns = raw["tests"] if isinstance(raw["tests"], list) else [raw["tests"]]
            inputs = sorted({p for pattern in patterns for p in root.glob(pattern) if p.is_file()})
            for path in inputs:
                answer = path.with_suffix(".out")
                group.tests.append(TestFile(group.name, path.stem, path, answer if answer.is_file() else None))
            groups.append(group)
        return cls(
            root=root,
            slug=data["slug"],
            type=data["type"],
            titles=data["title"],
            time_ms=limits.get("time_ms"),
            wall_ms=limits.get("wall_ms"),
            memory_mb=limits.get("memory_mb"),
            output_kb=limits.get("output_kb"),
            per_language=limits.get("per_language") or {},
            io_mode=io.get("mode", "stdio"),
            io_input=io.get("input"),
            io_output=io.get("output"),
            languages=data.get("languages"),
            checker_type=checker.get("type"),
            checker_eps=checker.get("eps"),
            checker_path=root / checker["path"] if checker.get("path") else None,
            checker_language=checker.get("language", "cpp"),
            interactor_path=root / interactor["path"] if interactor else None,
            interactor_language=(interactor or {}).get("language", "cpp"),
            feedback=data.get("feedback", "first_fail"),
            groups=groups,
            questions=data.get("questions", []),
            answers=data.get("answers", []),
            solutions=data.get("solutions", []),
            submission=data.get("submission") or {},
        )

    def group_feedback(self, group: Group) -> str:
        return group.feedback or self.feedback

    def max_score(self) -> Decimal:
        return sum((g.points for g in self.groups), Decimal(0))
