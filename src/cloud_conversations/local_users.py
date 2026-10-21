"""Local test users (password only)."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

import cloud_conversations.db.store as store
from cloud_conversations.config import account_id

PREFIX = "local:"

_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_MIN_PASSWORD = 12
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def user_id(testbed: str, name: str) -> str:
    """Raises ValueError for a name that is not 1 to 64 letters, digits, '_', '.' or '-'."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"{name!r} is not 1 to 64 letters, digits, '_', '.' or '-'")
    return account_id(testbed, PREFIX + name)


def add(testbed: str, name: str, password: str) -> None:
    """Raises store.ConflictError if the name is taken."""
    store.add_local_user(user_id(testbed, name), _hash(password))


def set_password(testbed: str, name: str, password: str) -> None:
    store.set_local_password(user_id(testbed, name), _hash(password))


def delete(testbed: str, name: str) -> None:
    store.delete_local_user(user_id(testbed, name))


def is_local(user_id: str) -> bool:
    return user_id.partition(":")[2].startswith(PREFIX)


def check(testbed: str, name: str, password: str) -> str | None:
    """The user_id of the local user with this name and password, or None."""
    try:
        uid = user_id(testbed, name)
    except ValueError:
        return None
    stored = store.local_password_hash(uid)
    return uid if stored and _matches(stored, password) else None


def _hash(password: str) -> str:
    """Raises ValueError for a password shorter than 12 characters."""
    if len(password) < _MIN_PASSWORD:
        raise ValueError(f"a password must be at least {_MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def _matches(stored: str, password: str) -> bool:
    _, n, r, p, salt, digest = stored.split("$")
    tried = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
    )
    return hmac.compare_digest(tried.hex(), digest)
