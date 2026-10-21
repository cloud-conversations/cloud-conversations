"""Imported public keys and the keypair name each is given."""

from __future__ import annotations

import re

import paramiko
import pytest

from cloud_conversations.adapter import user_key


@pytest.fixture()
def public_key() -> str:
    return f"ssh-rsa {paramiko.RSAKey.generate(2048).get_base64()} me@laptop"


def test_an_imported_key_gets_a_prefixed_keypair_name_with_a_random_suffix(owner_a, public_key):
    key = user_key.import_key(owner_a, "laptop", public_key + "\n")

    assert re.fullmatch(r"cc-laptop-[a-z0-9]{6}", key.keypair_name)
    assert key.public_key == public_key


def test_importing_a_name_again_replaces_the_key_and_keeps_the_keypair_name(owner_a, public_key):
    first = user_key.import_key(owner_a, "laptop", public_key)
    replacement = f"ssh-rsa {paramiko.RSAKey.generate(2048).get_base64()} me@laptop"

    second = user_key.import_key(owner_a, "laptop", replacement)

    assert second.keypair_name == first.keypair_name
    assert second.public_key == replacement


def test_a_name_nova_would_refuse_is_rejected(owner_a, public_key):
    with pytest.raises(ValueError) as raised:
        user_key.import_key(owner_a, "my.laptop", public_key)
    assert str(raised.value) == (
        "key name 'my.laptop' must be 1 to 32 letters, digits, '_' or '-'"
    )


def test_a_private_key_is_not_a_public_key(owner_a):
    with pytest.raises(ValueError) as raised:
        user_key.import_key(owner_a, "laptop", "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbg==\n")
    assert str(raised.value) == "not an OpenSSH public key"
