"""Recover a failed run with the experiment agent."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.agent.build import Build, build
from cloud_conversations.agent.catalog import Catalog
from cloud_conversations.agent.revision import revise
from cloud_conversations.config import Config, Settings
from cloud_conversations.knowledge.retrieve import KnowledgeBase
from cloud_conversations.llm.client import Model


def recover(
    client: Model,
    owner_id: str,
    run_id: uuid.UUID,
    adapter: TestbedAdapter,
    config: Config,
    settings: Settings,
    knowledge: KnowledgeBase | None = None,
    report: Callable[[Build], None] | None = None,
) -> uuid.UUID:
    """Drive a failed run until it passes or stops.

    Returns the newest run of the build.
    """
    catalog: Catalog | None = None
    while True:
        outcome = build(client, owner_id, run_id, adapter, settings, knowledge)
        run_id = outcome.run_id
        if report is not None:
            report(outcome)
        if outcome.revision is None:
            return run_id
        revised = revise(
            client, owner_id, run_id, outcome.revision, adapter, config, settings,
            catalog, knowledge,
        )
        run_id, catalog = revised.run_id, revised.catalog
        if revised.awaiting is not None:
            return run_id
