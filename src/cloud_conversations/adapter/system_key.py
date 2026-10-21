"""The system key: the service's own SSH key, trusted by every instance it boots."""

from __future__ import annotations

import io

import paramiko

import cloud_conversations.db.store as store
from cloud_conversations import crypt

# The system key's label in an instance's authorized_keys.
LABEL = "cloud-conversations-system"

_BITS = 3072


def current() -> paramiko.PKey:
    """Return the current key, creating it on first use."""
    key = store.current_system_key() or store.add_system_key(*_generate())
    return load(key.private_key)


def by_fingerprint(fingerprint: str) -> paramiko.PKey:
    """A current or retired key. Raises store.NotFoundError."""
    return load(store.system_key(fingerprint).private_key)


def rotate() -> paramiko.PKey:
    """Retire the current key and create its replacement."""
    return load(store.rotate_system_key(*_generate()).private_key)


def private_half(fingerprint: str) -> str:
    """A current or retired key's private half, decrypted. Raises store.NotFoundError."""
    return crypt.decrypt(store.system_key(fingerprint).private_key)


def authorized_key(key: paramiko.PKey) -> str:
    """The key's line in authorized_keys."""
    return f"{key.get_name()} {key.get_base64()} {LABEL}"


def _generate() -> tuple[str, str]:
    """A new key's fingerprint and encrypted private half."""
    key = paramiko.RSAKey.generate(_BITS)
    pem = io.StringIO()
    key.write_private_key(pem)
    return key.fingerprint, crypt.encrypt(pem.getvalue())


def load(stored: str) -> paramiko.PKey:
    """A stored, encrypted private half as a key."""
    return paramiko.RSAKey.from_private_key(io.StringIO(crypt.decrypt(stored)))
