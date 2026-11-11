"""List a user's resources at the testbed."""

from __future__ import annotations

import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel

import cloud_conversations.db.store as store
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.protocol import Kind, Resource, TestbedAdapter
from cloud_conversations.agent.inventory import (
    COMMON,
    Condition,
    HoldingsStep,
    Sort,
    field_types,
    parse_time,
)
from cloud_conversations.config import Config

_COMMON = {c.name for c in COMMON}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_OP_WORDS = {
    "is": "is", "in": "is", "contains": "contains", "before": "before", "after": "after",
    "lt": "under", "gt": "over",
}


class Row(Resource):
    made_by: Literal["experiment", "you", "other"] | None = None
    run_id: uuid.UUID | None = None
    thread_id: uuid.UUID | None = None
    title: str | None = None
    lease_ends: datetime | None = None
    released_at: datetime | None = None


class Holdings(BaseModel):
    summary: str
    kinds: list[Kind]
    rows: list[Row]
    left_out: list[str]
    # Each site that couldn't be read, with its error.
    unread: dict[str, str]


def read(
    adapter: TestbedAdapter,
    config: Config,
    step: HoldingsStep,
    *,
    owner_id: str,
    project_id: str | None,
    thread_id: uuid.UUID | None,
    testbed: str,
    now: datetime,
) -> Holdings:
    """now is in the user's time zone."""
    service = config.service
    sites, left_out = _sites(step.sites, adapter, config, testbed)
    kinds = [k for k in adapter.resource_kinds() if not step.kinds or k.name in step.kinds]

    listed, unread = _read_all(adapter, sites, kinds, service.holdings_read_s)
    read_sites = [s for s in sites if s not in unread]
    types = field_types(kinds)
    timed = any(types.get(c.field) == "time" for c in step.where)
    since = _EPOCH if timed else now - timedelta(days=service.holdings_released_days)
    rows = _joined(listed, store.owner_resources(owner_id, project_id, since), kinds)

    rows = [r for r in rows if all(_matches(r, c, now, thread_id) for c in step.where)]
    rows = _sorted(rows, step.sort) if step.sort else rows
    rows = rows[: step.limit] if step.limit else rows

    if len(rows) > service.holdings_max_rows:
        left_out.append(
            f"Showing {service.holdings_max_rows} of {len(rows)}. Ask for fewer to see the rest."
        )
    return Holdings(
        summary=_summary(rows, kinds, read_sites, step.where, now, types),
        kinds=kinds,
        rows=rows[: service.holdings_max_rows],
        left_out=left_out,
        unread=unread,
    )


def _sites(
    named: list[str], adapter: TestbedAdapter, config: Config, testbed: str
) -> tuple[list[str], list[str]]:
    """Returns the sites the names match, and a line for each name with no match."""
    configured = [s.site for s in adapter.capabilities().sites]
    named = [n for n in named if n.lower() != testbed.lower()]
    if not named:
        return configured, []
    retired = config.retired_at(testbed)
    sites: list[str] = []
    lines: list[str] = []
    for name in named:
        matched = [s for s in configured if name.lower() in s.lower()]
        if matched:
            sites += [s for s in matched if s not in sites]
        elif name in retired:
            lines.append(retired[name])
        else:
            lines.append(f"There's no site called {name}.")
    return sites, lines


def _read_all(
    adapter: TestbedAdapter, sites: list[str], kinds: list[Kind], timeout_s: float
) -> tuple[dict[tuple[str, str], list[Resource]], dict[str, str]]:
    """Returns each site's rows by kind, and each unread site's first error."""
    pool = ThreadPoolExecutor(max_workers=max(1, len(sites) * len(kinds)))
    futures: dict[tuple[str, str], Future[list[Resource]]] = {
        (site, kind.name): pool.submit(adapter.list_resources, site, kind.name)
        for site in sites
        for kind in kinds
    }
    wait(futures.values(), timeout=timeout_s)
    pool.shutdown(wait=False, cancel_futures=True)

    listed: dict[tuple[str, str], list[Resource]] = {}
    unread: dict[str, str] = {}
    for (site, kind), future in futures.items():
        if not future.done():
            unread.setdefault(site, f"No answer in {timeout_s:g} s.")
        elif isinstance(future.exception(), AdapterError):
            unread.setdefault(site, str(future.exception()))
        else:
            listed[(site, kind)] = future.result()
    return listed, unread


def _joined(
    listed: dict[tuple[str, str], list[Resource]],
    owned: list[store.OwnedResource],
    kinds: list[Kind],
) -> list[Row]:
    """Mark rows from the owner's runs, and add their released and gone resources."""
    run_kinds = {k.name: k.run_kind for k in kinds}
    rows: list[Row] = []
    for (site, kind), resources in listed.items():
        mine = [o for o in owned if (o.kind, o.site) == (run_kinds[kind], site)]
        runs = {o.provider_id: o for o in sorted(mine, key=lambda o: o.released_at is None)}
        for r in resources:
            run = runs.pop(r.resource_id, None)
            if run is not None and run.released_at is None:
                rows.append(_from_run(r, run))
            else:
                rows.append(Row(**r.model_dump(), made_by=_made_by(r.mine)))
        rows += [
            _from_run(
                Resource(
                    kind=kind, site=site, resource_id=o.provider_id, name=o.provider_id,
                    status="released" if o.released_at else "gone",
                    created=o.created_at, mine=True,
                ),
                o,
            )
            for o in runs.values()
        ]
    return sorted(rows, key=_order)


def _from_run(resource: Resource, run: store.OwnedResource) -> Row:
    return Row(
        **resource.model_dump(), made_by="experiment", run_id=run.run_id,
        thread_id=run.thread_id, title=run.title, lease_ends=run.lease_ends,
        released_at=run.released_at,
    )


def _made_by(mine: bool | None) -> Literal["you", "other"] | None:
    if mine is None:
        return None
    return "you" if mine else "other"


def _order(row: Row) -> tuple[bool, float]:
    """The owner's runs first, then newest first."""
    return row.made_by != "experiment", -(row.created.timestamp() if row.created else 0)


def _matches(row: Row, c: Condition, now: datetime, thread_id: uuid.UUID | None) -> bool:
    if c.field == "made_by":
        return any(_made(row, who, thread_id) for who in c.values())
    value = getattr(row, c.field) if c.field in _COMMON else row.fields.get(c.field)
    if value is None:
        return False
    if isinstance(value, datetime):
        bound = parse_time(str(c.value), now)
        return value < bound if c.op == "before" else value > bound
    if isinstance(c.value, int | float):
        return float(value) < c.value if c.op == "lt" else float(value) > c.value
    text = str(value).lower()
    if c.op == "contains":
        return str(c.value).lower() in text
    return text in [w.lower() for w in c.values()]


def _made(row: Row, who: str, thread_id: uuid.UUID | None) -> bool:
    if who == "you":
        return row.made_by in ("you", "experiment")
    if who == "others":
        return row.made_by == "other"
    return row.made_by == "experiment" and (who != "this_chat" or row.thread_id == thread_id)


def _sorted(rows: list[Row], sort: Sort) -> list[Row]:
    """Rows without the field go last."""

    def value(row: Row) -> Any:
        return getattr(row, sort.field) if sort.field in _COMMON else row.fields.get(sort.field)

    having = sorted(
        (r for r in rows if value(r) is not None), key=value, reverse=sort.order == "desc"
    )
    return having + [r for r in rows if value(r) is None]


def _said(c: Condition, now: datetime, types: dict[str, str]) -> str:
    """A condition in words, such as "created after Nov 05 09:00"."""
    if types.get(c.field) == "time":
        words = parse_time(str(c.value), now).strftime("%b %d %H:%M")
    else:
        words = " or ".join(v.replace("_", " ") for v in c.values())
    if c.field == "made_by":
        return f"made by {words}"
    return f"{c.field.replace('_', ' ')} {_OP_WORDS[c.op]} {words}"


def _summary(
    rows: list[Row],
    kinds: list[Kind],
    sites: list[str],
    where: list[Condition],
    now: datetime,
    types: dict[str, str],
) -> str:
    """A listing in words, such as "No active instances at 4 sites"."""
    if not sites:
        return "Nothing to list."
    states = [c for c in where if c.field == "status" and c.op != "contains"]
    state = " ".join(" or ".join(c.values()).lower() for c in states)
    said = [_said(c, now, types) for c in where if c not in states]
    counts = [(sum(r.kind == k.name for r in rows), k.label) for k in kinds]
    listed = [_counted(n, state, label) for n, label in counts if n]
    at = sites[0] if len(sites) == 1 else f"{len(sites)} sites"
    limits = f" where {' and '.join(said)}" if said else ""
    if not listed:
        noun = kinds[0].label.lower() if len(kinds) == 1 else "resources"
        return f"{' '.join(filter(None, ['No', state, noun]))} at {at}{limits}."
    return f"{', '.join(listed)} at {at}{limits}."


def _counted(count: int, state: str, label: str) -> str:
    noun = label[0].lower() + label[1:]
    return " ".join(filter(None, [str(count), state, noun[:-1] if count == 1 else noun]))
