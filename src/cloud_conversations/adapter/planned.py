"""Notes of resources about to be created at the provider."""

from __future__ import annotations

import uuid

import cloud_conversations.db.store as store
from cloud_conversations.adapter.protocol import TestbedAdapter


def made_earlier(
    adapter: TestbedAdapter, site: str, kind: str, run_id: uuid.UUID, idempotency_key: str
) -> str | None:
    """Note a resource about to be made.

    Returns its id if an earlier attempt made it.
    """
    if store.plan_resource(run_id, kind, site, idempotency_key):
        return None
    return adapter.find_planned(site, kind, run_id, idempotency_key)


def record_planned(run_id: uuid.UUID, adapter: TestbedAdapter) -> None:
    """Record the run's planned resources that exist at the provider."""
    for planned in store.planned_resources(run_id):
        found = adapter.find_planned(planned.site, planned.kind, run_id, planned.idempotency_key)
        if found is not None:
            store.create_resource_with_event(
                run_id, found, planned.kind, planned.site, None,
                idempotency_key=planned.idempotency_key,
            )
