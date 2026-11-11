"""Read a Chameleon project's resources at a site."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import chi.image
import chi.lease
import chi.network
import chi.server
import keystoneauth1.adapter

from cloud_conversations.adapter.protocol import Column, Kind, Resource

KINDS = [
    Kind(
        name="leases", label="Leases", run_kind="lease", holds_hardware=True,
        columns=[
            Column(name="reserved", label="Reserved", type="text"),
            Column(name="starts", label="Starts", type="time"),
            Column(name="ends", label="Ends", type="time"),
        ],
    ),
    Kind(
        name="instances", label="Instances", run_kind="instance", holds_hardware=True,
        columns=[
            Column(name="addresses", label="Addresses", type="text"),
            Column(name="key_pair", label="Key pair", type="text"),
            Column(name="fault", label="Fault", type="text"),
        ],
    ),
    Kind(
        name="floating_ips", label="Floating IPs", run_kind="floating_ip",
        columns=[Column(name="attached_to", label="Attached to", type="text")],
    ),
    Kind(
        name="security_groups", label="Security groups", run_kind="security_group",
        columns=[Column(name="description", label="Description", type="text")],
    ),
    Kind(
        name="images", label="Images",
        columns=[
            Column(name="size_gb", label="Size (GB)", type="number"),
            Column(name="visibility", label="Visibility", type="text"),
        ],
    ),
    Kind(name="key_pairs", label="Key pairs", columns=[]),
    Kind(name="networks", label="Networks", columns=[]),
    Kind(name="routers", label="Routers", columns=[]),
]


def read(kind: str, session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    return _READERS[kind](session, site)


def fault(server: Any) -> str | None:
    raw = getattr(server, "fault", None) or {}
    return " ".join(
        str(raw[k]) for k in ("code", "message", "details") if raw.get(k)
    ).strip() or None


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _reserved(lease: dict[str, Any]) -> str | None:
    """What a lease reserves, such as "2 × compute_zen3"."""
    for r in lease.get("reservations", []):
        if r.get("resource_type") == "physical:host":
            props = json.loads(r.get("resource_properties") or "[]")
            node_type = props[2] if props[:2] == ["==", "$node_type"] else "hosts"
            return f"{r.get('min', 1)} × {node_type}"
        if r.get("resource_type") == "flavor:instance":
            return f"{r.get('amount', 1)} × VM"
    return None


def _leases(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    user_id = session.get_user_id()
    return [
        Resource(
            kind="leases", site=site, resource_id=x["id"], name=x.get("name") or "",
            status=x.get("status", ""), created=_time(x.get("created_at")),
            mine=x["user_id"] == user_id if x.get("user_id") else None,
            fields={
                "reserved": _reserved(x),
                "starts": _time(x.get("start_date")),
                "ends": _time(x.get("end_date")),
            },
        )
        for x in chi.lease.blazar(session).lease.list()
    ]


def _instances(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    user_id = session.get_user_id()
    return [
        Resource(
            kind="instances", site=site, resource_id=s.id, name=s.name, status=s.status,
            created=_time(getattr(s, "created", None)),
            mine=s.user_id == user_id if getattr(s, "user_id", None) else None,
            fields={
                "addresses": ", ".join(
                    a["addr"] for net in (getattr(s, "addresses", None) or {}).values()
                    for a in net
                ) or None,
                "key_pair": getattr(s, "key_name", None),
                "fault": fault(s),
            },
        )
        for s in chi.server.nova(session).servers.list()
    ]


def _floating_ips(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    return [
        Resource(
            kind="floating_ips", site=site, resource_id=f["floating_ip_address"],
            name=f["floating_ip_address"], status=f.get("status", ""),
            created=_time(f.get("created_at")), mine=None,
            fields={"attached_to": f.get("fixed_ip_address")},
        )
        for f in chi.network.neutron(session).list_floatingips()["floatingips"]
    ]


def _security_groups(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    return [
        Resource(
            kind="security_groups", site=site, resource_id=g["id"], name=g["name"],
            status="", created=_time(g.get("created_at")), mine=None,
            fields={"description": g.get("description") or None},
        )
        for g in chi.network.neutron(session).list_security_groups()["security_groups"]
    ]


def _images(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    owner = session.get_project_id()
    return [
        Resource(
            kind="images", site=site, resource_id=i.id, name=i.name or "",
            status=getattr(i, "status", ""), created=_time(i.created_at), mine=None,
            fields={
                "size_gb": round(i.size / 1e9, 1) if getattr(i, "size", None) else None,
                "visibility": getattr(i, "visibility", None),
            },
        )
        for i in chi.image.glance(session).images.list(filters={"owner": owner})
    ]


def _key_pairs(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    return [
        Resource(
            kind="key_pairs", site=site, resource_id=k.name, name=k.name,
            status="", created=None, mine=True,
        )
        for k in chi.server.nova(session).keypairs.list()
    ]


def _networks(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    neutron = chi.network.neutron(session)
    listed = neutron.list_networks(project_id=session.get_project_id())["networks"]
    return [_network_row("networks", site, n) for n in listed]


def _routers(session: keystoneauth1.adapter.Adapter, site: str) -> list[Resource]:
    neutron = chi.network.neutron(session)
    listed = neutron.list_routers(project_id=session.get_project_id())["routers"]
    return [_network_row("routers", site, r) for r in listed]


def _network_row(kind: str, site: str, raw: dict[str, Any]) -> Resource:
    return Resource(
        kind=kind, site=site, resource_id=raw["id"], name=raw.get("name") or "",
        status=raw.get("status", ""), created=_time(raw.get("created_at")), mine=None,
    )


_READERS: dict[str, Callable[[keystoneauth1.adapter.Adapter, str], list[Resource]]] = {
    "leases": _leases,
    "instances": _instances,
    "floating_ips": _floating_ips,
    "security_groups": _security_groups,
    "images": _images,
    "key_pairs": _key_pairs,
    "networks": _networks,
    "routers": _routers,
}
