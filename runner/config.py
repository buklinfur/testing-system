from __future__ import annotations

from dataclasses import dataclass


# Manual runs (runner.cli, tools) use these boxes, above every worker's range, so that a check run by hand
# while the services work never takes a box a worker needs. Workers must stay below (worker/config.py).
MANUAL_BOX_BASE = 100


@dataclass
class RunnerConfig:
    # --- sandbox backend -------------------------------------------------
    backend: str = "isolate"  # "isolate" (real) | "local" (UNSAFE, development only)
    isolate_bin: str = "isolate"
    box_id_base: int = 0  # a worker uses box ids box_id_base and box_id_base + 1

    # --- hard system caps: apply even when a problem sets no limit --------
    time_ms_cap: int = 10_000
    wall_ms_cap: int = 30_000
    memory_mb_cap: int = 1024
    output_kb_cap: int = 65_536
    processes: int = 16
    stderr_cap_bytes: int = 65_536

    # --- compilation -----------------------------------------------------
    compile_time_ms: int = 15_000
    compile_wall_ms: int = 30_000
    compile_memory_mb: int = 1024
    compile_processes: int = 64
    compile_fsize_kb: int = 65_536
    max_executable_bytes: int = 64 * 1024 * 1024

    # --- submissions -----------------------------------------------------
    max_source_bytes: int = 256 * 1024
    max_archive_bytes: int = 1024 * 1024
    max_unpacked_bytes: int = 8 * 1024 * 1024
    max_files: int = 20

    # --- jury programs (checker, interactor) -----------------------------
    jury_time_ms: int = 10_000
    jury_memory_mb: int = 512

    # --- jury program cache ----------------------------------------------
    # Part of the cache key of compiled checkers/interactors (e.g. a hash of `g++ --version`).
    toolchain_id: str = ""

    # --- behaviour -------------------------------------------------------
    run_all_tests: bool = False  # False: stop a all_or_nothing group at its first failure
    message_cap_bytes: int = 1024
    excerpt_cap_bytes: int = 4096
