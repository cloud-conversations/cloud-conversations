"""Revise the description of a failed run."""

from __future__ import annotations

import uuid
from typing import NamedTuple

import cloud_conversations.db.store as store
from cloud_conversations import releases
from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.agent.build import record_refusal, record_revision, situation
from cloud_conversations.agent.catalog import Catalog, get_catalog
from cloud_conversations.agent.contract import DeclineStep, ProposedDescription, ReviseStep
from cloud_conversations.agent.describe import Round, propose, store_proposal
from cloud_conversations.config import Config, ConfigError, Settings, testbed_of
from cloud_conversations.db.store import Description, Run
from cloud_conversations.knowledge.retrieve import KnowledgeBase
from cloud_conversations.llm.client import Model
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import Requirements, reservation_kind_of


class Revised(NamedTuple):
    run_id: uuid.UUID
    catalog: Catalog | None
    # The new version, if the user must approve it.
    awaiting: int | None = None


def revise(
    client: Model,
    owner_id: str,
    run_id: uuid.UUID,
    step: ReviseStep,
    adapter: TestbedAdapter,
    config: Config,
    settings: Settings,
    catalog: Catalog | None = None,
    knowledge: KnowledgeBase | None = None,
) -> Revised:
    """Write the next version of a failed run's description."""
    change = step.change
    run = store.get_run(owner_id, run_id)
    desc = store.get_description(owner_id, run.description_id, run.description_version)
    holding = bool(store.live_resources(run_id))
    if not holding:
        sequential.abandon(run_id)

    if catalog is None:
        try:
            catalog = get_catalog(adapter, config, desc.requirements.site)
        except (ValueError, ConfigError) as exc:
            record_refusal(
                owner_id, run_id, f"no catalog for {desc.requirements.site}: {exc}"
            )
            return Revised(run_id, None)

    outcome, rounds = propose(
        client, catalog, change, settings, prior=desc, knowledge=knowledge,
        testbed=testbed_of(owner_id), adapter=adapter,
    )
    if not isinstance(outcome, ProposedDescription):
        store.create_conversation(owner_id, change, [t.model_dump(mode="json") for t in rounds])
        record_refusal(owner_id, run_id, _why(outcome, rounds))
        return Revised(run_id, catalog)

    stored = store_proposal(owner_id, change, rounds, outcome, catalog, desc)
    version = stored.version
    changed = _changes(desc.requirements, stored.requirements)
    if holding:
        releases.offer(
            owner_id, run, step.reason,
            {**situation(run, "revise"), "change": change, "changes": changed}, version,
        )
        record_refusal(
            owner_id, run_id,
            f"version {version} replaces this run, so it waits for the user to release it",
        )
        return Revised(run_id, catalog, version)
    if changed:
        _ask_approval(owner_id, desc, version, changed, stored.conversation_id)
        record_refusal(
            owner_id, run_id,
            f"version {version} changes {changed}, so it waits for the user to approve it",
        )
        return Revised(run_id, catalog, version)

    keypair = keypair_for(owner_id, run, outcome.site)
    if keypair is None:
        record_refusal(owner_id, run_id, f"no keypair is chosen at {outcome.site}")
        return Revised(run_id, catalog)
    new_run = build_replacement(owner_id, run, version, change, keypair, adapter, config)
    return Revised(new_run, catalog)


def build_replacement(
    owner_id: str,
    old: Run,
    version: int,
    change: str,
    keypair: str,
    adapter: TestbedAdapter,
    config: Config,
) -> uuid.UUID:
    """Build a version of the old run's description in its place."""
    requirements = store.get_description(owner_id, old.description_id, version).requirements
    site = requirements.site
    if not site:
        raise ValueError(f"description {old.description_id} v{version} names no site")
    new_run = sequential.provision(
        owner_id=owner_id,
        description_id=old.description_id,
        version=version,
        project_id=old.project_id,
        keypair=keypair,
        adapter=adapter,
        site=site,
        settings=config.for_site(site, reservation_kind_of(requirements.items)),
    )
    record_revision(owner_id, new_run, old.run_id, old.description_version, version, change)
    return new_run


def keypair_for(owner_id: str, run: Run, site: str) -> str | None:
    """The failed run's keypair, or the chat's at site if the run never planned."""
    if run.plan is not None:
        return run.plan.keypair()
    chat = store.thread_for_description(owner_id, run.description_id)
    return chat.keypair_at(site) if chat is not None else None


def _changes(approved: Requirements, revised: Requirements) -> str:
    """The parts of the build that changed, or an empty string."""
    changed = [
        name
        for name, before, after in (
            ("the site", approved.site, revised.site),
            ("the node type", approved.value_of("node_type"), revised.value_of("node_type")),
            ("the flavor", approved.value_of("flavor"), revised.value_of("flavor")),
            ("the lease length", approved.duration_hours, revised.duration_hours),
            ("the lease start", approved.start_at, revised.start_at),
            ("the image name", approved.value_of("image_name"), revised.value_of("image_name")),
            ("the OS release", approved.value_of("os_release"), revised.value_of("os_release")),
        )
        if before != after
    ]
    if approved.image is not None and (
        revised.image is None or revised.image.image_id != approved.image.image_id
    ):
        changed.append("the image")
    return ", ".join(changed)


def _ask_approval(
    owner_id: str,
    desc: Description,
    version: int,
    changed: str,
    conversation_id: uuid.UUID,
) -> None:
    """Post the version to its chat for approval."""
    chat = store.thread_for_description(owner_id, desc.description_id)
    if chat is None:
        return
    store.append_message(
        chat.thread_id, "assistant", "description",
        f"Version {version} changes {changed} from version {desc.version}. "
        "Approve it to build it.",
        {},
        owner_id=owner_id,
        conversation_id=conversation_id,
        description_id=desc.description_id,
        description_version=version,
    )


def _why(outcome: DeclineStep | None, rounds: list[Round]) -> str:
    """Why the experiment description agent produced no new version."""
    if isinstance(outcome, DeclineStep):
        suggestions = "; ".join(outcome.suggestions)
        return (
            f"the experiment description agent declined that change: {outcome.reason}"
            + (f" It suggests: {suggestions}" if suggestions else "")
        )
    last = rounds[-1].reasons if rounds else []
    return (
        "the experiment description agent could not write that revision in "
        f"{len(rounds)} round(s)"
        + (f"; it was last refused because: {'; '.join(last)}" if last else "")
    )
