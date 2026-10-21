"""A user's public keys, and the keypair name for each."""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import string

import paramiko

import cloud_conversations.db.store as store
from cloud_conversations.config import RESOURCE_PREFIX

# A key's name in cloud conversations.
_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")

_SUFFIX_CHARS = string.ascii_lowercase + string.digits
_SUFFIX_LENGTH = 6


def import_key(owner_id: str, name: str, public_key: str) -> store.UserKey:
    """Store an OpenSSH public key under name. Raises ValueError for a bad name or key."""
    if not _NAME.fullmatch(name):
        raise ValueError(f"key name {name!r} must be 1 to 32 letters, digits, '_' or '-'")
    _parse(public_key)
    suffix = "".join(secrets.choice(_SUFFIX_CHARS) for _ in range(_SUFFIX_LENGTH))
    return store.import_user_key(
        owner_id, name, f"{RESOURCE_PREFIX}-{name}-{suffix}", public_key.strip()
    )


def fingerprint(public_key: str) -> str:
    return _parse(public_key).fingerprint


def _parse(public_key: str) -> paramiko.PKey:
    """Raises ValueError if public_key is not one OpenSSH public key."""
    parts = public_key.split()
    try:
        return paramiko.PKey.from_type_string(
            parts[0], base64.b64decode(parts[1], validate=True)
        )
    except (IndexError, binascii.Error, paramiko.UnknownKeyType, paramiko.SSHException) as exc:
        raise ValueError("not an OpenSSH public key") from exc
