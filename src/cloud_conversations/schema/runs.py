"""Pydantic models for the runs JSONB columns, plus the legal state-transition table."""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class RunState(str, enum.Enum):
    planning = "planning"
    provisioning = "provisioning"
    configuring = "configuring"
    validating = "validating"
    passed = "passed"
    failed = "failed"
    abandoned = "abandoned"


# States from which no further work may be started.
SETTLED_STATES: frozenset[RunState] = frozenset({RunState.passed, RunState.abandoned})

_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.planning: frozenset({
        RunState.provisioning,
        RunState.failed,
        RunState.abandoned,
    }),
    RunState.provisioning: frozenset({
        RunState.configuring,
        RunState.failed,
        RunState.abandoned,
    }),
    RunState.configuring: frozenset({
        RunState.provisioning,
        RunState.validating,
        RunState.failed,
        RunState.abandoned,
    }),
    RunState.validating: frozenset({
        RunState.provisioning,
        RunState.passed,
        RunState.failed,
        RunState.abandoned,
    }),
    RunState.passed: frozenset(),
    RunState.failed: frozenset({RunState.provisioning, RunState.abandoned}),
    RunState.abandoned: frozenset(),
}


class IllegalTransitionError(Exception):
    """Raised when a requested state transition is not in the legal table."""

    def __init__(self, from_state: RunState, to_state: RunState) -> None:
        super().__init__(
            f"Cannot transition run from {from_state.value!r} to {to_state.value!r}"
        )
        self.from_state = from_state
        self.to_state = to_state


def check_transition(from_state: RunState, to_state: RunState) -> None:
    """Raise IllegalTransitionError if the transition is not permitted."""
    if to_state not in _TRANSITIONS.get(from_state, frozenset()):
        raise IllegalTransitionError(from_state, to_state)


class CheckResult(BaseModel):
    """Result of one rubric check, stored in the runs.results JSONB array."""

    check_id: str
    kind: str
    expected: Any
    actual: Any
    passed: bool


class Connection(BaseModel):
    """SSH endpoint once the host is ready, stored in runs.connection."""

    host: str
    user: str
    port: int
    expires_at: str | None = None  # when the underlying lease ends


class Failure(BaseModel):
    """Terminal failure details, stored in runs.failure."""

    kind: Literal["step", "plan", "description"]
    message: str
    step_id: str | None = None
    action: str | None = None


class PlanStep(BaseModel):
    step_id: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """Provisioning plan stored in runs.plan."""

    plan_id: str
    site: str
    steps: list[PlanStep]

    def keypair(self) -> str:
        return next(s.args["ssh_key_ref"] for s in self.steps if s.action == "ensure_keypair")
