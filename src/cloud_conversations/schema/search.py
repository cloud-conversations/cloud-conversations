"""A hardware search the describe agent asks for, and the free hardware it found."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from cloud_conversations.schema.description import ComputeKey


class NodeQuery(BaseModel):
    model_config = {"extra": "forbid"}

    reserve_by: ComputeKey
    site: str | None = None  # region name, or None for every site
    node_type: str | None = None  # bare metal
    flavor: str | None = None  # a VM
    arch: str | None = None
    gpu: bool | None = None
    infiniband: bool | None = None
    min_ram_gb: int | None = None
    start_at: datetime | None = None  # None means as soon as possible
    duration_hours: int
    intent: str  # the user's request, restated


class Candidate(BaseModel):
    site: str  # region name, e.g. CHI@TACC
    compute: str  # a node type or a flavor
    free: int


class Candidates(BaseModel):
    query: NodeQuery
    start: datetime
    end: datetime
    items: list[Candidate]
