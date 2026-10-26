from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Optional

from .common import Verdict


@dataclass
class TestReport:
    __test__ = False
    group: str
    name: str
    index: int  # 1-based over the whole problem
    verdict: Verdict
    time_ms: int = 0
    memory_kb: Optional[int] = None
    exit_code: Optional[int] = None
    signal: Optional[int] = None
    tl_kind: Optional[str] = None  # TL only: "cpu" or "wall"
    message: str = ""  # checker / interactor message
    stderr_excerpt: str = ""
    output_excerpt: str = ""


@dataclass
class GroupReport:
    name: str
    points: Decimal
    score: Decimal
    visibility: str
    feedback: str
    scoring: str
    tests: list = field(default_factory=list)


@dataclass
class JudgeReport:
    verdict: Verdict
    score: Decimal = Decimal(0)
    max_score: Decimal = Decimal(0)
    first_fail_verdict: Verdict = Verdict.OK
    compile_log: str = ""
    groups: list = field(default_factory=list)
    error: Optional[str] = None  # set when verdict is IE

    @property
    def tests(self) -> list:
        return [t for g in self.groups for t in g.tests]

    def to_dict(self) -> dict:
        def conv(obj):
            if isinstance(obj, Decimal):
                return float(obj)
            if isinstance(obj, Verdict):
                return obj.value
            if isinstance(obj, dict):
                return {k: conv(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [conv(v) for v in obj]
            return obj

        return conv(asdict(self))
