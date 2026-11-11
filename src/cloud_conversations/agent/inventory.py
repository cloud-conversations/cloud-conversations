"""Turn a message into a listing request."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from cloud_conversations import bundle
from cloud_conversations.adapter.protocol import Column, Kind
from cloud_conversations.agent import turns
from cloud_conversations.agent.contract import DeclineStep
from cloud_conversations.llm.client import Model

# Fields every kind has.
COMMON = [
    Column(name="name", label="Name", type="text"),
    Column(name="status", label="Status", type="text"),
    Column(name="created", label="Created", type="time"),
    Column(name="lease_ends", label="Lease ends", type="time"),
]
MADE_BY = ["you", "your_experiments", "this_chat", "others"]

_OPS: dict[str, set[str]] = {
    "text": {"is", "in", "contains"},
    "time": {"before", "after"},
    "number": {"lt", "gt"},
    "made_by": {"in"},
}
_OFFSET = re.compile(r"^([+-]\d+)([hdw])$")
_UNITS = {"h": "hours", "d": "days", "w": "weeks"}


class Condition(BaseModel):
    model_config = {"extra": "forbid"}

    field: str
    op: Literal["is", "in", "contains", "before", "after", "lt", "gt"]
    value: str | float | list[str]

    def values(self) -> list[str]:
        return self.value if isinstance(self.value, list) else [str(self.value)]


class Sort(BaseModel):
    model_config = {"extra": "forbid"}

    field: str
    order: Literal["asc", "desc"] = "desc"


class HoldingsStep(BaseModel):
    """A request to list resources."""

    model_config = {"extra": "forbid"}

    action: Literal["holdings"]
    kinds: list[str] = []
    sites: list[str] = []
    where: list[Condition] = []
    sort: Sort | None = None
    limit: int | None = Field(default=None, ge=1)


Listing = Annotated[HoldingsStep | DeclineStep, Field(discriminator="action")]

_STEP: TypeAdapter[Listing] = TypeAdapter(Listing)


def parse_time(value: str, now: datetime) -> datetime:
    """A time with no zone is in now's zone. Raises ValueError if value is not a time."""
    if value == "now":
        return now
    if value == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = _OFFSET.match(value)
    if offset:
        return now + timedelta(**{_UNITS[offset.group(2)]: int(offset.group(1))})
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=now.tzinfo)


def field_types(kinds: list[Kind]) -> dict[str, str]:
    """The fields all of these kinds share, with their types."""
    own = [{c.name: c.type for c in k.columns} for k in kinds]
    shared = {n: t for n, t in own[0].items() if all(n in o for o in own)} if own else {}
    return {c.name: c.type for c in COMMON} | {"made_by": "made_by"} | shared


def read_request(
    client: Model, kinds: list[Kind], message: str, *, testbed: str, now: datetime
) -> tuple[HoldingsStep | DeclineStep | None, list[turns.Round]]:
    """Ask the model what to list. Returns the request, a refusal, or None."""
    messages = [
        {"role": "system", "content": _system_prompt(testbed, kinds, now)},
        {"role": "user", "content": f"The message:\n\n{message}"},
    ]
    known = [k.name for k in kinds]

    def act(step: HoldingsStep | DeclineStep) -> turns.Turn[HoldingsStep | DeclineStep]:
        if isinstance(step, DeclineStep):
            return turns.Turn("decline", [], answer=step)
        unknown = [k for k in step.kinds if k not in known]
        if unknown:
            return turns.Turn("holdings", [
                f"there is no kind {k!r}; the kinds are {', '.join(known)}" for k in unknown
            ])
        named = [k for k in kinds if not step.kinds or k.name in step.kinds]
        reasons = _faults(step, field_types(named), now)
        return turns.Turn("holdings", reasons, answer=None if reasons else step)

    return turns.converse(client, messages, _STEP, 2, act, turns.Round)


def _faults(step: HoldingsStep, types: dict[str, str], now: datetime) -> list[str]:
    fields = ", ".join(types)
    reasons = []
    if step.sort is not None and step.sort.field not in types:
        reasons.append(f"cannot sort by {step.sort.field!r}; the fields are {fields}")
    for c in step.where:
        kind = types.get(c.field)
        if kind is None:
            reasons.append(f"{c.field!r} is not a field of these kinds; the fields are {fields}")
        elif c.op not in _OPS[kind]:
            ops = ", ".join(sorted(_OPS[kind]))
            reasons.append(f"{c.op!r} does not apply to {c.field!r}; use {ops}")
        elif (c.op == "in") != isinstance(c.value, list):
            reasons.append(f"{c.field!r} {c.op} takes {'a list' if c.op == 'in' else 'one value'}")
        elif kind == "made_by" and not set(c.values()) <= set(MADE_BY):
            reasons.append(f"made_by takes {', '.join(MADE_BY)}")
        elif kind == "number" and not isinstance(c.value, float | int):
            reasons.append(f"{c.field!r} {c.op} takes a number")
        elif kind == "text" and isinstance(c.value, float | int):
            reasons.append(f"{c.field!r} {c.op} takes text")
        elif kind == "time" and not _names_a_time(c.value, now):
            reasons.append(
                f"{c.value!r} is not a time; write an ISO time, now, today, "
                "or an offset like -7d or +24h"
            )
    return reasons


def _names_a_time(value: str | float | list[str], now: datetime) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_time(value, now)
    except (ValueError, OverflowError):
        return False
    return True


def _system_prompt(testbed: str, kinds: list[Kind], now: datetime) -> str:
    listed = "\n".join(
        f"  {k.name}: {', '.join(c.name for c in k.columns) or 'no fields of its own'}"
        for k in kinds
    )
    return bundle.default().prompt(
        testbed, "inventory", "instructions",
        kinds=listed,
        common=", ".join(c.name for c in COMMON),
        made_by=", ".join(MADE_BY),
        now=now.strftime("%A %Y-%m-%dT%H:%M %Z"),
    )
