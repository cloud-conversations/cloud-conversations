"""cloud_conversations.adapter.credentials: the adapter registry."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter import credentials
from cloud_conversations.config import load_config
from tests.conftest import PUBLISHED_CONFIG, TODAY


def test_a_testbed_with_no_adapter_package_is_refused():
    with pytest.raises(ValueError, match="testbed 'elsewhere' has no adapter"):
        credentials.public_adapter(PUBLISHED_CONFIG, "elsewhere")


def test_a_retired_site_is_named_retired_rather_than_unknown(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        '[adapters.chameleon.kinds.vm]\nreservation_kind = "flavor_instance"\n'
        '[adapters.chameleon.sites."KVM@TACC"]\nkinds = ["vm"]\n'
        '[adapters.chameleon.sites."KVM@TACC".retirement]\n'
        f"retires_on = {TODAY}\nwarn_from = {TODAY}\n"
        'notice = "KVM@TACC retires today."\nretired_notice = "KVM@TACC has retired."\n'
    )

    with pytest.raises(ValueError) as raised:
        credentials.build_adapter("KVM@TACC", load_config(path))
    assert str(raised.value) == "KVM@TACC has retired."
