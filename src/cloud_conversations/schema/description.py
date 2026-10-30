"""Pydantic models for the JSONB payloads stored in experiment_descriptions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import AfterValidator, BaseModel, Field, model_validator

from cloud_conversations.environment.items import SETUP_KEYS, SetupKey, parse
from cloud_conversations.validator.rubric import HostKind

RequirementKey = Literal[
    "node_type",  # bare-metal
    "flavor",  # KVM
    "count",
    "image_name",  # exact image name
    "os_release",
    "cpu_arch",
    "cpu_count",
    "mem_gb",
    "disk_gb",
    "kernel",
    "gpu_model",
    "gpu_count",
    SetupKey,
]

ReservationKind = Literal["physical_host", "flavor_instance"]

ComputeKey = Literal["node_type", "flavor"]

# A chat's keypair: an imported key's keypair name, or a keypair name at each site.
KeypairChoice = str | dict[str, str]


def keypair_at(choice: KeypairChoice, site: str) -> str | None:
    return choice if isinstance(choice, str) else choice.get(site)


# The reservation kind of each compute requirement.
RESERVATION_KINDS: dict[ComputeKey, ReservationKind] = {
    "node_type": "physical_host",
    "flavor": "flavor_instance",
}

# Where a value came from.
Source = Literal["stated", "inferred", "default", "asked"]

# One line of prose the model writes.
MAX_NOTE = 300

def _printable(text: str) -> str:
    return "".join(c for c in text if c.isprintable())


Note = Annotated[str, Field(max_length=MAX_NOTE), AfterValidator(_printable)]

PLANNER_KEYS: frozenset[str] = frozenset(
    {"node_type", "flavor", "count", "image_name", "os_release", *SETUP_KEYS}
)

# Check kinds that verify a requirement of the same name.
VERIFIABLE_KEYS: frozenset[str] = frozenset(get_args(HostKind)) & frozenset(
    get_args(RequirementKey)
)


class Requirement(BaseModel):
    key: RequirementKey
    value: Any
    source: Source
    rationale: Note | None = None

    @model_validator(mode="after")
    def _setup_value(self) -> Requirement:
        if self.key in SETUP_KEYS:
            try:
                self.value = parse(self.key, self.value)
            except ValueError as exc:
                raise ValueError(f"{self.key}: {exc}") from None
        return self


class ImageChoice(BaseModel):
    """The image chosen when the description was written. A run boots only this one."""

    image_id: str
    name: str
    supported: bool
    # Label of the testbed that marks images as supported.
    testbed: str


class Requirements(BaseModel):
    """The requirements payload stored as JSONB in experiment_descriptions."""

    site: str | None = None
    start_at: datetime | None = None  # None leases as soon as the calendar allows
    start_source: Source | None = None
    duration_hours: int = Field(ge=1)
    duration_source: Source | None = None
    items: list[Requirement] = []
    image: ImageChoice | None = None

    def value_of(self, key: str, default: Any = None) -> Any:
        return next((i.value for i in self.items if i.key == key), default)


def reservation_kind_of(items: list[Requirement]) -> ReservationKind | None:
    return next(
        (RESERVATION_KINDS[i.key] for i in items if i.key in RESERVATION_KINDS), None
    )
