"""Safe unpacking of a problem package (R27): our own loop, never ZipFile.extractall.

Before anything is written every entry name is checked: no absolute paths, drive letters, backslashes, `..`
or empty parts, no symbolic links or special files (by the Unix mode in external_attr), no duplicates, not
more than max_files entries. Then files are streamed out with byte counters: the sizes written in the zip
headers are never trusted (a lying header ends in a CRC error from zipfile), the ratio unpacked / compressed
is checked per file and in total, and so is the unpacked total. Junk (protocol.problem_package.is_junk) is
skipped. When every file sits in one top-level folder (a zipped package folder), that folder is removed.
A directory import gets the same checks through check_directory.
"""
from __future__ import annotations

import io
import os
import re
import stat
import zipfile
from pathlib import Path

from protocol.problem_package import PackageError, is_junk, list_files

from ..settings import ImportSettings

CHUNK = 64 * 1024
MIB = 1024 * 1024


class ArchiveError(PackageError):
    """The archive itself is refused (before or while unpacking); `problems` lists every reason."""


def _mb(n: int) -> str:
    return f"{n / MIB:.1f} MB"


def _name_problem(raw: str) -> str | None:
    if "\\" in raw:
        return "backslashes are not allowed in names"
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return "absolute paths are not allowed"
    parts = raw.rstrip("/").split("/")
    if ".." in parts:
        return "'..' is not allowed in names"
    if any(part in ("", ".") for part in parts):
        return "empty or '.' parts are not allowed in names"
    return None


def _kind_problem(info: zipfile.ZipInfo) -> str | None:
    kind = stat.S_IFMT(info.external_attr >> 16)
    if kind == stat.S_IFLNK:
        return "symbolic links are not allowed"
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
        return "only regular files and folders are allowed"
    return None


def unpack_zip(data: bytes, dest: Path, limits: ImportSettings) -> list[str]:
    """Unpack a zip into the empty folder `dest`; the sorted relative paths of the files. ArchiveError if refused."""
    if len(data) > limits.max_archive_bytes:
        raise ArchiveError([f"the archive is {_mb(len(data))}, the limit is {_mb(limits.max_archive_bytes)}"])
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ArchiveError([f"not a zip archive ({exc})"]) from exc
    infos = archive.infolist()
    if len(infos) > limits.max_files:
        raise ArchiveError([f"the archive has {len(infos)} entries, the limit is {limits.max_files}"])

    problems, files, seen = [], [], set()
    for info in infos:
        name = info.filename
        why = _name_problem(name) or _kind_problem(info)
        if why:
            problems.append(f"{name}: {why}")
            continue
        rel = name.rstrip("/")
        if rel in seen:
            problems.append(f"{name}: the name occurs twice")
            continue
        seen.add(rel)
        if not info.is_dir() and not is_junk(rel):
            files.append((rel, info))
    if problems:
        raise ArchiveError(problems)

    tops = {rel.split("/", 1)[0] for rel, _info in files}
    strip = len(tops) == 1 and all("/" in rel for rel, _info in files)
    dest = Path(dest)
    base = dest.resolve()
    total = compressed = 0
    out = []
    for rel, info in files:
        target_rel = rel.split("/", 1)[1] if strip else rel
        target = dest / target_rel
        if not target.resolve().is_relative_to(base):  # unreachable after the name checks; kept as a guard
            raise ArchiveError([f"{rel}: the path leaves the package"])
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        compressed += info.compress_size
        allowed = limits.max_ratio * max(info.compress_size, 1)
        try:
            with archive.open(info) as src, \
                    os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as dst:
                while chunk := src.read(CHUNK):
                    written += len(chunk)
                    total += len(chunk)
                    if written > allowed:
                        raise ArchiveError([f"{rel}: unpacks to more than {limits.max_ratio} times its compressed "
                                            f"size ({info.compress_size} bytes): refused as a zip bomb"])
                    if total > limits.max_unpacked_bytes:
                        raise ArchiveError([f"the archive unpacks to more than {_mb(limits.max_unpacked_bytes)}"])
                    dst.write(chunk)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, NotImplementedError, RuntimeError) as exc:
            raise ArchiveError([f"{rel}: cannot be unpacked ({exc})"]) from exc
        except FileExistsError as exc:  # a file and a folder, or two names that differ only in the top folder
            raise ArchiveError([f"{rel}: the name occurs twice"]) from exc
        out.append(target_rel)
    if total > limits.max_ratio * max(compressed, 1):
        raise ArchiveError([f"the archive unpacks to more than {limits.max_ratio} times its compressed size: "
                            "refused as a zip bomb"])
    return sorted(out)


def check_directory(root: Path, limits: ImportSettings) -> list[str]:
    """The files of a package folder (junk skipped) with the same limits; symlinks and special files refused."""
    files = list_files(root)
    if len(files) > limits.max_files:
        raise ArchiveError([f"the package has {len(files)} files, the limit is {limits.max_files}"])
    total = sum(os.lstat(Path(root) / rel).st_size for rel in files)
    if total > limits.max_unpacked_bytes:
        raise ArchiveError([f"the package is {_mb(total)}, the limit is {_mb(limits.max_unpacked_bytes)}"])
    return files
