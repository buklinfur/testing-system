"""The class list of `judgectl user import` and POST /api/admin/users/import (R7): parsing, and the result file.
Pure: no database, no files.

    фамилия;имя[;логин][;display_name][;класс]

The encoding is detected (UTF-8, UTF-8 with BOM, cp1251: what Excel on Windows saves), so is the
delimiter (`;` or `,`, from the first non-empty line). A header line is optional (one of its cells is
"фамилия" / "surname"); with a header the columns are taken by their names, in any order ("Фамилия;Имя;Класс"
works), without one by position, the class being the fifth (В4: a three-column file without a header must not
turn "10a" into a login). The class creates the group or adds to the one of that name (users.import_students).
Empty lines and trailing empty cells are ignored. Every problem is reported with its line number, all at once,
so the file can be fixed in one go.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from typing import Optional

from ..auth.logins import LoginError, checked_login, default_login

HEADER_WORDS = frozenset({"фамилия", "surname", "last_name", "lastname", "last name"})
COLUMNS = {  # header cell (lower case) -> field
    **{word: "surname" for word in HEADER_WORDS},
    **{word: "first_name" for word in ("имя", "first_name", "firstname", "first name")},
    **{word: "login" for word in ("логин", "login")},
    **{word: "display_name" for word in ("display_name", "имя в рейтинге", "отображаемое имя")},
    **{word: "group" for word in ("класс", "class", "группа", "group")},
}
POSITIONS = ("surname", "first_name", "login", "display_name", "group")
DISPLAY_NAME_MAX = 128
GROUP_MAX = 64


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
    group: Optional[str] = None  # the class: spaces collapsed; None = no group


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


def _header(cells: list, line: int) -> tuple[Optional[tuple], list]:
    """The fields of a header line by position, and its problems; (None, []) when the line is not a header."""
    if not any(c.lower() in HEADER_WORDS for c in cells):  # a header names the surname column somewhere
        return None, []
    fields = tuple(COLUMNS.get(c.lower()) for c in cells)
    problems = [f"line {line}: unknown column {cell!r} (known: фамилия, имя, логин, display_name, класс)"
                for cell, name in zip(cells, fields) if name is None]
    twice = sorted({name for name in fields if name and fields.count(name) > 1})
    if twice:
        problems.append(f"line {line}: a column occurs twice: {', '.join(twice)}")
    if "first_name" not in fields:
        problems.append(f"line {line}: the header has no column имя")
    return fields, problems


def parse(data: bytes) -> Parsed:
    text, encoding = decode(data)
    delimiter = sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows, problems, logins, first, fields = [], [], {}, True, POSITIONS
    for cells in reader:
        line = reader.line_num
        cells = [c.strip() for c in cells]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue
        if first:
            first = False
            header, header_problems = _header(cells, line)
            if header is not None:
                if header_problems:
                    raise CsvImportError(header_problems)  # nothing below can be read without the columns
                fields = header
                continue
        if not 2 <= len(cells) <= len(fields):
            expected = (f"the {len(fields)} column(s) of the header" if fields is not POSITIONS else
                        f"surname{delimiter}first name[{delimiter}login][{delimiter}display name][{delimiter}class]")
            problems.append(f"line {line}: expected {expected}, got {len(cells)} field(s)")
            continue
        values = {name: cell for name, cell in zip(fields, cells)}
        surname, first_name = values.get("surname", ""), values.get("first_name", "")
        login = values.get("login") or None
        display_name = values.get("display_name") or None
        group = " ".join(values.get("group", "").split()) or None
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
        if group and len(group) > GROUP_MAX:
            problems.append(f"line {line}: the class is longer than {GROUP_MAX} characters")
            continue
        rows.append(Row(line, surname, first_name, login, display_name, group))
    if not rows and not problems:
        problems.append("no students in the file")
    if problems:
        raise CsvImportError(problems)
    return Parsed(rows, encoding, delimiter)


# ----------------------------------------------------------------------------- the result: logins and passwords
OUT_HEADER = ["фамилия", "имя", "логин", "пароль", "display_name"]


def result_csv(results: list) -> str:
    """The result of an import as Excel on Windows opens it: ';', CRLF, a BOM (the caller adds it when writing
    bytes: encoding utf-8-sig). `results`: dicts with surname, first_name, login, password, display_name and
    group; the column класс is there only when some student has one."""
    with_group = any(r.get("group") for r in results)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    writer.writerow(OUT_HEADER + (["класс"] if with_group else []))
    for r in results:
        writer.writerow([r["surname"], r["first_name"], r["login"], r["password"], r["display_name"]]
                        + ([r.get("group") or ""] if with_group else []))
    return out.getvalue()
