"""ChameleonAdapter's mapping of Nova and Blazar wording onto typed errors."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter.chameleon.adapter import instance_error
from cloud_conversations.adapter.errors import (
    FatalError,
    HostUnusableError,
    UnschedulableError,
)

# Nova and Blazar error messages.
NO_VALID_HOST = "No valid host was found. There are not enough hosts available."
EXHAUSTED = (
    "Instance abc encountered a failure. Exhausted all hosts available for "
    "retrying build failures for instance abc."
)
NOT_DEPLOYABLE = "Failed to provision instance abc: Failed to prepare to deploy."


def test_no_valid_host_is_unschedulable():
    assert isinstance(instance_error(NO_VALID_HOST), UnschedulableError)


@pytest.mark.parametrize("message", [EXHAUSTED, NOT_DEPLOYABLE])
def test_a_host_that_will_not_deploy_is_unusable(message):
    assert isinstance(instance_error(message), HostUnusableError)


def test_other_wording_is_a_plain_fatal_error():
    error = instance_error("Volume attach failed")

    assert type(error) is FatalError
