"""Sites and node types read from the Chameleon reference API."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, NamedTuple

from cloud_conversations.adapter.errors import TransientError
from cloud_conversations.adapter.protocol import Flavor, NodeType


class CatalogError(TransientError):
    """The reference API was unreachable or returned something unusable."""


class PublishedSite(NamedTuple):
    uid: str
    auth_url: str
    node_catalog_url: str


def fetch_sites(index_url: str, timeout_s: float) -> dict[str, PublishedSite]:
    """Return each site keyed by its Keystone region name."""
    items = _get(index_url, timeout_s)
    root = index_url.rsplit("/sites", 1)[0]
    sites = {}
    for item in items:
        name, uid, web = item.get("name"), item.get("uid"), item.get("web")
        if not (name and uid and web):
            continue
        sites[name] = PublishedSite(
            uid=uid,
            auth_url=f"{web}:5000/v3",
            node_catalog_url=f"{root}/sites/{uid}/clusters/chameleon/nodes",
        )
    return sites


def fetch_node_types(url: str, timeout_s: float) -> list[NodeType]:
    """Return one NodeType per distinct node type, sorted by name."""
    items = _get(url, timeout_s)
    by_name: dict[str, NodeType] = {}
    for node in items:
        name = node.get("node_type")
        if not name or name in by_name or is_hypervisor(node):
            continue
        by_name[name] = to_node_type(name, node)
    return [by_name[k] for k in sorted(by_name)]


def fetch_flavors(url: str, timeout_s: float) -> list[Flavor]:
    """Return one Flavor per published VM size, sorted by name."""
    return sorted(
        (_to_flavor(item) for item in _get(url, timeout_s) if item.get("name")),
        key=lambda f: f.name,
    )


def _to_flavor(item: dict[str, Any]) -> Flavor:
    gpu = item.get("gpu") or {}
    has_gpu = bool(gpu.get("gpu"))
    return Flavor(
        flavor_id=item.get("uid") or item["name"],
        name=item["name"],
        vcpus=item.get("vcpus") or 0,
        mem_gb=int((item.get("ram_size") or 0) / 1_073_741_824),
        disk_gb=int((item.get("disk_size") or 0) / 1_073_741_824),
        gpu=has_gpu,
        gpu_count=(gpu.get("gpu_count") or 1) if has_gpu else 0,
        gpu_allocation=gpu.get("gpu_allocation") if has_gpu else None,
    )


def is_hypervisor(node: dict[str, Any]) -> bool:
    return node.get("lease_mode") == "flavor"


def _get(url: str, timeout_s: float) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise CatalogError(f"{url}: {exc}") from exc
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise CatalogError(f"{url}: expected a list")
    return items


def to_node_type(name: str, node: dict[str, Any]) -> NodeType:
    arch = node.get("architecture") or {}
    memory = node.get("main_memory") or {}
    gpu = node.get("gpu") or {}
    sizes = [d.get("size") or 0 for d in (node.get("storage_devices") or [])]
    return NodeType(
        name=name,
        cpu_arch=arch.get("platform_type") or "x86_64",
        # smt_size is logical cores.
        cpu_count=arch.get("smt_size") or 0,
        mem_gb=int((memory.get("ram_size") or 0) / 1_073_741_824),
        disk_gb=int(max(sizes, default=0) / 1_000_000_000),
        gpu_model=gpu.get("gpu_model") if gpu.get("gpu") else None,
    )
