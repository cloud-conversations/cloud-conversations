"""The audit trail of changes to the testbed."""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, cast

import psycopg

import cloud_conversations.db.store as store
from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.db.store import CallOutcome, Via

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Acting:
    owner_id: str
    request_id: uuid.UUID
    run_id: uuid.UUID | None = None
    reason: str | None = None


_ACTING: ContextVar[Acting | None] = ContextVar("acting", default=None)


def approved(
    owner_id: str,
    via: Via,
    *,
    description_id: uuid.UUID,
    version: int,
    thread_id: uuid.UUID | None = None,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    """Record a build the user approved."""
    request_id = uuid.uuid4()
    store.record_decision(
        request_id, owner_id, "confirmed", "user", "build", via=via, thread_id=thread_id,
        description_id=description_id, version=version, conn=conn,
    )
    return request_id


def resumed(owner_id: str, run_id: uuid.UUID) -> uuid.UUID:
    """Record a run the user resumed from the CLI."""
    request_id = uuid.uuid4()
    store.record_decision(
        request_id, owner_id, "confirmed", "user", "resume", via="cli", run_id=run_id
    )
    return request_id


@contextmanager
def acting(owner_id: str, request_id: uuid.UUID) -> Iterator[None]:
    """Attribute the testbed calls inside to owner_id and request_id."""
    token = _ACTING.set(Acting(owner_id, request_id))
    try:
        yield
    finally:
        _ACTING.reset(token)


def on_run(run_id: uuid.UUID) -> AbstractContextManager[None]:
    """Attribute the testbed calls inside to run_id."""
    return _narrowed(run_id=run_id)


def because(reason: str) -> AbstractContextManager[None]:
    """Attach reason to the testbed calls inside."""
    return _narrowed(reason=reason)


@contextmanager
def _narrowed(**changes: Any) -> Iterator[None]:
    current = _ACTING.get()
    token = _ACTING.set(replace(current, **changes)) if current is not None else None
    try:
        yield
    finally:
        if token is not None:
            _ACTING.reset(token)


# Each adapter call that changes the testbed, with its resource kind and ID.
CHANGES: dict[str, tuple[str, Callable[[dict[str, Any], Any], str | None]]] = {
    "create_lease": ("lease", lambda a, r: r.lease_id if r is not None else None),
    "launch_instance": ("instance", lambda a, r: r.instance_id if r is not None else None),
    "ensure_security_group": ("security_group", lambda a, r: r),
    "allocate_floating_ip": ("floating_ip", lambda a, r: r),
    "attach_floating_ip": ("floating_ip", lambda a, r: a["address"]),
    "import_keypair": ("keypair", lambda a, r: a["name"]),
    "ssh_exec": ("instance", lambda a, r: a["host"]),
    "delete_lease": ("lease", lambda a, r: a["lease_id"]),
    "delete_instance": ("instance", lambda a, r: a["instance_id"]),
    "delete_security_group": ("security_group", lambda a, r: a["group_id"]),
    "delete_floating_ip": ("floating_ip", lambda a, r: a["address"]),
    "delete_keypair": ("keypair", lambda a, r: a["name"]),
}


class _Audited:
    """An adapter that records its testbed changes in audit.testbed_calls."""

    def __init__(self, inner: TestbedAdapter) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        call = getattr(self._inner, name)
        if name not in CHANGES:
            return call
        kind, ident = CHANGES[name]
        signature = inspect.signature(getattr(TestbedAdapter, name))

        def recorded(*args: Any, **kwargs: Any) -> Any:
            """Raises RuntimeError if no one is named as acting."""
            who = _ACTING.get()
            if who is None:
                raise RuntimeError(f"{name} changes the testbed, and no one is named as acting")
            arguments = dict(signature.bind(None, *args, **kwargs).arguments)
            del arguments["self"]
            site = arguments.pop("site")
            call_id = uuid.uuid4()
            _record(who, call_id, name, kind, site, ident(arguments, None), arguments, "started")
            try:
                result = call(*args, **kwargs)
            except Exception as exc:
                _settled(
                    who, call_id, name, kind, site, ident(arguments, None), arguments,
                    "failed", f"{type(exc).__name__}: {exc}",
                )
                raise
            _settled(who, call_id, name, kind, site, ident(arguments, result), arguments, "ok")
            return result

        return recorded


def _record(
    who: Acting,
    call_id: uuid.UUID,
    operation: str,
    kind: str,
    site: str,
    provider_id: str | None,
    arguments: dict[str, Any],
    outcome: CallOutcome,
    error: str | None = None,
) -> None:
    store.record_testbed_call(
        call_id, who.owner_id, site, operation, kind, provider_id, outcome, error,
        who.request_id, who.run_id or arguments.get("run_id"), who.reason,
        json.loads(json.dumps(arguments, default=str)),
    )


def _settled(
    who: Acting,
    call_id: uuid.UUID,
    operation: str,
    kind: str,
    site: str,
    provider_id: str | None,
    arguments: dict[str, Any],
    outcome: CallOutcome,
    error: str | None = None,
) -> None:
    """Record a call's outcome.

    Logs a failed write and returns.
    """
    try:
        _record(who, call_id, operation, kind, site, provider_id, arguments, outcome, error)
    except Exception:
        log.exception("could not record the outcome of %s at %s", operation, site)


def audited(adapter: TestbedAdapter) -> TestbedAdapter:
    return cast(TestbedAdapter, _Audited(adapter))
