"""Exception hierarchy for testbed adapter errors."""

from __future__ import annotations


class AdapterError(Exception):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PreconditionError(AdapterError):

    def __init__(self, rule_id: str) -> None:
        super().__init__(f"Precondition {rule_id!r} not met", code="precondition_failed")


class TransientError(AdapterError):
    """A provider failure that may succeed on retry."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="transient")


class CapacityError(AdapterError):
    """No hardware available now."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="capacity_exhausted")


class ProviderNotFoundError(AdapterError):

    def __init__(self, kind: str, id: str) -> None:
        super().__init__(f"{kind} {id!r} not found at provider", code="not_found")


class AdapterTimeoutError(AdapterError):
    """Timed out waiting. last_state is the provider state on the final poll."""

    def __init__(self, message: str, *, waited_s: float, last_state: str) -> None:
        super().__init__(message, code="timeout")
        self.waited_s = waited_s
        self.last_state = last_state


class FatalError(AdapterError): ...


class UnschedulableError(FatalError):
    """No host could take the instance."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="unschedulable")


class HostUnusableError(FatalError):
    """The reserved host could not deploy the instance."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="host_unusable")


class NoAddressError(CapacityError):
    """No floating IP is free to reserve."""
