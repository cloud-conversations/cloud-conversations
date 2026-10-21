"""The system key in the database."""

from __future__ import annotations

import cloud_conversations.db.store as store
from cloud_conversations.adapter import system_key


def test_the_first_use_creates_the_key_and_later_uses_reuse_it():
    first = system_key.current()

    assert system_key.current().fingerprint == first.fingerprint
    assert [k.fingerprint for k in store.system_keys()] == [first.fingerprint]


def test_a_second_key_added_at_once_yields_to_the_first():
    store.add_system_key("SHA256:first", "first-private")

    current = store.add_system_key("SHA256:second", "second-private")

    assert current.fingerprint == "SHA256:first"


def test_rotation_keeps_the_retired_key_for_the_instances_that_trust_it():
    old = system_key.current()

    new = system_key.rotate()

    assert system_key.current().fingerprint == new.fingerprint != old.fingerprint
    assert system_key.by_fingerprint(old.fingerprint).fingerprint == old.fingerprint
    assert store.system_key(old.fingerprint).retired_at is not None


def test_the_private_half_is_stored_encrypted():
    key = system_key.current()

    stored = store.system_key(key.fingerprint).private_key

    assert "PRIVATE KEY" not in stored
    assert "PRIVATE KEY" in system_key.private_half(key.fingerprint)
