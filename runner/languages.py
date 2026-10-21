from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LanguageProfile:
    """How to build and run one language. Placeholders: {sources} {out} {entry}."""

    key: str
    name: str
    extensions: tuple
    default_source: str  # file name for a single-file submission
    compile: Optional[tuple]  # None for interpreted languages
    check: Optional[tuple]  # syntax check for interpreted languages
    run: tuple
    time_multiplier: float = 1.0
    memory_multiplier: float = 1.0


PROFILES = {
    "python": LanguageProfile(
        key="python",
        name="Python 3",
        extensions=(".py",),
        default_source="main.py",
        compile=None,
        check=("/usr/bin/python3", "-m", "py_compile", "{entry}"),
        run=("/usr/bin/python3", "{entry}"),
    ),
    "c": LanguageProfile(
        key="c",
        name="C (gcc, C11)",
        extensions=(".c",),
        default_source="main.c",
        compile=("/usr/bin/gcc", "-O2", "-std=c11", "-o", "{out}", "{sources}", "-lm"),
        check=None,
        run=("./{out}",),
    ),
    "cpp": LanguageProfile(
        key="cpp",
        name="C++17 (g++)",
        extensions=(".cpp", ".cc", ".cxx"),
        default_source="main.cpp",
        compile=("/usr/bin/g++", "-O2", "-std=c++17", "-o", "{out}", "{sources}"),
        check=None,
        run=("./{out}",),
    ),
}

HEADER_EXTENSIONS = (".h", ".hpp", ".hh", ".inc")
EXECUTABLE_NAME = "sol"


def get_profile(key: str) -> LanguageProfile:
    try:
        return PROFILES[key]
    except KeyError:
        raise ValueError(f"unknown language: {key!r}") from None


def guess_language(filename: str) -> str:
    lower = filename.lower()
    for profile in PROFILES.values():
        if lower.endswith(profile.extensions):
            return profile.key
    raise ValueError(f"cannot guess language of {filename!r}")
