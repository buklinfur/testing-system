"""Red team: hostile programs that must be contained by the sandbox.

Each case prints BREACH if it manages to do something forbidden. A case passes when the
verdict is one of the expected ones, "BREACH" is absent from stdout and it finishes in time.
Cases marked local_safe can be run on the UNSAFE local backend (they cannot hurt the host).
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .common import Limits, Verdict
from .compile import compile_program
from .config import RunnerConfig
from .languages import get_profile
from .sandbox import make_factory


@dataclass
class Case:
    name: str
    language: str
    source: str
    expect: set
    local_safe: bool = False
    note: str = ""


def _py(*lines: str) -> str:
    return "\n".join(lines) + "\n"


CASES = [
    Case("infinite-loop-cpp", "cpp", "int main(){ volatile unsigned long x=0; for(;;) x++; }\n", {"TL"}, True, "CPU time limit"),
    Case("sleep-python", "python", _py("import time", "time.sleep(120)"), {"TL"}, True, "wall-time limit"),
    Case("memory-bomb-cpp", "cpp",
         '#include <cstdlib>\n#include <cstring>\nint main(){ for(;;){ char*p=(char*)malloc(1<<20); if(!p) abort(); memset(p,1,1<<20);} }\n',
         {"ML"}, True, "memory limit"),
    Case("memory-bomb-python", "python", _py("a = []", "while True:", "    a.append(bytearray(10**7))"), {"ML"}, True, "memory limit"),
    Case("stack-overflow-cpp", "cpp",
         "int f(int n){ volatile char buf[4096]; buf[0]=n; return f(n+1)+buf[0]; }\nint main(){ return f(0); }\n",
         {"RE", "ML"}, True, "deep recursion"),
    Case("output-flood-python", "python", _py("while True:", "    print('x' * 1000)"), {"OL"}, True, "output limit"),
    Case("disk-fill-python", "python",
         _py("f = open('junk', 'wb')", "while True:", "    f.write(b'x' * 1000000)", "    f.flush()"), {"OL", "RE"}, True,
         "file size limit"),
    Case("fork-bomb-python", "python", _py("import os", "while True:", "    os.fork()"), {"TL", "RE", "ML"}, False,
         "process limit: must finish and leave the machine alive"),
    Case("network-python", "python", _py(
        "import socket",
        "for host, port in [('1.1.1.1', 53), ('127.0.0.1', 5432), ('127.0.0.1', 80), ('127.0.0.1', 8000)]:",
        "    try:",
        "        socket.create_connection((host, port), timeout=1)",
        "        print('BREACH network', host, port)",
        "    except Exception:",
        "        pass",
        "print('done')"), {"OK"}, False, "no network, including localhost services such as PostgreSQL"),
    Case("read-secrets-python", "python", _py(
        "import os",
        "for path in ['/etc/shadow', '/root/.bash_history', '/mnt/c/Windows/System32/config/SAM']:",
        "    try:",
        "        open(path, 'rb').read(1)",
        "        print('BREACH read', path)",
        "    except Exception:",
        "        pass",
        "for path in ['/root', '/home', '/mnt', '/var/local/lib/isolate', '/var/lib/postgresql']:",
        "    try:",
        "        if os.listdir(path):",
        "            print('BREACH list', path)",
        "    except Exception:",
        "        pass",
        "print('done')"), {"OK"}, False, "host files must be invisible"),
    Case("write-outside-python", "python", _py(
        "for path in ['/usr/bin/pwned', '/etc/pwned', '/bin/pwned', '/lib/pwned']:",
        "    try:",
        "        open(path, 'w').write('x')",
        "        print('BREACH write', path)",
        "    except Exception:",
        "        pass",
        "print('done')"), {"OK"}, False, "read-only system directories"),
    Case("setuid-python", "python", _py(
        "import os",
        "try:",
        "    os.setuid(0)",
        "    print('BREACH setuid, uid =', os.getuid())",
        "except Exception:",
        "    pass",
        "print('done')"), {"OK"}, False, "no privilege escalation"),
    Case("mount-python", "python", _py(
        "import ctypes",
        "libc = ctypes.CDLL(None, use_errno=True)",
        "r = libc.mount(b'none', b'/tmp', b'tmpfs', 0, None)",
        "if r == 0:",
        "    print('BREACH mount')",
        "print('done')"), {"OK"}, False, "mount must fail"),
    Case("shell-escape-python", "python", _py(
        "import subprocess",
        "try:",
        "    r = subprocess.run(['/bin/sh', '-c', 'cat /etc/shadow'], capture_output=True, timeout=5)",
        "    if r.returncode == 0 and r.stdout:",
        "        print('BREACH shell read')",
        "except Exception:",
        "    pass",
        "print('done')"), {"OK", "RE"}, False, "a shell inside the box sees nothing more"),
]


def run_redteam(config: RunnerConfig, only_local_safe: bool = False) -> list:
    factory = make_factory(config)
    limits = Limits(time_ms=1000, wall_ms=3000, memory_mb=256, processes=16, fsize_kb=1024)
    rows = []
    for case in CASES:
        if only_local_safe and not case.local_safe:
            continue
        profile = get_profile(case.language)
        outcome = compile_program(factory, profile, {profile.default_source: case.source.encode()}, None, config)
        if not outcome.ok:
            rows.append({"name": case.name, "ok": False, "verdict": "CE", "detail": outcome.log[:200], "seconds": 0.0})
            continue
        started = time.monotonic()
        with factory(0) as box:
            for name, data in outcome.program.files.items():
                box.put_file(name, data, executable=name in outcome.program.executables)
            run = box.run(outcome.program.argv, limits, env=outcome.program.env, stdout_cap=65536)
        seconds = time.monotonic() - started
        breach = b"BREACH" in run.stdout
        ok = run.verdict.value in case.expect and not breach and seconds < 15
        detail = case.note
        if breach:
            detail = "BREACH: " + run.stdout.decode(errors="replace")[:200]
        elif run.verdict.value not in case.expect:
            detail = f"expected {sorted(case.expect)}, got {run.verdict.value} {run.message} {run.stderr[:150]!r}"
        rows.append({"name": case.name, "ok": ok, "verdict": run.verdict.value, "detail": detail, "seconds": round(seconds, 1)})
    return rows
