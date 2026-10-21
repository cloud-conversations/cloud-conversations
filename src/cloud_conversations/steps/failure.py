"""What a provider failure means for the run."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

import cloud_conversations.db.store as store
from cloud_conversations.adapter.errors import (
    CapacityError,
    FatalError,
    HostUnusableError,
    NoAddressError,
    TransientError,
    UnschedulableError,
)

FailureKind = Literal["plan", "description", "step"]

# What a retry would re-do.
RetryScope = Literal["none", "step", "reservation", "address"]


@dataclass(frozen=True)
class Verdict:
    kind: FailureKind
    retry: RetryScope


_STEP = Verdict(kind="step", retry="step")
_TERMINAL_STEP = Verdict(kind="step", retry="none")
_CAPACITY = Verdict(kind="plan", retry="none")
_RESERVATION = Verdict(kind="plan", retry="reservation")


def classify(exc: Exception) -> Verdict:
    """The verdict on one provider failure."""
    if isinstance(exc, UnschedulableError):
        return _STEP
    if isinstance(exc, HostUnusableError):
        return _RESERVATION
    if isinstance(exc, FatalError):
        return _TERMINAL_STEP

    if isinstance(exc, CapacityError):
        return _CAPACITY
    if isinstance(exc, TransientError):
        return _STEP
    return _TERMINAL_STEP


def record(
    run_id: uuid.UUID,
    owner_id: str,
    step_id: str,
    action: str,
    kind: FailureKind,
    error: str,
    **extra: Any,
) -> None:
    """Append a step_failed event."""
    store.append_event(
        run_id,
        "runner",
        "step_failed",
        {"step_id": step_id, "action": action, "failure_kind": kind, "error": error, **extra},
        owner_id=owner_id,
    )


def out_of_addresses(exc: Exception) -> bool:
    return isinstance(exc, NoAddressError)


def unusable_host(exc: Exception) -> bool:
    """Whether the reserved host is the problem."""
    return isinstance(exc, (UnschedulableError, HostUnusableError))
