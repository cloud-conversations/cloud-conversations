"""cloud_conversations.steps.failure: what each adapter error means for the run."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter.errors import (
    AdapterTimeoutError,
    CapacityError,
    FatalError,
    HostUnusableError,
    TransientError,
    UnschedulableError,
)
from cloud_conversations.steps.failure import classify


def test_an_unplaceable_boot_is_worth_running_again():
    verdict = classify(UnschedulableError("no host"))

    assert verdict.kind == "step"
    assert verdict.retry == "step"


def test_a_host_that_will_not_deploy_needs_a_different_reservation():
    verdict = classify(HostUnusableError("will not deploy"))

    assert verdict.kind == "plan"
    assert verdict.retry == "reservation"


def test_any_other_fatal_error_is_not_retried():
    verdict = classify(FatalError("Instance went to ERROR", code="fatal"))

    assert verdict.kind == "step"
    assert verdict.retry == "none"


def test_no_capacity_is_a_plan_failure_and_retrying_will_not_help():
    verdict = classify(CapacityError("No availability for 1xcompute_zen3"))

    assert verdict.kind == "plan"
    assert verdict.retry == "none"


def test_a_transient_error_is_worth_running_again():
    verdict = classify(TransientError("connection reset"))

    assert verdict.retry == "step"


@pytest.mark.parametrize(
    "exc",
    [
        AdapterTimeoutError("timed out", waited_s=1.0, last_state="BUILD"),
        ValueError("something else entirely"),
    ],
)
def test_everything_else_is_a_terminal_step_failure(exc):
    verdict = classify(exc)

    assert verdict.kind == "step"
    assert verdict.retry == "none"
