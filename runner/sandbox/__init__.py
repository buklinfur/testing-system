from __future__ import annotations

from typing import Callable

from ..config import RunnerConfig
from .base import Sandbox

SandboxFactory = Callable[[int], Sandbox]


def make_factory(config: RunnerConfig) -> SandboxFactory:
    """Return factory(slot) -> Sandbox. Slot 0 runs the solution, slot 1 the checker/interactor."""
    if config.backend == "isolate":
        from .isolate import IsolateSandbox

        return lambda slot: IsolateSandbox(config.box_id_base + slot, config.isolate_bin)
    if config.backend == "local":
        from .local import LocalSandbox

        return lambda slot: LocalSandbox()
    raise ValueError(f"unknown sandbox backend: {config.backend!r}")
