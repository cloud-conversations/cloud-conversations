"""Actor and event-kind vocabularies for run_events, enforced by store.append_event."""

from __future__ import annotations

from typing import Literal

Actor = Literal[
    "runner",
    "adapter",
    "validator",
    "agent",
    "tool",
]

EventKind = Literal[
    "run_started",
    "plan_proposed",
    "step_started",
    "step_succeeded",
    "step_failed",
    "tool_called",
    "model_turn",
    "resource_created",
    "resource_released",
    "step_retried",
    "check_observed",
    "validation_completed",
    "connection_ready",
    "run_terminal",
    "description_revision_requested",
]
