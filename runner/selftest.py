"""`sandbox-check`: verifies the assumptions the isolate backend relies on."""
from __future__ import annotations

import os
import threading

from .common import Limits, Verdict
from .compile import compile_program
from .config import RunnerConfig
from .languages import get_profile
from .sandbox import make_factory

LIMITS = Limits(time_ms=1000, wall_ms=3000, memory_mb=128, processes=8, fsize_kb=1024)


def _prepare(factory, config, language, source):
    profile = get_profile(language)
    outcome = compile_program(factory, profile, {profile.default_source: source.encode()}, None, config)
    if not outcome.ok:
        raise RuntimeError(f"compile failed: {outcome.log}")
    return outcome.program


def _load(box, program):
    for name, data in program.files.items():
        box.put_file(name, data, executable=name in program.executables)


def sandbox_check(config: RunnerConfig) -> list:
    factory = make_factory(config)
    checks = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - report everything
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append({"name": name, "ok": ok, "detail": detail})

    def cpp_hello():
        program = _prepare(factory, config, "cpp", '#include <iostream>\nint main(){ std::cout << "hello"; }\n')
        with factory(0) as box:
            _load(box, program)
            run = box.run(program.argv, LIMITS, env=program.env, stdout_cap=1024)
        return run.verdict == Verdict.OK and run.stdout == b"hello", f"{run.verdict.value} stdout={run.stdout!r} {run.message}"

    def stdin_from_file():
        program = _prepare(factory, config, "python", "print(input()[::-1])\n")
        with factory(0) as box:
            _load(box, program)
            box.put_file("__input", b"abc\n")
            run = box.run(program.argv, LIMITS, stdin_file="__input", env=program.env, stdout_cap=1024)
        return run.stdout.strip() == b"cba", f"{run.verdict.value} stdout={run.stdout!r} stderr={run.stderr[:100]!r}"

    def exit_code_is_re():
        program = _prepare(factory, config, "python", "import sys\nsys.exit(3)\n")
        with factory(0) as box:
            _load(box, program)
            run = box.run(program.argv, LIMITS, env=program.env, stdout_cap=1024)
        return run.verdict == Verdict.RE and run.exit_code == 3, f"{run.verdict.value} exit={run.exit_code}"

    def time_limit():
        program = _prepare(factory, config, "python", "while True:\n    pass\n")
        with factory(0) as box:
            _load(box, program)
            run = box.run(program.argv, LIMITS, env=program.env, stdout_cap=1024)
        return run.verdict == Verdict.TL, f"{run.verdict.value} cpu={run.time_ms}ms wall={run.wall_ms}ms"

    def pipes_between_two_boxes():
        # the interactive protocol needs stdin/stdout inheritance and a second box
        echo = _prepare(factory, config, "python", "line = input()\nprint(line.upper(), flush=True)\n")
        talker = _prepare(factory, config, "python",
                          "print('ping', flush=True)\nreply = input()\nimport sys\nsys.exit(0 if reply == 'PING' else 1)\n")
        with factory(0) as box_a, factory(1) as box_b:
            _load(box_a, echo)
            _load(box_b, talker)
            a_in, b_out = os.pipe()
            b_in, a_out = os.pipe()
            try:
                ha = box_a.start(echo.argv, LIMITS, stdin_fd=a_in, stdout_fd=a_out, env=echo.env)
                hb = box_b.start(talker.argv, LIMITS, stdin_fd=b_in, stdout_fd=b_out, env=talker.env)
            finally:
                for fd in (a_in, b_out, b_in, a_out):
                    os.close(fd)
            res = {}
            threads = [threading.Thread(target=lambda k=k, h=h: res.__setitem__(k, h.wait())) for k, h in (("a", ha), ("b", hb))]
            [t.start() for t in threads]
            [t.join() for t in threads]
        ok = res["a"].verdict == Verdict.OK and res["b"].verdict == Verdict.OK
        return ok, f"echo={res['a'].verdict.value} talker={res['b'].verdict.value} {res['b'].stderr[:100]!r}"

    check("compile and run C++, stdout is captured", cpp_hello)
    check("stdin from a file inside the box", stdin_from_file)
    check("non-zero exit code is reported as RE", exit_code_is_re)
    check("CPU time limit gives TL", time_limit)
    check("two boxes talk through pipes (interactive tasks)", pipes_between_two_boxes)
    return checks
