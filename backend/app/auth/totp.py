"""TOTP (RFC 6238) for admins: secrets encrypted with Fernet, each code accepted once.

The key is JUDGE_SECRET_KEY (a Fernet key: 32 random bytes, urlsafe base64), generated with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Losing the key makes every enrolled secret unreadable: admins then need `judgectl admin reset-2fa`.
"""
from __future__ import annotations

import hmac
from datetime import datetime
from typing import Optional

import pyotp
from cryptography.fernet import Fernet, InvalidToken

STEP_S = 30
DIGITS = 6
DRIFT_STEPS = 1  # accept the previous and the next 30 s window: phone clocks drift


class TotpConfigError(RuntimeError):
    """JUDGE_SECRET_KEY is missing or not a Fernet key, or a stored secret cannot be decrypted with it."""


class SecretBox:
    def __init__(self, key: Optional[str]):
        if not key:
            raise TotpConfigError("JUDGE_SECRET_KEY is not set: 2FA cannot work without it (see docs/AUTH.md)")
        try:
            self._fernet = Fernet(key.encode() if isinstance(key, str) else key)
        except (ValueError, TypeError) as exc:
            raise TotpConfigError("JUDGE_SECRET_KEY is not a valid Fernet key (see docs/AUTH.md)") from exc

    def encrypt(self, secret: str) -> str:
        return self._fernet.encrypt(secret.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise TotpConfigError("a TOTP secret cannot be decrypted: JUDGE_SECRET_KEY changed?") from exc


def new_secret() -> str:
    return pyotp.random_base32()  # 160 bits


def provisioning_uri(secret: str, login: str, issuer: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_S).provisioning_uri(name=login, issuer_name=issuer)


def step_at(now: datetime) -> int:
    return int(now.timestamp()) // STEP_S


def code_at_step(secret: str, step: int) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_S).at(step * STEP_S)


def match_code(secret: str, code: str, now: datetime, last_step: Optional[int]) -> Optional[int]:
    """The step of the accepted code, or None. Steps at or before `last_step` are refused (no replay)."""
    code = "".join(code.split())
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = step_at(now)
    for step in range(current - DRIFT_STEPS, current + DRIFT_STEPS + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_at_step(secret, step), code):
            return step
    return None
