"""Requests to release a run, and the releases they allow."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from typing import Any, Literal

import psycopg

import cloud_conversations.db.store as store
from cloud_conversations import audit
from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.db.store import (
    ConflictError,
    Decision,
    DecisionAction,
    DecisionActor,
    Run,
    Via,
)
from cloud_conversations.runner import sequential
from cloud_conversations.runner.sequential import ReleaseOutcome
from cloud_conversations.schema.runs import RunState


def held(run_id: uuid.UUID) -> list[dict[str, str]]:
    return [
        {"kind": r.kind, "provider_id": r.provider_id, "site": r.site}
        for r in store.live_resources(run_id)
    ]


def named(resources: list[dict[str, str]]) -> str:
    return ", ".join(f"{r['kind']} {r['provider_id']} at {r['site']}" for r in resources)


def propose(
    owner_id: str,
    actor: DecisionActor,
    action: DecisionAction,
    reason: str,
    resources: list[dict[str, str]],
    *,
    via: Via | None = None,
    run_id: uuid.UUID | None = None,
    thread_id: uuid.UUID | None = None,
    description_id: uuid.UUID | None = None,
    version: int | None = None,
    shown: str | None = None,
    detail: dict[str, Any] | None = None,
    conn: psycopg.Connection | None = None,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    store.record_decision(
        request_id, owner_id, "proposed", actor, action,
        via=via, run_id=run_id, thread_id=thread_id, description_id=description_id,
        version=version, resources=resources, shown=shown, reason=reason, detail=detail,
        conn=conn,
    )
    return request_id


def offer(
    owner_id: str, run: Run, reason: str, detail: dict[str, Any], version: int | None
) -> uuid.UUID:
    """Record the agent's proposal to release the run, and post it to the run's chat."""
    resources = held(run.run_id)
    then = ""
    if version is not None:
        then = f", then build version {version}"
        if detail.get("changes"):
            then += f", which changes {detail['changes']}"
    shown = f"The experiment agent wants to release {named(resources)}{then}. It says: {reason}"
    chat = store.thread_for_description(owner_id, run.description_id)
    request_id = propose(
        owner_id, "agent", "release" if version is None else "release_and_build", reason,
        resources, run_id=run.run_id, thread_id=chat.thread_id if chat else None,
        description_id=run.description_id, version=version, shown=shown, detail=detail,
    )
    if chat is not None:
        store.append_message(
            chat.thread_id, "assistant", "release", shown,
            {"request_id": str(request_id), "version": version},
            owner_id=owner_id,
            description_id=run.description_id,
            description_version=version or run.description_version,
        )
    return request_id


def confirm(
    owner_id: str, request_id: uuid.UUID, via: Via, conn: psycopg.Connection | None = None
) -> None:
    """Record the user's yes.

    Raises ConflictError if the request is answered or out of date, or the run holds
    something it does not name.
    """
    _answer(owner_id, request_id, "confirmed", via, conn)


def decline(owner_id: str, request_id: uuid.UUID, via: Via) -> None:
    """Record the user's no.

    Raises ConflictError if the request is answered.
    """
    _answer(owner_id, request_id, "declined", via, None)


def _answer(
    owner_id: str,
    request_id: uuid.UUID,
    step: Literal["confirmed", "declined"],
    via: Via,
    conn: psycopg.Connection | None,
) -> None:
    with store.transaction() if conn is None else nullcontext(conn) as c:
        proposal, *answers = store.decision_request(owner_id, request_id, c)
        if answers:
            raise ConflictError(f"release {request_id} is already answered")
        if step == "confirmed":
            _current(proposal)
            _covers(proposal)
        store.record_decision(
            request_id, owner_id, step, "user", proposal.action,
            via=via, run_id=proposal.run_id, thread_id=proposal.thread_id,
            description_id=proposal.description_id, version=proposal.version,
            resources=proposal.resources, conn=c,
        )


def _current(proposal: Decision) -> None:
    """Raises ConflictError if the run is no longer failed, or the agent has proposed since."""
    if proposal.actor != "agent" or proposal.run_id is None:
        return
    run = store.get_run(proposal.owner_id, proposal.run_id)
    newest = store.pending_release(proposal.owner_id, proposal.run_id)
    if run.state != RunState.failed or newest is None or newest.request_id != proposal.request_id:
        raise ConflictError(
            "the run has changed since the experiment agent asked. "
            "Review the run and release it from its card"
        )


def _covers(proposal: Decision) -> None:
    """Raises ConflictError if the run holds a resource the proposal does not name."""
    if proposal.run_id is None:
        return
    proposed = {(r["kind"], r["provider_id"]) for r in proposal.resources}
    extra = [r for r in held(proposal.run_id) if (r["kind"], r["provider_id"]) not in proposed]
    if extra:
        raise ConflictError(
            f"the run also holds {named(extra)}. Review the run and release it again"
        )


def carry_out(
    owner_id: str, request_id: uuid.UUID, adapter: TestbedAdapter
) -> list[ReleaseOutcome]:
    """Release the run the user confirmed releasing.

    Raises ConflictError if the user has not confirmed it, or the run holds more than
    they confirmed.
    """
    proposal, *answers = store.decision_request(owner_id, request_id)
    if not any(a.step == "confirmed" for a in answers):
        raise ConflictError(f"release {request_id} is not confirmed")
    if proposal.run_id is None:
        raise ValueError(f"release {request_id} names no run")
    _covers(proposal)
    with audit.acting(owner_id, request_id):
        return sequential.teardown(proposal.run_id, adapter)
