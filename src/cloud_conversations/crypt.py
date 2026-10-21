"""Encryption at rest, under the key in CC_SECRET_KEY."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

SECRET_KEY_ENV = "CC_SECRET_KEY"


class CryptError(Exception):
    """CC_SECRET_KEY is unusable, or a value was not encrypted under it."""


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token: str, max_age_s: float | None = None) -> str:
    """Raises CryptError if token is older than max_age_s."""
    ttl = None if max_age_s is None else int(max_age_s)
    try:
        return _fernet().decrypt(token.encode(), ttl=ttl).decode()
    except InvalidToken as exc:
        raise CryptError(
            f"a value is expired, or was not encrypted under {SECRET_KEY_ENV}"
        ) from exc


def _fernet() -> Fernet:
    key = os.environ.get(SECRET_KEY_ENV)
    if not key:
        raise CryptError(f"{SECRET_KEY_ENV} is unset")
    try:
        return Fernet(key)
    except ValueError as exc:
        raise CryptError(f"{SECRET_KEY_ENV} is not a Fernet key: {exc}") from exc
