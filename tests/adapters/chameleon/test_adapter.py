"""ChameleonAdapter against stub Blazar, Nova, Neutron and Glance clients."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import keystoneauth1.plugin
import pytest
from keystoneauth1 import exceptions as ks

import cloud_conversations.db.store as store
from cloud_conversations.adapter import system_key
from cloud_conversations.adapter.chameleon.adapter import (
    BLAZAR_TIME_FORMAT,
    ChameleonAdapter,
    Login,
)
from cloud_conversations.adapter.errors import (
    AdapterTimeoutError,
    CapacityError,
    FatalError,
    NoAddressError,
    PreconditionError,
    ProviderNotFoundError,
    TransientError,
    UnschedulableError,
)
from cloud_conversations.schema.search import NodeQuery
from tests.conftest import FAST, PUBLISHED_CONFIG
from tests.loopback import node, page, serving


class _StubBlazarLease:
    def __init__(self, parent: _StubBlazar) -> None:
        self._p = parent

    def create(self, name, start, end, reservations, events, before_end=None):
        self._p.create_calls.append(
            {"name": name, "start": start, "end": end, "reservations": reservations}
        )
        return self._p.lease_json

    def get(self, lease_id, detail=False):
        if self._p.lease_json is None:
            raise RuntimeError("404 Not Found")
        return self._p.lease_json

    def delete(self, lease_id):
        if self._p.raise_on_delete is not None:
            raise self._p.raise_on_delete


class _StubBlazarHost:
    def __init__(self, parent: _StubBlazar) -> None:
        self._p = parent

    def list(self):
        if self._p.host_list_failures > 0:
            self._p.host_list_failures -= 1
            self._p.host_list_attempts += 1
            raise RuntimeError(
                "Request to https://chi.tacc.invalid:1234/v1/os-hosts timed out"
            )
        self._p.host_list_attempts += 1
        return self._p.hosts

    def list_allocations(self, sort_by=None):
        return self._p.allocations


class _StubBlazarFlavorInstance:
    def __init__(self, parent: _StubBlazar) -> None:
        self._p = parent

    def get_availability(self, flavor_id=None, start_date=None, end_date=None):
        self._p.availability_calls.append(
            {"flavor_id": flavor_id, "start_date": start_date, "end_date": end_date}
        )
        return self._p.flavor_availability


class _StubBlazar:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []
        self.availability_calls: list[dict[str, Any]] = []
        self.raise_on_delete: Exception | None = None
        self.lease_json: dict[str, Any] | None = None
        self.hosts: list[dict[str, Any]] = []
        self.host_list_failures = 0
        self.host_list_attempts = 0
        self.allocations: list[dict[str, Any]] = []
        self.flavor_availability: dict[str, Any] = {"flavor_instances": []}
        self.lease = _StubBlazarLease(self)
        self.host = _StubBlazarHost(self)
        self.flavor_instance = _StubBlazarFlavorInstance(self)


class _StubFlavor:
    def __init__(
        self, fid: str, name: str, keys: dict[str, str] | None = None, listed: bool = True
    ) -> None:
        self.id = fid
        self.name = name
        self.vcpus = 4
        self.ram = 8192
        self.disk = 40
        self.keys = keys or {}
        if listed:
            self.extra_specs = self.keys
        self.fetched = 0

    def get_keys(self) -> dict[str, str]:
        self.fetched += 1
        return self.keys


class _StubServer:
    def __init__(self, sid: str, name: str, status: str = "BUILD", fault=None) -> None:
        self.id = sid
        self.name = name
        self.status = status
        self.fault = fault
        self.metadata: dict[str, str] = {}


class _StubNovaServers:
    def __init__(self, parent: _StubNova) -> None:
        self._p = parent

    def create(self, **kwargs):
        self._p.create_kwargs.append(kwargs)
        return _StubServer("srv-1", kwargs["name"])

    def get(self, sid):
        if self._p.raise_on_get is not None:
            raise self._p.raise_on_get
        server = _StubServer(sid, "cc-x", self._p.get_status, self._p.get_fault)
        server.metadata = self._p.get_metadata
        return server

    def delete(self, sid):
        if self._p.raise_on_delete is not None:
            raise self._p.raise_on_delete


class _StubNovaFlavors:
    def __init__(self, parent: _StubNova) -> None:
        self._p = parent

    def get(self, ref):
        for f in self._p.flavors_data:
            if f.id == ref:
                return f
        raise RuntimeError("404 flavor not found")

    def list(self):
        return list(self._p.flavors_data)


class _StubNova:
    def __init__(self) -> None:
        self.create_kwargs: list[dict[str, Any]] = []
        self.raise_on_get: Exception | None = None
        self.raise_on_delete: Exception | None = None
        self.get_status = "BUILD"
        self.get_fault: dict[str, Any] | None = None
        self.get_metadata: dict[str, str] = {}
        self.flavors_data = [_StubFlavor("flav-baremetal", "baremetal")]
        self.servers = _StubNovaServers(self)
        self.keypairs = _StubKeypairs()
        self.flavors = _StubNovaFlavors(self)


class _StubKeypair:
    def __init__(self, name: str, public_key: str) -> None:
        self.name = name
        self.public_key = public_key


class _StubKeypairs:
    def __init__(self) -> None:
        self.stored: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    def get(self, name):
        if name not in self.stored:
            raise RuntimeError("404 keypair not found")
        return _StubKeypair(name, self.stored[name])

    def create(self, name, public_key, key_type):
        self.calls.append(("create", name, key_type))
        self.stored[name] = public_key
        return _StubKeypair(name, public_key)

    def delete(self, name):
        if name not in self.stored:
            raise RuntimeError("404 keypair not found")
        self.calls.append(("delete", name))
        del self.stored[name]

    def list(self):
        return [_StubKeypair(n, k) for n, k in self.stored.items()]


class _StubImage:
    def __init__(
        self, uid: str, name: str, created: str = "2026-01-01", supported: bool = True
    ) -> None:
        self.id = uid
        self.name = name
        self.created_at = created
        self.properties = {"created_at": created}
        if supported:
            self.properties["chameleon-supported"] = "true"

    def get(self, key: str) -> object:
        return self.properties.get(key)


class _StubGlanceImages:
    def __init__(self, images: list[_StubImage]) -> None:
        self.stored = images
        self.filters: list[dict[str, str]] = []

    def list(self, filters=None):
        self.filters.append(filters)
        return list(self.stored)


class _StubGlance:
    def __init__(self, images: list[_StubImage]) -> None:
        self.images = _StubGlanceImages(images)


class _StubNeutron:
    def __init__(self) -> None:
        self.fips: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self.created: list[dict] = []

    def list_networks(self, name):
        return {"networks": [{"id": f"net-{name}", "name": name}]}

    def create_floatingip(self, body):
        self.created.append(body)
        return {"floatingip": {"floating_ip_address": "192.0.2.9"}}

    def list_floatingips(self, floating_ip_address=None):
        return {
            "floatingips": [
                f for f in self.fips
                if floating_ip_address in (None, f["floating_ip_address"])
            ]
        }

    def list_ports(self, device_id):
        return {"ports": [{"id": f"port-of-{device_id}"}]}

    def update_floatingip(self, fip_id, body):
        self.updated.append((fip_id, body))

    def delete_floatingip(self, fip_id):
        self.deleted.append(fip_id)


class _StubAuth(keystoneauth1.plugin.BaseAuthPlugin):
    """A Keystone login with a fixed token."""

    def __init__(self, project_id: str = "project-9") -> None:
        super().__init__()
        self.project_id = project_id

    def get_token(self, session, **kwargs):
        return "token"

    def get_project_id(self, session, **kwargs):
        return self.project_id


_CONFIG = PUBLISHED_CONFIG.model_copy(update={"defaults": FAST.defaults})


def _everywhere(login: Login) -> dict[str, Login]:
    return dict.fromkeys(_CONFIG.sites(), login)


@dataclass
class _Stubs:
    """Provider doubles for the `chameleon` fixture."""

    blazar: _StubBlazar
    nova: _StubNova
    neutron: _StubNeutron
    glance: _StubGlance


@pytest.fixture()
def stubs(monkeypatch):
    """Replace every provider client python-chi would build."""
    from cloud_conversations.adapter.chameleon import adapter as mod

    s = _Stubs(
        blazar=_StubBlazar(),
        nova=_StubNova(),
        neutron=_StubNeutron(),
        glance=_StubGlance([_StubImage("img-2204", "CC-Ubuntu22.04")]),
    )
    monkeypatch.setattr(mod.chi.lease, "blazar", lambda *a, **k: s.blazar)
    monkeypatch.setattr(mod.chi.server, "nova", lambda *a, **k: s.nova)
    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: s.neutron)
    monkeypatch.setattr(mod.chi.image, "glance", lambda *a, **k: s.glance)
    return s


@pytest.fixture()
def chameleon(stubs):
    return ChameleonAdapter(
        config=_CONFIG, logins=_everywhere(Login(auth=lambda _url: _StubAuth(), secrets=("shh",)))
    )


def _lease_json(
    *,
    status: str = "ACTIVE",
    reservations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    return {
        "id": "lease-1",
        "name": "cc-run",
        "status": status,
        "start_date": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "end_date": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "reservations": reservations if reservations is not None else [],
    }


def test_find_image_matches_the_convention(chameleon):
    img = chameleon.find_image("CHI@UC", os_release="Ubuntu 22.04.4 LTS")
    assert img is not None
    assert img.image_id == "img-2204"


def test_find_image_returns_none_rather_than_echoing_the_request(chameleon):
    assert chameleon.find_image("CHI@UC", os_release="Plan9 4") is None


def test_find_image_needs_a_selector(chameleon):
    with pytest.raises(PreconditionError):
        chameleon.find_image("CHI@UC")


def test_lease_create_sends_blazar_formatted_date_strings(chameleon, stubs, run_id):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")

    chameleon.create_lease(
        "CHI@UC", duration_hours=2.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="k1",
    )

    call = stub.create_calls[0]
    start = datetime.strptime(call["start"], BLAZAR_TIME_FORMAT)
    datetime.strptime(call["end"], BLAZAR_TIME_FORMAT)
    assert start.replace(tzinfo=timezone.utc) > datetime.now(tz=timezone.utc)


def test_baremetal_reservation_dict_shape(chameleon, stubs, run_id):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")

    chameleon.create_lease(
        "CHI@UC", duration_hours=2.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="k1",
    )

    res = stub.create_calls[0]["reservations"]
    host = next(r for r in res if r["resource_type"] == "physical:host")
    assert host["min"] == 1 and host["max"] == 1
    assert host["hypervisor_properties"] == ""
    assert json.loads(host["resource_properties"]) == ["==", "$node_type", "compute_skylake"]


def test_kvm_reservation_dict_shape(chameleon, stubs, run_id):
    """Blazar requires `affinity`, set to None."""
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")

    chameleon.create_lease(
        "KVM@TACC", duration_hours=2.0, flavor_id="flav-1",
        run_id=run_id, idempotency_key="k1",
    )

    res = stub.create_calls[0]["reservations"]
    flavor = next(r for r in res if r["resource_type"] == "flavor:instance")
    assert flavor["flavor_id"] == "flav-1"
    assert flavor["amount"] == 1
    assert "affinity" in flavor and flavor["affinity"] is None


def test_floating_ip_reservation_is_included(chameleon, stubs, run_id):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")

    chameleon.create_lease(
        "CHI@UC", duration_hours=2.0, node_type="compute_skylake",
        with_floating_ip=True, run_id=run_id, idempotency_key="k1",
    )

    res = stub.create_calls[0]["reservations"]
    fip = next(r for r in res if r["resource_type"] == "virtual:floatingip")
    assert fip["network_id"] == "net-public"
    assert fip["amount"] == 1


def test_an_address_is_allocated_from_the_floating_ip_pool(chameleon, stubs, run_id):
    address = chameleon.allocate_floating_ip("CHI@UC", run_id=run_id, idempotency_key="k1")

    assert address == "192.0.2.9"
    assert stubs.neutron.created == [{"floatingip": {"floating_network_id": "net-public"}}]


def test_lease_records_a_resource_with_expiry(chameleon, stubs, run_id):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")
    start = datetime.now(tz=timezone.utc) + timedelta(hours=1)

    chameleon.create_lease(
        "CHI@UC", duration_hours=2.0, node_type="compute_skylake", start=start,
        run_id=run_id, idempotency_key="k1",
    )
    resources = store.live_resources(run_id)
    assert [r.kind for r in resources] == ["lease"]
    assert resources[0].expires_at == start + timedelta(hours=2)


def test_capacity_error_from_a_blazar_message(chameleon, stubs, run_id):
    stub = stubs.blazar

    def boom(**_kw):
        raise RuntimeError("Not enough hosts available")

    stub.lease.create = boom  # type: ignore[method-assign]
    with pytest.raises(CapacityError):
        chameleon.create_lease(
            "CHI@UC", duration_hours=2.0, node_type="compute_skylake",
            run_id=run_id, idempotency_key="k1",
        )


def test_blazar_out_of_floating_ips_is_an_address_shortage(chameleon, stubs, run_id):
    def boom(**_kw):
        raise RuntimeError("Not enough floating IPs available in the reservable pool")

    stubs.blazar.lease.create = boom  # type: ignore[method-assign]
    with pytest.raises(NoAddressError):
        chameleon.create_lease(
            "CHI@UC", duration_hours=2.0, node_type="compute_skylake",
            run_id=run_id, idempotency_key="k1",
        )


def test_lease_count_reads_min_not_min_count(chameleon, stubs):
    """`min_count` never appears on a physical:host reservation."""
    stub = stubs.blazar
    stub.lease_json = _lease_json(
        reservations=[{"id": "res-1", "resource_type": "physical:host", "min": 4, "max": 4}]
    )
    lease = chameleon.get_lease("CHI@UC", "lease-1")
    assert lease.count == 4
    assert lease.reservation_id == "res-1"


def test_reserved_flavor_id_is_taken_from_the_reservation(chameleon, stubs):
    """Blazar rewrites flavor_id to the reserved flavor once the lease starts."""
    stub = stubs.blazar
    stub.lease_json = _lease_json(
        reservations=[
            {
                "id": "res-flavor",
                "resource_type": "flavor:instance",
                "flavor_id": "res-flavor",
                "amount": 1,
            }
        ]
    )
    lease = chameleon.get_lease("KVM@TACC", "lease-1")
    assert lease.reserved_flavor_id == "res-flavor"


def test_pending_lease_exposes_no_reservation_ids(chameleon, stubs):
    stub = stubs.blazar
    stub.lease_json = _lease_json(
        status="PENDING",
        reservations=[{"id": "res-1", "resource_type": "physical:host", "min": 1, "max": 1}],
    )
    lease = chameleon.get_lease("CHI@UC", "lease-1")
    assert lease.reservation_id is None
    assert lease.reserved_fips == []


def test_reserved_fips_found_by_reservation_tag(chameleon, stubs):
    stub = stubs.blazar
    stub.lease_json = _lease_json(
        reservations=[{"id": "res-fip", "resource_type": "virtual:floatingip", "amount": 1}]
    )
    stubs.neutron.fips.extend(
        [
            {"floating_ip_address": "192.0.2.10", "tags": ["blazar", "reservation:res-fip"]},
            {"floating_ip_address": "192.0.2.99", "tags": ["reservation:someone-else"]},
        ]
    )
    lease = chameleon.get_lease("CHI@UC", "lease-1")
    assert lease.reserved_fips == ["192.0.2.10"]


def test_wait_lease_active_fires_the_heartbeat(chameleon, stubs):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")
    beats: list[int] = []
    with pytest.raises(AdapterTimeoutError):
        chameleon.wait_lease_active(
            "CHI@UC", "lease-1", 0.001, on_poll=lambda: beats.append(1)
        )
    assert beats


def test_availability_subtracts_allocated_hosts(chameleon, stubs):
    stub = stubs.blazar
    stub.hosts = [
        {"id": "h1", "node_type": "compute_skylake", "reservable": True},
        {"id": "h2", "node_type": "compute_skylake", "reservable": True},
    ]
    now = datetime.now(tz=timezone.utc)
    stub.allocations = [
        {
            "resource_id": "h1",
            "reservations": [
                {
                    "start_date": now.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "end_date": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.%f"),
                }
            ],
        }
    ]

    windows = chameleon.find_availability("CHI@UC", "compute_skylake", None, 2.0)
    assert [w.count for w in windows] == [1]


def test_availability_ignores_non_overlapping_reservations(chameleon, stubs):
    stub = stubs.blazar
    stub.hosts = [{"id": "h1", "node_type": "compute_skylake", "reservable": True}]
    later = datetime.now(tz=timezone.utc) + timedelta(days=30)
    stub.allocations = [
        {
            "resource_id": "h1",
            "reservations": [
                {
                    "start_date": later.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "end_date": (later + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.%f"),
                }
            ],
        }
    ]
    assert chameleon.find_availability("CHI@UC", "compute_skylake", None, 2.0)


def test_availability_skips_unreservable_hosts(chameleon, stubs):
    stub = stubs.blazar
    stub.hosts = [{"id": "h1", "node_type": "compute_skylake", "reservable": False}]
    assert chameleon.find_availability("CHI@UC", "compute_skylake", None, 2.0) == []


def _published_at(root: str) -> ChameleonAdapter:
    chameleon_table = _CONFIG.adapters["chameleon"].model_copy(
        update={"site_index_url": f"{root}/sites"}
    )
    config = _CONFIG.model_copy(update={"adapters": {"chameleon": chameleon_table}})
    return ChameleonAdapter(
        config=config, logins=_everywhere(Login(auth=lambda _url: _StubAuth(), secrets=()))
    )


def test_published_availability_asks_the_reference_api_not_blazar(stubs):
    free = node("compute_skylake", "uc", "available")
    with serving(page(free, free)) as (root, asked):
        windows = _published_at(root).published_availability(
            "CHI@UC", "compute_skylake", None, 2.0
        )

    assert [w.count for w in windows] == [2]
    assert [path for path, _ in asked] == ["/nodes/search"]
    assert stubs.blazar.host_list_attempts == 0


def test_a_hardware_search_asks_the_reference_api(stubs):
    with serving(page(node("compute_skylake", "uc", "available"))) as (root, asked):
        found = _published_at(root).search_hardware(NodeQuery(
            reserve_by="node_type", site="CHI@UC", node_type="compute_skylake",
            duration_hours=2, intent="a skylake node",
        ))

    assert [(c.site, c.compute, c.free) for c in found.items] == [("CHI@UC", "compute_skylake", 1)]
    assert [path for path, _ in asked] == ["/nodes/search"]


def test_a_hardware_search_with_no_reference_api_says_so(chameleon):
    with pytest.raises(FatalError, match="no reference API is configured"):
        chameleon.search_hardware(NodeQuery(
            reserve_by="node_type", site="CHI@UC", node_type="compute_skylake",
            duration_hours=2, intent="a skylake node",
        ))


def test_published_availability_falls_back_to_blazar_without_an_index(chameleon, stubs):
    chameleon.published_availability("CHI@UC", "compute_skylake", None, 2.0)

    assert stubs.blazar.host_list_attempts == 1


def test_kvm_availability_uses_the_flavor_endpoint(chameleon, stubs):
    stub = stubs.blazar
    stub.flavor_availability = {
        "flavor_instances": [
            {"flavor_id": "flav-1", "availability": [{"available": 5, "total": 8}]}
        ]
    }
    windows = chameleon.find_availability("KVM@TACC", None, "flav-1", 2.0)
    assert windows and windows[0].count == 5
    call = stub.availability_calls[0]
    datetime.strptime(call["start_date"], BLAZAR_TIME_FORMAT)


def test_list_flavors_maps_nova_and_drops_the_reserved_ones(chameleon, stubs):
    listed = [
        _StubFlavor("flav-1", "m1.large"),
        _StubFlavor("res-1", "reservation:res-1",
                    {"resources:CUSTOM_RESERVATION_RES_1": "1"}),
    ]
    stubs.nova.flavors_data = listed
    flavors = chameleon.list_flavors("KVM@TACC")

    assert [(f.flavor_id, f.name) for f in flavors] == [("flav-1", "m1.large")]
    assert (flavors[0].vcpus, flavors[0].mem_gb, flavors[0].disk_gb) == (4, 8, 40)
    assert [f.fetched for f in listed] == [0, 0]


def test_a_listing_without_extra_specs_fetches_them_per_flavor(chameleon, stubs):
    unlisted = [
        _StubFlavor("flav-1", "m1.large", listed=False),
        _StubFlavor("res-1", "reservation:res-1",
                    {"resources:CUSTOM_RESERVATION_RES_1": "1"}, listed=False),
    ]
    stubs.nova.flavors_data = unlisted

    assert [f.flavor_id for f in chameleon.list_flavors("KVM@TACC")] == ["flav-1"]
    assert [f.fetched for f in unlisted] == [1, 1]


def test_find_flavor_picks_one_name_out_of_the_listing(chameleon, stubs):
    stubs.nova.flavors_data = [
        _StubFlavor("flav-1", "m1.large"),
        _StubFlavor("flav-2", "m1.xlarge"),
        _StubFlavor("res-1", "m1.xlarge", {"resources:CUSTOM_RESERVATION_RES_1": "1"}),
    ]
    assert chameleon.find_flavor("KVM@TACC", "m1.xlarge").flavor_id == "flav-2"
    assert chameleon.find_flavor("KVM@TACC", "m1.huge") is None


def test_baremetal_boot_uses_a_scheduler_hint(chameleon, stubs, run_id):
    nova = stubs.nova
    chameleon.launch_instance(
        "CHI@UC",
        image_id="img-2204",
        keypair_name="cc-keypair",
        name="cc-x",
        reservation_id="res-1",
        run_id=run_id,
        idempotency_key="k1",
    )
    kwargs = nova.create_kwargs[0]
    assert kwargs["scheduler_hints"] == {"reservation": "res-1"}
    assert kwargs["flavor"] == "flav-baremetal"
    assert kwargs["nics"] == [{"net-id": "net-sharednet1"}]


def test_a_boot_with_neither_a_flavor_nor_a_reservation_is_refused(chameleon, run_id):
    with pytest.raises(PreconditionError) as raised:
        chameleon.launch_instance(
            "CHI@UC", image_id="img-2204", keypair_name="cc-keypair", name="cc-x",
            run_id=run_id, idempotency_key="k1",
        )
    assert str(raised.value) == "Precondition 'flavor_required' not met"


def test_kvm_boot_sends_no_scheduler_hint(chameleon, stubs, run_id):
    """KVM pins through the reserved flavor itself."""
    nova = stubs.nova
    nova.flavors_data = [_StubFlavor("res-flavor", "reservation:res-flavor")]

    chameleon.launch_instance(
        "KVM@TACC",
        image_id="img-2204",
        keypair_name="cc-keypair",
        name="cc-x",
        flavor="res-flavor",
        run_id=run_id,
        idempotency_key="k1",
    )
    kwargs = nova.create_kwargs[0]
    assert "scheduler_hints" not in kwargs
    assert kwargs["flavor"] == "res-flavor"


def test_boot_tags_the_instance_with_the_idempotency_key(chameleon, stubs, run_id):
    nova = stubs.nova
    chameleon.launch_instance(
        "CHI@UC", image_id="img-2204", keypair_name="cc-keypair", name="cc-x",
        reservation_id="res-1", run_id=run_id, idempotency_key="key-abc",
    )
    meta = nova.create_kwargs[0]["meta"]
    assert meta["run_id"] == str(run_id)
    assert meta["cc_ikey"] == "key-abc"
    assert meta["cc_system_key"] == system_key.current().fingerprint


def test_boot_adds_the_system_key_through_user_data(chameleon, stubs, run_id):
    chameleon.launch_instance(
        "CHI@UC", image_id="img-2204", keypair_name="cc-keypair", name="cc-x",
        reservation_id="res-1", run_id=run_id, idempotency_key="k1",
    )
    kwargs = stubs.nova.create_kwargs[0]
    assert kwargs["key_name"] == "cc-keypair"
    assert kwargs["userdata"] == (
        "#cloud-config\n"
        "ssh_authorized_keys:\n"
        f"  - ssh-rsa {system_key.current().get_base64()} cloud-conversations-system\n"
    )


def test_boot_records_the_expiry_it_was_given(chameleon, run_id):
    expiry = datetime.now(tz=timezone.utc) + timedelta(hours=2)
    chameleon.launch_instance(
        "CHI@UC", image_id="img-2204", keypair_name="cc-keypair", name="cc-x",
        reservation_id="res-1", expires_at=expiry, run_id=run_id, idempotency_key="k1",
    )
    inst = next(r for r in store.live_resources(run_id) if r.kind == "instance")
    assert inst.expires_at == expiry


@pytest.mark.parametrize(
    ("exc", "expected", "code"),
    [
        (RuntimeError("404 Not Found"), ProviderNotFoundError, "not_found"),
        (RuntimeError("503 Service Unavailable"), TransientError, "transient"),
        (RuntimeError("403 Forbidden"), FatalError, "unauthorized"),
        (RuntimeError("Quota exceeded for instances"), CapacityError, "capacity_exhausted"),
        (RuntimeError("something inexplicable"), FatalError, "provider_error"),
        (ks.ConnectFailure("aborted"), TransientError, "transient"),
        (ks.SSLError("bad cert"), FatalError, "provider_error"),
    ],
)
def test_provider_errors_map_to_the_adapter_vocabulary(chameleon, stubs, exc, expected, code):
    nova = stubs.nova
    nova.raise_on_get = exc
    with pytest.raises(expected) as raised:
        chameleon.get_instance("CHI@UC", "srv-1")
    assert raised.value.code == code


def test_missing_keypair_returns_none_but_an_outage_raises(chameleon, stubs):
    assert chameleon.find_keypair("CHI@UC", "absent") is None

    class _Boom:
        def get(self, name):
            raise RuntimeError("503 Service Unavailable")

    stubs.nova.keypairs = _Boom()
    with pytest.raises(TransientError):
        chameleon.find_keypair("CHI@UC", "anything")


_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKey me@laptop"


def test_importing_a_keypair_the_site_lacks_creates_it(chameleon, stubs):
    kp = chameleon.import_keypair("CHI@UC", "cc-laptop-x7k2p9", _PUBLIC_KEY)

    assert stubs.nova.keypairs.calls == [("create", "cc-laptop-x7k2p9", "ssh")]
    assert stubs.nova.keypairs.stored["cc-laptop-x7k2p9"] == _PUBLIC_KEY
    assert kp.name == "cc-laptop-x7k2p9"


def test_importing_a_keypair_the_site_holds_reuses_it_whatever_its_comment(chameleon, stubs):
    stubs.nova.keypairs.stored["cc-laptop-x7k2p9"] = _PUBLIC_KEY.replace("me@laptop", "other")

    chameleon.import_keypair("CHI@UC", "cc-laptop-x7k2p9", _PUBLIC_KEY)

    assert stubs.nova.keypairs.calls == []


def test_importing_a_changed_key_replaces_the_sites_keypair(chameleon, stubs):
    stubs.nova.keypairs.stored["cc-laptop-x7k2p9"] = "ssh-ed25519 AAAAOld me@laptop"

    chameleon.import_keypair("CHI@UC", "cc-laptop-x7k2p9", _PUBLIC_KEY)

    assert stubs.nova.keypairs.calls == [
        ("delete", "cc-laptop-x7k2p9"),
        ("create", "cc-laptop-x7k2p9", "ssh"),
    ]
    assert stubs.nova.keypairs.stored["cc-laptop-x7k2p9"] == _PUBLIC_KEY


def test_delete_is_idempotent_on_404(chameleon, stubs):
    """A delete that 404s is done once a GET also 404s."""
    nova = stubs.nova
    nova.raise_on_delete = RuntimeError("404 Not Found")
    nova.raise_on_get = RuntimeError("404 Not Found")
    chameleon.delete_instance("CHI@UC", "already-gone")

    stub = stubs.blazar
    stub.raise_on_delete = RuntimeError("404 Not Found")
    stub.lease_json = None
    chameleon.delete_lease("CHI@UC", "already-gone")


def test_delete_refuses_to_confirm_a_resource_it_can_still_see(chameleon, stubs):
    stub = stubs.blazar
    stub.lease_json = _lease_json()
    stub.raise_on_delete = RuntimeError("upstream dependency not found")

    with pytest.raises(FatalError, match="still visible at the provider"):
        chameleon.delete_lease("CHI@UC", "lease-1")


def test_a_three_digit_number_in_a_message_is_not_a_status(chameleon, stubs):
    nova = stubs.nova
    nova.raise_on_get = RuntimeError("instance has 404 vcpus, which is absurd")
    with pytest.raises(FatalError):
        chameleon.get_instance("CHI@UC", "srv-1")


def test_each_adapter_logs_in_as_its_own_principal(stubs):
    def adapter_for(project: str) -> ChameleonAdapter:
        return ChameleonAdapter(
            logins=_everywhere(Login(auth=lambda _url: _StubAuth(project))), config=_CONFIG
        )

    alice, bob = adapter_for("alice-project"), adapter_for("bob-project")

    assert alice._session("CHI@UC").get_project_id() == "alice-project"  # noqa: SLF001
    assert bob._session("CHI@UC").get_project_id() == "bob-project"  # noqa: SLF001


def test_each_site_gets_one_session_at_its_own_keystone(stubs):
    asked: list[str] = []

    def login(auth_url: str) -> _StubAuth:
        asked.append(auth_url)
        return _StubAuth()

    adapter = ChameleonAdapter(logins=_everywhere(Login(auth=login)), config=_CONFIG)
    for _ in range(3):
        uc = adapter._session("CHI@UC")  # noqa: SLF001
    kvm = adapter._session("KVM@TACC")  # noqa: SLF001

    assert asked == ["https://chi.uc.invalid:5000/v3", "https://kvm.tacc.invalid:5000/v3"]
    assert (uc.region_name, kvm.region_name) == ("CHI@UC", "KVM@TACC")


def test_unknown_site_is_rejected(chameleon):
    with pytest.raises(FatalError, match="Unknown site"):
        chameleon.list_instances("NOPE@NOWHERE")


def test_a_site_with_no_login_is_fatal_and_names_the_sites_with_one():
    a = ChameleonAdapter({"CHI@UC": Login(auth=lambda _url: _StubAuth())}, _CONFIG)
    with pytest.raises(FatalError) as raised:
        a.list_instances("KVM@TACC")
    assert raised.value.code == "no_credentials"
    assert "CHI@UC" in str(raised.value)


def test_every_principals_secret_is_scrubbed_from_an_error(stubs):
    a = ChameleonAdapter(
        {
            "CHI@UC": Login(auth=lambda _url: _StubAuth(), secrets=("uc-secret",)),
            "KVM@TACC": Login(auth=lambda _url: _StubAuth(), secrets=("tacc-secret",)),
        },
        _CONFIG,
    )
    stubs.blazar.lease_json = _lease_json()
    stubs.blazar.raise_on_delete = RuntimeError("not found: uc-secret and tacc-secret")

    with pytest.raises(FatalError) as raised:
        a.delete_lease("CHI@UC", "lease-1")
    assert str(raised.value).endswith(": not found: [redacted] and [redacted]")


def test_wait_survives_a_transient_blip(chameleon, stubs, monkeypatch):
    stub = stubs.blazar
    stub.lease_json = _lease_json(status="PENDING")
    calls = {"n": 0}
    real_get = chameleon.get_lease

    def flaky(site, lease_id):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise TransientError("Connection aborted.")
        stub.lease_json = _lease_json(status="ACTIVE")
        return real_get(site, lease_id)

    monkeypatch.setattr(chameleon, "get_lease", flaky)
    lease = chameleon.wait_lease_active("CHI@UC", "lease-1", timeout_s=100.0)
    assert lease.state == "ACTIVE"
    assert calls["n"] == 4


def test_wait_still_gives_up_on_a_terminal_state(chameleon, stubs):
    stubs.blazar.lease_json = _lease_json(status="ERROR")
    with pytest.raises(FatalError, match="entered ERROR"):
        chameleon.wait_lease_active("CHI@UC", "lease-1", timeout_s=10.0)


def test_wait_times_out_when_every_poll_is_transient(chameleon, monkeypatch):
    def always_flaky(site, lease_id):
        raise TransientError("Connection aborted.")

    monkeypatch.setattr(chameleon, "get_lease", always_flaky)
    with pytest.raises(AdapterTimeoutError) as exc:
        chameleon.wait_lease_active("CHI@UC", "lease-1", timeout_s=0.05)
    assert exc.value.last_state == "UNKNOWN"


def test_a_boot_error_names_nova_s_fault(chameleon, stubs):
    nova = stubs.nova
    nova.get_status = "ERROR"
    nova.get_fault = {
        "code": 500,
        "message": "No valid host was found",
        "details": "scheduler could not satisfy reservation hint",
    }
    with pytest.raises(UnschedulableError) as exc:
        chameleon.wait_instance_active("CHI@UC", "srv-1", timeout_s=1.0)
    assert "entered ERROR" in str(exc.value)
    assert "No valid host was found" in str(exc.value)
    assert "reservation hint" in str(exc.value)


def test_an_instance_without_a_fault_still_reports_the_state(chameleon, stubs):
    stubs.nova.get_status = "ERROR"
    with pytest.raises(FatalError, match="entered ERROR"):
        chameleon.wait_instance_active("CHI@UC", "srv-1", timeout_s=1.0)


def test_a_blazar_timeout_on_the_calendar_is_retried(chameleon, stubs):
    stub = stubs.blazar
    stub.host_list_failures = 2
    stub.hosts = [{"id": "h1", "node_type": "compute_skylake", "reservable": True}]
    stub.allocations = []

    windows = chameleon.find_availability("CHI@UC", "compute_skylake", None, 2.0)

    assert windows and windows[0].count == 1
    assert stub.host_list_attempts == 3


def test_a_blazar_timeout_that_never_clears_is_transient_not_fatal(chameleon, stubs):
    stub = stubs.blazar
    stub.host_list_failures = 99

    with pytest.raises(TransientError) as exc:
        chameleon.find_availability("CHI@UC", "compute_skylake", None, 2.0)
    assert "timed out" in str(exc.value)


def test_a_neutron_timeout_resolving_the_network_is_retried(
    chameleon, stubs, monkeypatch, run_id
):

    calls: list[str] = []
    real = stubs.neutron.list_networks

    def flaky(name):
        calls.append(name)
        if len(calls) < 3:
            raise RuntimeError(
                "Request to https://chi.tacc.invalid:9696/v2.0/networks timed out"
            )
        return real(name)

    monkeypatch.setattr(stubs.neutron, "list_networks", flaky)
    inst = chameleon.launch_instance(
        "CHI@UC",
        image_id="img-1",
        keypair_name="kp",
        name="cc-net-retry",
        flavor="baremetal",
        reservation_id="res-1",
        run_id=run_id,
        idempotency_key="ikey-net-retry",
    )
    assert inst.instance_id
    assert len(calls) == 3


class _FipNeutron:
    """An address attached to the port of instance srv-1."""

    @staticmethod
    def list_floatingips(**kwargs):
        return {"floatingips": [{"port_id": "port-1"}]}

    @staticmethod
    def show_port(port_id):
        return {"port": {"device_id": "srv-1"}}


@pytest.fixture()
def tagged(stubs):
    """Instance srv-1 was booted with the current system key."""
    stubs.nova.get_metadata = {"cc_system_key": system_key.current().fingerprint}


def _ssh_raising(exc: Exception):
    """A paramiko.SSHClient factory whose connect raises exc."""

    class _Client:
        def set_missing_host_key_policy(self, policy): ...

        def connect(self, *a, **k):
            raise exc

        def close(self): ...

    return lambda: _Client()


def test_a_socket_error_on_ssh_is_transient(chameleon, tagged, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(
        mod.paramiko, "SSHClient", _ssh_raising(OSError(65, "No route to host"))
    )

    with pytest.raises(TransientError) as raised:
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)
    assert "192.0.2.4" in str(raised.value)


def test_ssh_authentication_failure_stays_fatal(chameleon, tagged, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(
        mod.paramiko, "SSHClient", _ssh_raising(mod.paramiko.AuthenticationException("nope"))
    )

    with pytest.raises(FatalError) as raised:
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)
    assert raised.value.code == "ssh_auth"
    assert str(raised.value) == "SSH authentication failed for cc@192.0.2.4"


class _Channel:
    """A command channel that writes stdout forever, and has exited if exited is set."""

    def __init__(self, exited: bool) -> None:
        import threading

        self.status_event = threading.Event()
        self.reads = 0
        if exited:
            self.status_event.set()

    def recv_ready(self) -> bool:
        return True

    def recv(self, n: int) -> bytes:
        self.reads += 1
        return b"x" * n

    def recv_stderr_ready(self) -> bool:
        return False

    def exit_status_ready(self) -> bool:
        return self.status_event.is_set()

    def recv_exit_status(self) -> int:
        return 0


def _ssh_running(channel: _Channel):
    """A paramiko.SSHClient factory whose command runs on channel."""

    class _Stream:
        pass

    stream = _Stream()
    stream.channel = channel  # type: ignore[attr-defined]

    class _Client:
        def set_missing_host_key_policy(self, policy): ...

        def connect(self, *a, **k): ...

        def exec_command(self, command, timeout):
            return None, stream, stream

        def close(self): ...

    return _Client


def test_a_command_that_never_finishes_times_out(chameleon, tagged, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(mod.paramiko, "SSHClient", _ssh_running(_Channel(exited=False)))
    monkeypatch.setattr(mod, "_OUTPUT_BYTES", 10)

    with pytest.raises(AdapterTimeoutError) as raised:
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "sleep infinity", timeout_s=0.01)
    assert str(raised.value) == "the command on 192.0.2.4 did not finish in 0.01s"


def test_a_command_past_the_cap_still_finishes(chameleon, tagged, monkeypatch):
    import threading

    from cloud_conversations.adapter.chameleon import adapter as mod

    channel = _Channel(exited=False)
    threading.Timer(0.05, channel.status_event.set).start()
    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(mod.paramiko, "SSHClient", _ssh_running(channel))
    monkeypatch.setattr(mod, "_OUTPUT_BYTES", 10)

    result = chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)

    assert (result.stdout, result.exit_code) == ("x" * 10, 0)


def test_a_command_that_keeps_writing_after_it_exits_is_cut_at_the_cap(
    chameleon, tagged, monkeypatch
):
    from cloud_conversations.adapter.chameleon import adapter as mod

    channel = _Channel(exited=True)
    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(mod.paramiko, "SSHClient", _ssh_running(channel))
    monkeypatch.setattr(mod, "_OUTPUT_BYTES", 10)

    result = chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)

    assert (result.stdout, result.exit_code, channel.reads) == ("x" * 10, 0, 1)


def _ssh_recording(connected: list[dict]):
    """A paramiko.SSHClient factory that records each connect, then refuses it."""
    import paramiko

    class _Client:
        def set_missing_host_key_policy(self, policy): ...

        def connect(self, *a, **k):
            connected.append(k)
            raise paramiko.AuthenticationException("nope")

        def close(self): ...

    return lambda: _Client()


def test_ssh_uses_the_system_key_the_instance_was_booted_with(chameleon, stubs, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    booted_with = system_key.current()
    system_key.rotate()
    stubs.nova.get_metadata = {"cc_system_key": booted_with.fingerprint}
    connected: list[dict] = []
    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)
    monkeypatch.setattr(mod.paramiko, "SSHClient", _ssh_recording(connected))

    with pytest.raises(FatalError):
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)
    assert connected[0]["pkey"].fingerprint == booted_with.fingerprint
    assert connected[0]["allow_agent"] is False
    assert connected[0]["look_for_keys"] is False


def test_ssh_to_an_instance_booted_without_a_system_key_is_fatal(chameleon, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)

    with pytest.raises(FatalError) as raised:
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)
    assert raised.value.code == "ssh_auth"
    assert str(raised.value) == "192.0.2.4 was booted without a system key"


def test_ssh_as_a_system_key_the_database_lacks_is_fatal(chameleon, stubs, monkeypatch):
    from cloud_conversations.adapter.chameleon import adapter as mod

    stubs.nova.get_metadata = {"cc_system_key": "SHA256:gone"}
    monkeypatch.setattr(mod.chi.network, "neutron", lambda *a, **k: _FipNeutron)

    with pytest.raises(FatalError) as raised:
        chameleon.ssh_exec("CHI@UC", "192.0.2.4", "cc", "true", timeout_s=5.0)
    assert raised.value.code == "ssh_auth"
    assert str(raised.value) == (
        "192.0.2.4 trusts system key SHA256:gone, which the database does not hold"
    )


def test_own_images_are_filtered_to_the_callers_project(chameleon, stubs):
    stubs.glance.images.stored[:] = [
        _StubImage("img-old", "snapshot-june", "2026-06-01"),
        _StubImage("img-new", "snapshot-july", "2026-07-01"),
    ]

    images = chameleon.list_own_images("CHI@UC")

    assert stubs.glance.images.filters == [{"owner": "project-9"}]
    assert [i.name for i in images] == ["snapshot-july", "snapshot-june"]
    assert images[0].image_id == "img-new"


def test_capabilities_come_from_the_config_and_the_reference_api(tmp_path):
    from cloud_conversations.adapter.chameleon.sites import load_capabilities
    from cloud_conversations.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[adapters.chameleon]\nsite_index_url = "https://api.invalid/sites"\n'
        '[adapters.chameleon.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.chameleon.sites."CHI@TACC"]\nkinds = ["bm"]\n'
    )
    caps = load_capabilities(load_config(path), "chameleon")

    assert [s.site for s in caps.sites] == ["CHI@TACC"]
    site = caps.site("CHI@TACC")
    assert site is not None
    assert site.auth_url == "https://chi.tacc.invalid:5000/v3"
    assert site.node_catalog_url is not None


def test_a_configured_auth_url_wins_over_the_reference_api(tmp_path):
    from cloud_conversations.adapter.chameleon.sites import load_capabilities
    from cloud_conversations.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[adapters.chameleon.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.chameleon.sites."CHI@TACC"]\n'
        'kinds = ["bm"]\n'
        'auth_url = "https://private.invalid:5000/v3"\n'
        'node_catalog_url = "https://private.invalid/nodes"\n'
    )
    site = load_capabilities(load_config(path), "chameleon").site("CHI@TACC")
    assert site is not None
    assert site.auth_url == "https://private.invalid:5000/v3"


def test_a_kvm_site_gets_no_node_catalog(tmp_path):
    from cloud_conversations.adapter.chameleon.sites import load_capabilities
    from cloud_conversations.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        '[adapters.chameleon.kinds.vm]\nreservation_kind = "flavor_instance"\n'
        '[adapters.chameleon.sites."KVM@TACC"]\nkinds = ["vm"]\n'
    )
    site = load_capabilities(load_config(path), "chameleon").site("KVM@TACC")
    assert site is not None
    assert site.node_catalog_url is None


def test_every_image_is_listed_and_chameleon_supported_ones_are_marked(chameleon, stubs):
    stubs.glance.images.stored.append(_StubImage("img-mine", "my-image", supported=False))

    listed = chameleon.list_images("CHI@UC")

    assert [(i.name, i.supported) for i in listed] == [
        ("CC-Ubuntu22.04", True), ("my-image", False)
    ]


def test_an_os_release_picks_the_newest_supported_build_over_any_other(chameleon, stubs):
    stubs.glance.images.stored[:] = [
        _StubImage("old", "CC-Ubuntu24.04", created="2026-01-01T00:00:00Z"),
        _StubImage("new", "CC-Ubuntu24.04", created="2026-03-01T00:00:00Z"),
        _StubImage("theirs", "CC-Ubuntu24.04", created="2026-09-01T00:00:00Z", supported=False),
    ]

    chosen = chameleon.find_image("CHI@UC", os_release="Ubuntu 24.04")

    assert chosen is not None and chosen.image_id == "new"


def test_an_image_named_exactly_is_chosen_whether_or_not_it_is_supported(chameleon, stubs):
    stubs.glance.images.stored.append(_StubImage("img-mine", "my-image", supported=False))

    chosen = chameleon.find_image("CHI@UC", name="my-image")

    assert chosen is not None and chosen.image_id == "img-mine"


def test_attaching_an_address_puts_it_on_the_instances_port(chameleon, stubs):
    stubs.neutron.fips.append({"id": "fip-1", "floating_ip_address": "192.0.2.10"})

    chameleon.attach_floating_ip("CHI@UC", "srv-1", "192.0.2.10")

    assert stubs.neutron.updated == [("fip-1", {"floatingip": {"port_id": "port-of-srv-1"}})]


def test_deleting_an_address_deletes_the_floating_ip_it_names(chameleon, stubs):
    stubs.neutron.fips.append({"id": "fip-1", "floating_ip_address": "192.0.2.10"})

    chameleon.delete_floating_ip("CHI@UC", "192.0.2.10")

    assert stubs.neutron.deleted == ["fip-1"]


def test_the_keypairs_at_a_site_come_back_by_name(chameleon, stubs):
    stubs.nova.keypairs.stored.update({"zeta": "ssh-ed25519 AAAA a", "alpha": "ssh-ed25519 AAAA z"})

    assert [k.name for k in chameleon.list_keypairs("CHI@UC")] == ["alpha", "zeta"]


def test_deleting_a_keypair_says_whether_the_site_had_it(chameleon, stubs):
    stubs.nova.keypairs.stored["cc-laptop-a1b2c3"] = "ssh-ed25519 AAAA a"

    assert chameleon.delete_keypair("CHI@UC", "cc-laptop-a1b2c3") is True
    assert chameleon.delete_keypair("CHI@UC", "cc-laptop-a1b2c3") is False
    assert stubs.nova.keypairs.stored == {}
