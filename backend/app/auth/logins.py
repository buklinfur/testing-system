"""Logins: normalisation, format check (R3), transliteration and default logins like ivanov.p."""
from __future__ import annotations

import re
from typing import Callable

from ..models import LOGIN_PATTERN

LOGIN_RE = re.compile(LOGIN_PATTERN)
MAX_LOGIN = 32

# Simplified transliteration agreed for module 4a (docs/AUTH.md): no diacritics, soft and hard signs dropped.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}


class LoginError(ValueError):
    pass


def normalize_login(raw: str) -> str:
    """What the user typed -> what is stored and compared: trimmed, lower case."""
    return raw.strip().lower()


def is_valid_login(login: str) -> bool:
    return bool(LOGIN_RE.fullmatch(login))


def checked_login(raw: str) -> str:
    login = normalize_login(raw)
    if not is_valid_login(login):
        raise LoginError("a login is 3-32 characters: latin letters, digits, '.', '_', '-', starting with a letter "
                         f"or a digit: {raw!r}")
    return login


def transliterate(text: str) -> str:
    """Russian -> latin for logins. Hyphens (double surnames) are kept, spaces become hyphens, anything else
    (apostrophes, dots, accented letters) is dropped."""
    out = []
    for ch in text.strip().lower():
        if ch in TRANSLIT:
            out.append(TRANSLIT[ch])
        elif "a" <= ch <= "z" or "0" <= ch <= "9" or ch == "-":
            out.append(ch)
        elif ch.isspace():
            out.append("-")  # "де ла Круз" -> de-la-kruz
    return re.sub(r"-{2,}", "-", "".join(out)).strip("-")


def initial(first_name: str) -> str:
    """The transliteration of the first usable letter in full: "Пётр" -> "p", "Чеслав" -> "ch", "Яна" -> "ya"."""
    for ch in first_name:
        part = transliterate(ch)
        if part:
            return part
    return ""


def default_login(surname: str, first_name: str) -> str:
    """surname.initial, e.g. ("Иванов", "Пётр") -> "ivanov.p", ("Подъячев", "Чеслав") -> "podyachev.ch".
    Raises LoginError if nothing usable is left."""
    last = transliterate(surname)
    first = initial(first_name)
    if not last:
        raise LoginError(f"cannot make a login from the surname {surname!r}: give the login explicitly")
    base = f"{last}.{first}" if first else last
    base = base[:MAX_LOGIN].rstrip(".-_")
    if not is_valid_login(base):  # e.g. shorter than 3 characters
        raise LoginError(f"cannot make a login from {surname!r} {first_name!r}: give the login explicitly")
    return base


def unique_login(base: str, taken: Callable[[str], bool]) -> str:
    """base, then base2, base3 ... (the base is shortened if the suffix would exceed 32 characters)."""
    if not taken(base):
        return base
    for n in range(2, 10_000):
        suffix = str(n)
        candidate = base[:MAX_LOGIN - len(suffix)].rstrip(".-_") + suffix
        if not taken(candidate):
            return candidate
    raise LoginError(f"no free login like {base!r}")


def default_display_name(surname: str, first_name: str) -> str:
    """The name other students see (R4): first name and the initial of the surname, e.g. "Пётр И."."""
    first, last = first_name.strip(), surname.strip()
    if not first:
        return last
    return f"{first} {last[0].upper()}." if last else first
