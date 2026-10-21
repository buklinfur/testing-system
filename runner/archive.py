from __future__ import annotations

import io
import re
import stat
import zipfile
from pathlib import PurePosixPath

from .config import RunnerConfig
from .languages import LanguageProfile


class SubmissionError(Exception):
    """The submission is malformed (bad archive, too large, ...). Shown to the student."""


def _check_name(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name or re.match(r"^[A-Za-z]:", name):
        raise SubmissionError(f"недопустимое имя файла в архиве: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SubmissionError(f"недопустимый путь в архиве: {name!r}")
    return path


def extract_submission(data: bytes, is_archive: bool, profile: LanguageProfile, config: RunnerConfig) -> dict:
    """Return {relative path: bytes}. Rejects zip-slip, symlinks, encrypted files and zip bombs."""
    if not is_archive:
        if len(data) > config.max_source_bytes:
            raise SubmissionError("исходный файл слишком большой")
        return {profile.default_source: data}

    if len(data) > config.max_archive_bytes:
        raise SubmissionError("архив слишком большой")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SubmissionError("не удалось открыть zip-архив") from None

    infos = [i for i in archive.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
    if not infos:
        raise SubmissionError("архив пуст")
    if len(infos) > config.max_files:
        raise SubmissionError(f"в архиве больше {config.max_files} файлов")

    parts_list = []
    for info in infos:
        if info.flag_bits & 0x1:
            raise SubmissionError("архив зашифрован")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise SubmissionError("символические ссылки в архиве запрещены")
        parts_list.append(_check_name(info.filename).parts)

    # an archive of a folder: strip the single common top-level directory
    strip = 0
    if all(len(p) > 1 for p in parts_list) and len({p[0] for p in parts_list}) == 1:
        strip = 1

    files, total = {}, 0
    for info, parts in zip(infos, parts_list):
        remaining = config.max_unpacked_bytes - total
        with archive.open(info) as fh:
            content = fh.read(remaining + 1)  # headers may lie: read with a hard cap
        if len(content) > remaining:
            raise SubmissionError("слишком большой размер файлов после распаковки")
        total += len(content)
        name = "/".join(parts[strip:])
        if name in files:
            raise SubmissionError(f"файл повторяется в архиве: {name}")
        files[name] = content
    return files
