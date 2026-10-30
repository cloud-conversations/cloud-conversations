from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cloud_conversations.config import Settings
from cloud_conversations.schema.description import Requirements

if TYPE_CHECKING:
    from cloud_conversations.adapter.protocol import TestbedAdapter


@dataclass
class StepContext:
    run_id: uuid.UUID
    owner_id: str
    site: str
    adapter: TestbedAdapter
    requirements: Requirements
    settings: Settings
    outputs: dict[str, Any] = field(default_factory=dict)
    reservation_attempt: int = 1
    address_attempt: int = 1


class StepError(Exception):
    """Raised by a step after writing step_failed, carrying a typed failure_kind."""

    def __init__(
        self,
        cause: Exception,
        failure_kind: str,
        *,
        retry_reservation: bool = False,
        retry_address: bool = False,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.failure_kind = failure_kind
        # Set to retry on a new reservation or address.
        self.retry_reservation = retry_reservation
        self.retry_address = retry_address


# When the job carrying the steps must stop, in time.monotonic() seconds.
_FINISH_BY: ContextVar[float | None] = ContextVar("finish_by", default=None)


@contextmanager
def finishing_by(deadline: float) -> Iterator[None]:
    """Make the steps inside stop waiting by deadline, in time.monotonic() seconds."""
    token = _FINISH_BY.set(deadline)
    try:
        yield
    finally:
        _FINISH_BY.reset(token)


def finish_by() -> float | None:
    return _FINISH_BY.get()
