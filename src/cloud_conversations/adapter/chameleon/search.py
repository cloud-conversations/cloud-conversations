"""Availability read from the Chameleon reference API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from cloud_conversations.adapter.chameleon.catalog import CatalogError, is_hypervisor
from cloud_conversations.adapter.protocol import Flavor
from cloud_conversations.schema.search import Candidate, Candidates, NodeQuery

_BYTES_PER_GB = 1_073_741_824

# The largest limit /nodes/search accepts, and its default.
_PAGE = 500


def search_hardware(
    root: str,
    query: NodeQuery,
    uids: dict[str, str],
    flavors: dict[str, list[Flavor]],
    timeout_s: float,
) -> Candidates:
    """A describe agent search. flavors maps each flavor site to its published flavors."""
    if query.reserve_by == "flavor":
        offered = {
            site: (uids.get(site), listed)
            for site, listed in sorted(flavors.items())
            if query.site in (None, site)
        }
        return search_flavors(root, query, offered, timeout_s)
    return search_nodes(root, query, uids, timeout_s)


def search_nodes(
    root: str,
    query: NodeQuery,
    uid_for: dict[str, str],
    timeout_s: float,
) -> Candidates:
    """Free bare-metal nodes matching a query, grouped by site and node type."""
    if query.reserve_by != "node_type":
        raise CatalogError("a flavor search goes to search_flavors")

    start = query.start_at or datetime.now(timezone.utc)
    end = start + timedelta(hours=query.duration_hours)

    params: dict[str, str] = {}
    if query.site is not None:
        if query.site not in uid_for:
            raise CatalogError(f"no reference-api site is named {query.site!r}")
        params["site_id"] = uid_for[query.site]
    if query.node_type is not None:
        params["node_type"] = query.node_type
    if query.arch is not None:
        params["arch"] = query.arch
    if query.gpu is not None:
        params["gpu"] = "true" if query.gpu else "false"
    if query.infiniband is not None:
        params["infiniband"] = "true" if query.infiniband else "false"
    if query.min_ram_gb is not None:
        params["min_ram"] = str(query.min_ram_gb * _BYTES_PER_GB)
    params["start"] = start.isoformat()
    params["end"] = end.isoformat()

    nodes = _search_pages(f"{root}/nodes/search", params, timeout_s)

    name_for = {uid: name for name, uid in uid_for.items()}
    free: Counter[tuple[str, str]] = Counter()
    for node in nodes:
        if node.get("availability") != "available" or is_hypervisor(node):
            continue
        site = name_for.get(str(node.get("site_id")))
        node_type = node.get("node_type")
        if not (site and node_type):
            continue
        free[(site, node_type)] += 1

    return Candidates(
        query=query,
        start=start,
        end=end,
        items=[
            Candidate(site=site, compute=node_type, free=count)
            for (site, node_type), count in sorted(free.items())
        ],
    )


def search_flavors(
    root: str,
    query: NodeQuery,
    offered: dict[str, tuple[str | None, list[Flavor]]],
    timeout_s: float,
) -> Candidates:
    """Free instances of every published flavor a query matches.

    offered maps each site to its reference-api uid and its flavors.
    """
    start = query.start_at or datetime.now(timezone.utc)
    end = start + timedelta(hours=query.duration_hours)

    items: list[Candidate] = []
    for site, (uid, flavors) in offered.items():
        if uid is None:
            raise CatalogError(f"{site} publishes no flavor availability")
        items += [
            Candidate(
                site=site,
                compute=flavor.name,
                free=flavor_availability(root, uid, flavor.flavor_id, start, end, timeout_s),
            )
            for flavor in _matching(query, flavors)
        ]
    return Candidates(query=query, start=start, end=end, items=items)


def _matching(query: NodeQuery, flavors: list[Flavor]) -> list[Flavor]:
    """Flavors matching the query's name or filters."""
    if query.flavor is not None:
        return [f for f in flavors if f.name == query.flavor]
    return [
        flavor
        for flavor in flavors
        if (query.gpu is None or flavor.gpu == query.gpu)
        and (query.min_ram_gb is None or flavor.mem_gb >= query.min_ram_gb)
    ]


def flavor_availability(
    root: str,
    uid: str,
    flavor_id: str,
    start: datetime,
    end: datetime,
    timeout_s: float,
) -> int:
    """How many instances of a flavor are free across the whole window."""
    path = f"{root}/sites/{_quote(uid)}/flavors/{_quote(flavor_id)}/availability"
    payload = _get(path, {"start_date": start.isoformat(), "end_date": end.isoformat()},
                   timeout_s)
    segments = payload.get("availability")
    if not isinstance(segments, list):
        raise CatalogError(f"{path}: expected an availability timeline")
    return min((int(s.get("available") or 0) for s in segments), default=0)


def _search_pages(url: str, params: dict[str, str], timeout_s: float) -> list[dict[str, Any]]:
    """Read every page of a node search."""
    nodes: list[dict[str, Any]] = []
    while True:
        payload = _get(url, {**params, "offset": str(len(nodes)), "limit": str(_PAGE)},
                       timeout_s)
        items = payload.get("items")
        total = payload.get("total")
        if not isinstance(items, list) or not isinstance(total, int):
            raise CatalogError(f"{url}: expected a page of items")
        nodes.extend(items)
        if not items or len(nodes) >= total:
            return nodes


def _get(url: str, params: dict[str, str], timeout_s: float) -> dict[str, Any]:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full, timeout=timeout_s) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{full}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CatalogError(f"{full}: expected an object")
    return payload


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="")
