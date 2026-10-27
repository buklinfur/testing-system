"""Password hashing (argon2id), password policy and the generator of student passwords."""
from __future__ import annotations

import math
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..models import Role

HERE = Path(__file__).resolve().parent
MIN_LENGTH = {Role.student: 8, Role.admin: 12}
MAX_LENGTH = 128  # argon2 does not care, but nobody types more and huge inputs cost CPU
DIGITS = "23456789"  # no 0 and 1: they look like o and l

_hasher = PasswordHasher()  # argon2id with the library's RFC 9106 defaults


class PasswordPolicyError(ValueError):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> tuple[bool, Optional[str]]:
    """(matches, new hash if the stored one uses outdated parameters). Unusable hashes ("!") never match."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, None
    return True, (_hasher.hash(password) if _hasher.check_needs_rehash(password_hash) else None)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_hex(16))


def burn_verify_time(password: str) -> None:
    """Spend the time of one real verification: "no such login" must look like "wrong password"."""
    verify_password(_dummy_hash(), password)


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    lines = (HERE / "common_passwords.txt").read_text(encoding="utf-8").splitlines()
    return frozenset(line.strip().lower() for line in lines if line.strip() and not line.startswith("#"))


def check_policy(password: str, login: str, role: Role) -> None:
    """Rules for a password a person chooses (generated ones always pass). Raises PasswordPolicyError."""
    if len(password) < MIN_LENGTH[role]:
        raise PasswordPolicyError(f"the password must be at least {MIN_LENGTH[role]} characters long")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"the password must be at most {MAX_LENGTH} characters long")
    if not password.strip():
        raise PasswordPolicyError("the password must not be blank")
    if password.strip().lower() == login.lower():
        raise PasswordPolicyError("the password must differ from the login")
    if password.strip().lower() in common_passwords():
        raise PasswordPolicyError("this password is too common")


@lru_cache(maxsize=1)
def wordlist() -> tuple[str, ...]:
    lines = (HERE / "words.txt").read_text(encoding="utf-8").splitlines()
    return tuple(sorted({line.strip() for line in lines if line.strip() and not line.startswith("#")}))


def generate_password() -> str:
    """Two short transliterated Russian words and a number, e.g. lisa-kot-472 (easy to type on a phone)."""
    words = wordlist()
    number = "".join(secrets.choice(DIGITS) for _ in range(3))
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{number}"


def generated_password_bits() -> float:
    return 2 * math.log2(len(wordlist())) + 3 * math.log2(len(DIGITS))
