from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .common import Limits, SandboxError, Verdict
from .config import RunnerConfig
from .explain import sanitize_compile_log
from .languages import EXECUTABLE_NAME, LanguageProfile


@dataclass
class Program:
    """Everything needed to start a prepared program inside a fresh box."""

    files: dict
    executables: set
    argv: list
    env: dict = field(default_factory=dict)


@dataclass
class CompileOutcome:
    ok: bool
    verdict: Verdict  # OK, CE or IE
    program: Optional[Program] = None
    log: str = ""


def _expand(template: tuple, sources: list, entry: str) -> list:
    argv = []
    for token in template:
        if token == "{sources}":
            argv.extend(sources)
        else:
            argv.append(token.format(out=EXECUTABLE_NAME, entry=entry))
    return argv


def compile_program(factory, profile: LanguageProfile, files: dict, entry: Optional[str],
                    config: RunnerConfig, slot: int = 0) -> CompileOutcome:
    """Build (or syntax-check) a program inside a sandbox box."""
    limits = Limits(
        time_ms=config.compile_time_ms,
        wall_ms=config.compile_wall_ms,
        memory_mb=config.compile_memory_mb,
        processes=config.compile_processes,
        fsize_kb=config.compile_fsize_kb,
    )
    if profile.compile is None:
        entry = entry or profile.default_source
        if entry not in files:
            return CompileOutcome(False, Verdict.CE, log=f"Не найден файл запуска {entry}")
        template, sources = profile.check, []
    else:
        entry = ""
        sources = sorted(n for n in files if n.lower().endswith(profile.extensions))
        if not sources:
            exts = ", ".join(profile.extensions)
            return CompileOutcome(False, Verdict.CE, log=f"Не найдено ни одного файла с исходным кодом ({exts})")
        template = profile.compile

    with factory(slot) as box:
        for name, data in files.items():
            box.put_file(name, data)
        env = {"TMPDIR": box.inside_path or "."}
        run = box.run(_expand(template, sources, entry), limits, env=env, stdout_cap=65536)
        if run.verdict == Verdict.IE:
            return CompileOutcome(False, Verdict.IE, log=run.message)
        if run.verdict != Verdict.OK:
            text = (run.stderr + run.stdout).decode("utf-8", errors="replace")
            reasons = {
                Verdict.TL: "\n[превышено время компиляции]",
                Verdict.ML: "\n[превышена память при компиляции]",
                Verdict.OL: "\n[слишком большой вывод компилятора]",
            }
            return CompileOutcome(False, Verdict.CE, log=sanitize_compile_log(text + reasons.get(run.verdict, "")))
        if profile.compile is None:
            program = Program(files=dict(files), executables=set(), argv=list(_expand(profile.run, [], entry)))
            return CompileOutcome(True, Verdict.OK, program)
        try:
            exe = box.read_file(EXECUTABLE_NAME, config.max_executable_bytes)
        except SandboxError as exc:
            return CompileOutcome(False, Verdict.IE, log=str(exc))
        if exe is None:
            return CompileOutcome(False, Verdict.IE, log="компилятор не создал исполняемый файл")
        argv = _expand(profile.run, [], entry)
        program = Program(files={EXECUTABLE_NAME: exe}, executables={EXECUTABLE_NAME}, argv=argv)
        return CompileOutcome(True, Verdict.OK, program)
