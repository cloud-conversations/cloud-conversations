"""What a user has at the testbed."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter

from cloud_conversations import bundle
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.protocol import Capabilities, TestbedAdapter
from cloud_conversations.agent import turns
from cloud_conversations.agent.contract import DeclineStep
from cloud_conversations.llm.client import Model

# What can be listed.
Holding = Literal["leases", "instances", "images"]


class HoldingsStep(BaseModel):
    """The listings one message asks for. A site of None means every site."""

    model_config = {"extra": "forbid"}

    action: Literal["holdings"]
    holdings: list[Holding] = Field(min_length=1)
    site: str | None = None


class Holdings(BaseModel):
    """One table per listing, and a line counting their rows."""

    summary: str
    tables: list[str]


Listing = Annotated[HoldingsStep | DeclineStep, Field(discriminator="action")]

_STEP: TypeAdapter[Listing] = TypeAdapter(Listing)


def read_holdings(
    client: Model,
    adapter: TestbedAdapter,
    capabilities: Capabilities,
    message: str,
    *,
    testbed: str,
) -> tuple[Holdings | DeclineStep | None, list[turns.Round]]:
    """Read what the message asks for. Returns the tables, a refusal, or None."""
    messages = [
        {"role": "system", "content": _system_prompt(testbed, capabilities)},
        {"role": "user", "content": f"The message:\n\n{message}"},
    ]

    def act(step: HoldingsStep | DeclineStep) -> turns.Turn[Holdings | DeclineStep]:
        if isinstance(step, DeclineStep):
            return turns.Turn("decline", [], answer=step)
        reasons = _unknown_site(step, capabilities)
        if reasons:
            return turns.Turn("holdings", reasons)
        sites = [step.site] if step.site else [s.site for s in capabilities.sites]
        read = [_table(holding, adapter, sites) for holding in step.holdings]
        counted = ", ".join(
            _counted(holding, total)
            for holding, (_, total) in zip(step.holdings, read, strict=True)
        )
        where = sites[0] if len(sites) == 1 else f"{len(sites)} sites"
        return turns.Turn("holdings", [], answer=Holdings(
            summary=f"{counted} at {where}.",
            tables=[table for table, _ in read],
        ))

    return turns.converse(client, messages, _STEP, 2, act, turns.Round)


def _unknown_site(step: HoldingsStep, capabilities: Capabilities) -> list[str]:
    """Reasons the named site has no credentials."""
    known = [s.site for s in capabilities.sites]
    if step.site is None or step.site in known:
        return []
    return [
        f"there are no credentials for {step.site!r}; there are credentials for "
        f"{', '.join(known) or 'none'}"
    ]


def _table(
    holding: Holding, adapter: TestbedAdapter, sites: list[str]
) -> tuple[str, int]:
    """One listing at each site, and how many rows were read."""
    lines = [f"# {holding}"]
    total = 0
    for site in sites:
        lines.append(f"\n## {site}\n")
        try:
            rows = _rows(holding, adapter, site)
        except AdapterError as exc:
            lines.append(f"could not be read: {exc}")
            continue
        total += len(rows)
        lines += rows or ["nothing"]
    return "\n".join(lines), total


def _rows(holding: Holding, adapter: TestbedAdapter, site: str) -> list[str]:
    if holding == "leases":
        return [
            f"{x.name}  {x.lease_id}  {x.state}  {x.count}x  "
            f"{x.start.isoformat()} to {x.end.isoformat()}"
            for x in adapter.list_leases(site)
        ]
    if holding == "instances":
        return [
            f"{x.name}  {x.instance_id}  {x.state}"
            + (f"  fault={x.fault}" if x.fault else "")
            for x in adapter.list_instances(site)
        ]
    return [f"{x.name}  {x.image_id}" for x in adapter.list_own_images(site)]


def _counted(holding: Holding, total: int) -> str:
    if total == 0:
        return f"no {holding}"
    return f"{total} {holding[:-1] if total == 1 else holding}"


def _system_prompt(testbed: str, capabilities: Capabilities) -> str:
    named = ", ".join(s.site for s in capabilities.sites) or "none"
    instructions = bundle.default().prompt(testbed, "inventory", "instructions")
    return f"{instructions}\nThe sites the user holds credentials for: {named}\n"
