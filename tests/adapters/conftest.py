"""Fixtures the adapter tests share."""

from __future__ import annotations

import uuid

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.schema.description import Requirement, Requirements
from cloud_conversations.validator.rubric import Check


@pytest.fixture()
def run_id(owner_a) -> uuid.UUID:
    reqs = Requirements(
        duration_hours=2,
        items=[Requirement(key="node_type", value="compute_skylake", source="stated")],
    )
    rubric = [Check(id="a", kind="cpu_arch", expected="x86_64", comparator="eq")]
    did, ver = store.create_description(owner_a, "conv-adapter", reqs, rubric)
    return store.create_run(owner_a, did, ver, None)
