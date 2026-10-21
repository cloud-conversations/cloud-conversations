"""Bounded retries, and the event they record."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

import cloud_conversations.db.store as store
from cloud_conversations.steps.context import StepContext
from cloud_conversations.steps.failure import RetryScope, classify

T = TypeVar("T")


def record(
    run_id: uuid.UUID,
    owner_id: str,
    step_id: str,
    action: str,
    scope: RetryScope,
    attempt: int,
    of: int,
    error: str,
    **extra: Any,
) -> None:
    """Append a step_retried event."""
    store.append_event(
        run_id,
        "runner",
        "step_retried",
        {
            "step_id": step_id,
            "action": action,
            "scope": scope,
            "attempt": attempt,
            "of": of,
            "error": error,
            **extra,
        },
        owner_id=owner_id,
    )


def attempt(
    ctx: StepContext,
    step_id: str,
    action: str,
    attempts: int,
    delay_s: float,
    fn: Callable[[int], T],
) -> T:
    """Run `fn`, retrying while the verdict allows it.

    `fn` takes the attempt number and releases whatever it created before raising.
    The last failure is re-raised unchanged.
    """
    for number in range(1, attempts + 1):
        try:
            return fn(number)
        except Exception as exc:
            if number == attempts or classify(exc).retry != "step":
                raise
            record(
                ctx.run_id, ctx.owner_id, step_id, action, "step",
                number, attempts, str(exc),
            )
            store.touch_heartbeat(ctx.run_id)
            time.sleep(delay_s * number)

    raise AssertionError("unreachable: the loop returns or raises")
