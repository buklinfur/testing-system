"""CPU topology and the plan "which worker gets which CPUs / isolate boxes".

All functions are pure (the sysfs root is a parameter) so they are unit-tested without hardware.

Policy: a worker gets *all hardware threads of one physical core* (two on Ryzen 7 7700). The solution
and the interactor of an interactive task then run in parallel without fighting other workers, and
two workers never share a core. The first `reserve_cores` cores stay free for the system.

Caveat (WSL2): the "cores" Linux sees are virtual processors. Windows decides which physical core
runs them, so pinning isolates workers from each other inside the VM, not from the Windows host.
Judging uses CPU time, not wall time, which keeps the measurements reasonably stable anyway.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

SYSFS_CPU = Path("/sys/devices/system/cpu")


def parse_cpu_list(text: str) -> list:
    """'0-2,8,10-11' -> [0, 1, 2, 8, 10, 11]."""
    cpus: set = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not match:
            raise ValueError(f"bad cpu list: {text!r}")
        low = int(match.group(1))
        high = int(match.group(2) or low)
        if high < low:
            raise ValueError(f"bad cpu range: {part!r}")
        cpus.update(range(low, high + 1))
    return sorted(cpus)


def format_cpu_list(cpus: Iterable[int]) -> str:
    items = sorted(set(cpus))
    parts, i = [], 0
    while i < len(items):
        j = i
        while j + 1 < len(items) and items[j + 1] == items[j] + 1:
            j += 1
        parts.append(str(items[i]) if i == j else f"{items[i]}-{items[j]}")
        i = j + 1
    return ",".join(parts)


def allowed_cpus() -> list:
    try:
        return sorted(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        return list(range(os.cpu_count() or 1))


def read_cores(sysfs: Path = SYSFS_CPU, allowed: Optional[Iterable[int]] = None) -> list:
    """Physical cores as sorted tuples of logical CPU ids, ordered by their first CPU.

    Falls back to one core per CPU if the topology files are missing (then SMT siblings are unknown;
    use an explicit plan in that case).
    """
    allowed_set = set(allowed if allowed is not None else allowed_cpus())
    cores: dict = {}
    for cpu in sorted(allowed_set):
        siblings_file = Path(sysfs) / f"cpu{cpu}" / "topology" / "thread_siblings_list"
        try:
            siblings = tuple(c for c in parse_cpu_list(siblings_file.read_text()) if c in allowed_set)
        except (OSError, ValueError):
            siblings = (cpu,)
        cores[siblings or (cpu,)] = None
    return sorted(cores, key=lambda core: core[0])


@dataclass(frozen=True)
class WorkerSlot:
    index: int
    cpus: tuple  # empty tuple = no pinning
    box_base: int  # the worker uses box_base and box_base + 1

    @property
    def boxes(self) -> tuple:
        return (self.box_base, self.box_base + 1)


def parse_explicit_plan(text: str) -> list:
    """'1-2;3-4;5' -> [(1, 2), (3, 4), (5,)]: one entry per worker, separated by ';'."""
    plan = [tuple(parse_cpu_list(chunk)) for chunk in text.split(";") if chunk.strip()]
    seen: set = set()
    for cpus in plan:
        if not cpus:
            raise ValueError("empty cpu set in the plan")
        if seen & set(cpus):
            raise ValueError("cpu sets of different workers overlap")
        seen |= set(cpus)
    return plan


def plan_workers(cores: list, workers: int, reserve_cores: int = 1, box_base: int = 0,
                 explicit: Optional[list] = None, pin: bool = True) -> list:
    """Return one WorkerSlot per worker.

    explicit: list of cpu tuples (overrides the automatic choice; its length is the worker count).
    pin=False: no CPU affinity (empty cpus), only box ids are assigned.
    If there are fewer free cores than requested workers, the number of workers is reduced
    (never share a core silently: that would distort time measurements).
    """
    if explicit is not None:
        sets = [tuple(c) for c in explicit]
    elif not pin:
        sets = [()] * workers
    else:
        free = cores[reserve_cores:] if len(cores) > reserve_cores else []
        if not free:
            raise ValueError(f"no free cores: {len(cores)} cores, {reserve_cores} reserved")
        sets = [tuple(core) for core in free[:workers]]
    return [WorkerSlot(i, cpus, box_base + 2 * i) for i, cpus in enumerate(sets)]


def apply_affinity(cpus: tuple) -> None:
    """Pin the current process (children inherit it: isolate, compilers, solutions)."""
    if cpus:
        os.sched_setaffinity(0, set(cpus))
