"""The experiment agent decides what to do after a run fails."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, TypeAdapter, ValidationError

import cloud_conversations.db.store as store
from cloud_conversations import audit, bundle, releases
from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.agent import observe, turns
from cloud_conversations.agent.contract import (
    AbandonStep,
    BuildStep,
    InspectStep,
    KnowledgeStep,
    ReviseStep,
    loads,
)
from cloud_conversations.agent.validate import validate_build_step
from cloud_conversations.config import Settings, testbed_of
from cloud_conversations.db.store import Event, Run
from cloud_conversations.knowledge.retrieve import KnowledgeBase
from cloud_conversations.llm.client import LLMError, Model
from cloud_conversations.runner import sequential
from cloud_conversations.schema.runs import RunState

_STEP: TypeAdapter[BuildStep] = TypeAdapter(BuildStep)


class Build(BaseModel):
    """The outcome of one call to `build`."""

    run_id: uuid.UUID
    rounds: list[turns.Round]
    # Set when the agent asked for a revision.
    revision: ReviseStep | None = None


def build(
    client: Model,
    owner_id: str,
    run_id: uuid.UUID,
    adapter: TestbedAdapter,
    settings: Settings,
    knowledge: KnowledgeBase | None = None,
) -> Build:
    """Drive a failed run until it passes or stops."""
    rounds: list[turns.Round] = []

    while True:
        run = store.get_run(owner_id, run_id)
        if run.state == RunState.passed:
            return Build(run_id=run_id, rounds=rounds)

        events = [
            e for rid in chain(owner_id, run_id) for e in store.read_events(owner_id, rid)
        ]
        rounds_left = settings.max_build_rounds - _count(events, "model_turn")
        leases_left = settings.max_leases_per_build - _leases(events)
        if rounds_left <= 0:
            return Build(run_id=run_id, rounds=rounds)

        desc = store.get_description(
            owner_id, run.description_id, run.description_version
        )
        number = settings.max_build_rounds - rounds_left + 1

        sent = [
            {
                "role": "system",
                "content": _system_prompt(testbed_of(owner_id), settings, knowledge),
            },
            {
                "role": "user",
                "content": observe.render(
                    desc, run, events, store.live_resources(run_id),
                    rounds_left, leases_left, settings.max_events_rendered,
                ),
            },
        ]
        try:
            reply = client.complete(sent)
        except LLMError as exc:
            rounds.append(turns.Round.failed(number, exc, sent))
            return Build(run_id=run_id, rounds=rounds)

        try:
            step = _STEP.validate_python(loads(reply.text))
        except (ValueError, ValidationError) as exc:
            rounds.append(turns.Round.of(number, "unparsed", reply, [str(exc)], sent))
            _record(owner_id, run_id, rounds[-1])
            continue

        reasons = validate_build_step(
            step, run, _inspected(events), leases_left, knowledge
        )
        rounds.append(turns.Round.of(number, step.action, reply, reasons, sent))
        _record(owner_id, run_id, rounds[-1])
        if reasons:
            continue

        if isinstance(step, KnowledgeStep):
            if knowledge is not None:
                store.append_event(
                    run_id, "tool", "tool_called",
                    {
                        "read": "search_knowledge_base",
                        "question": step.question,
                        "result": knowledge.search(step.question),
                    },
                    owner_id=owner_id,
                )
            continue

        if isinstance(step, AbandonStep):
            if store.live_resources(run_id):
                releases.offer(owner_id, run, step.reason, situation(run, "abandon"), None)
            else:
                sequential.abandon(run_id)
            return Build(run_id=run_id, rounds=rounds)

        if isinstance(step, ReviseStep):
            return Build(run_id=run_id, rounds=rounds, revision=step)

        if isinstance(step, InspectStep):
            with audit.on_run(run_id):
                result = observe.read(
                    step.read, adapter, run, desc, store.live_resources(run_id), settings
                )
            store.append_event(
                run_id, "tool", "tool_called",
                {"read": step.read, "result": result}, owner_id=owner_id,
            )
            continue

        sequential.resume(run_id, adapter, settings)


def situation(run: Run, action: str) -> dict:
    """The failed run's state and failure."""
    return {
        "action": action,
        "run_state": run.state.value,
        "failure": run.failure.model_dump() if run.failure else None,
    }


def record_revision(
    owner_id: str,
    new_run: uuid.UUID,
    from_run: uuid.UUID,
    from_version: int,
    version: int,
    change: str,
) -> None:
    """Link a run to the one it replaces."""
    store.append_event(
        new_run, "agent", "description_revision_requested",
        {
            "from_run_id": str(from_run),
            "from_version": from_version,
            "version": version,
            "change": change,
        },
        owner_id=owner_id,
    )


def record_refusal(owner_id: str, run_id: uuid.UUID, reason: str) -> None:
    """Record a revision that produced nothing."""
    store.append_event(
        run_id, "agent", "tool_called",
        {"read": "revise", "result": reason}, owner_id=owner_id,
    )


def _inspected(events: list[Event]) -> list[str]:
    """The reads taken since the run last started."""
    since = max(
        (i for i, e in enumerate(events) if e.kind == "run_started"), default=0
    )
    return [
        e.payload.get("question") or e.payload["read"]
        for e in events[since:]
        if e.kind == "tool_called"
    ]


def _system_prompt(testbed: str, settings: Settings, knowledge: KnowledgeBase | None) -> str:
    prompts = bundle.default()
    policy = settings.agent_policy.strip()
    parts = [
        prompts.prompt(testbed, "build", "instructions"),
        prompts.prompt(testbed, "build", "knowledge") if knowledge is not None else "",
        f"Also:\n\n{policy}" if policy else "",
    ]
    return "\n".join(part for part in parts if part)


def chain(owner_id: str, run_id: uuid.UUID) -> list[uuid.UUID]:
    """Every run of this build, oldest first."""
    ids = [run_id]
    while True:
        origin = next(
            (
                e.payload["from_run_id"]
                for e in store.read_events(owner_id, ids[0])
                if e.kind == "description_revision_requested"
            ),
            None,
        )
        if origin is None:
            return ids
        ids.insert(0, uuid.UUID(origin))


def _count(events: list[Event], kind: str) -> int:
    return sum(1 for e in events if e.kind == kind)


def _leases(events: list[Event]) -> int:
    return sum(
        1
        for e in events
        if e.kind == "resource_created" and e.payload.get("kind") == "lease"
    )


def _record(owner_id: str, run_id: uuid.UUID, entry: turns.Round) -> None:
    store.append_event(
        run_id, "agent", "model_turn", entry.model_dump(exclude={"sent"}),
        owner_id=owner_id, model_input=entry.sent,
    )
