"""Parsing the class list for `judgectl user import` (R7). Pure: no database, no files.

    фамилия;имя[;логин][;display_name]

The encoding is detected (UTF-8, UTF-8 with BOM, cp1251: what Excel on Windows saves), so is the
delimiter (`;` or `,`, from the first non-empty line). A header line is optional (its first cell is
"фамилия" / "surname"). Empty lines and trailing empty cells are ignored. Every problem is reported with
its line number, all at once, so the file can be fixed in one go.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Optional

from ..auth.logins import LoginError, checked_login, default_login

HEADER_WORDS = frozenset({"фамилия", "surname", "last_name", "lastname", "last name"})
DISPLAY_NAME_MAX = 128


class CsvImportError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"the CSV file has {len(problems)} problem(s), nothing was imported:\n  "
                         + "\n  ".join(problems))


@dataclass(frozen=True)
class Row:
    line: int
    surname: str
    first_name: str
    login: Optional[str] = None  # normalised; None = surname.initial
    display_name: Optional[str] = None  # None = "Имя Ф."


@dataclass(frozen=True)
class Parsed:
    rows: list[Row]
    encoding: str
    delimiter: str


def decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        try:
            data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "cp1251"
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError as exc:
        raise CsvImportError([f"the file is neither UTF-8 nor cp1251 (byte {exc.start})"]) from exc


def sniff_delimiter(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return ";" if ";" in line or "," not in line else ","
    return ";"


def parse(data: bytes) -> Parsed:
    text, encoding = decode(data)
    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows, problems, logins, first = [], [], {}, True
    for cells in reader:
        line = reader.line_num
        cells = [c.strip() for c in cells]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if first:
            first = False
            if cells[0].lower() in HEADER_WORDS:
                continue
        if not 2 <= len(cells) <= 4:
            problems.append(f"line {line}: expected surname{delimiter}first name[{delimiter}login]"
                            f"[{delimiter}display name], got {len(cells)} field(s)")
            continue
        surname, first_name = cells[0], cells[1]
        login = (cells[2] if len(cells) > 2 else "") or None
        display_name = (cells[3] if len(cells) > 3 else "") or None
        if not surname or not first_name:
            problems.append(f"line {line}: the surname and the first name are required")
            continue
        try:
            login = checked_login(login) if login else None
            if login is None:
                default_login(surname, first_name)  # fails now rather than halfway through the import
        except LoginError as exc:
            problems.append(f"line {line}: {exc}")
            continue
        if login is not None:
            if login in logins:
                problems.append(f"line {line}: login {login!r} is already used on line {logins[login]}")
                continue
            logins[login] = line
        if display_name and len(display_name) > DISPLAY_NAME_MAX:
            problems.append(f"line {line}: the display name is longer than {DISPLAY_NAME_MAX} characters")
            continue
        rows.append(Row(line, surname, first_name, login, display_name))
    if not rows and not problems:
        problems.append("no students in the file")
    if problems:
        raise CsvImportError(problems)
    return Parsed(rows, encoding, delimiter)
