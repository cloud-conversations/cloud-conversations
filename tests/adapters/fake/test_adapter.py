"""FakeAdapter, the in-memory testbed."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter.errors import (
    FatalError,
    NoAddressError,
    PreconditionError,
)
from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
from cloud_conversations.schema.search import NodeQuery
from tests.conftest import BM_SITE, FAST, KVM_SITE


def test_capabilities_exposes_sites(fake_adapter):
    caps = fake_adapter.capabilities()
    assert {s.site for s in caps.sites} == {BM_SITE, KVM_SITE}
    assert caps.site(BM_SITE).reserves_by() == ["node_type"]
    assert caps.site(KVM_SITE).reserves_by() == ["flavor"]


def test_availability_reflects_existing_leases(run_id):
    sites = default_fake_sites()
    sites[BM_SITE].stock = {"compute_skylake": 1}
    adapter = FakeAdapter(sites=sites, config=FAST)
    first = adapter.find_availability(BM_SITE, "compute_skylake", None, 2.0)
    assert first and first[0].count == 1

    adapter.create_lease(
        BM_SITE,
        duration_hours=2.0,
        node_type="compute_skylake",
        run_id=run_id,
        idempotency_key="k1",
    )
    assert adapter.find_availability(BM_SITE, "compute_skylake", None, 2.0) == []


def test_lease_starts_pending_then_active(fake_adapter, run_id):
    lease = fake_adapter.create_lease(
        BM_SITE, duration_hours=2.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="k1",
    )
    assert lease.state == "PENDING"
    assert lease.reservation_id is None

    active = fake_adapter.wait_lease_active(BM_SITE, lease.lease_id, 5.0)
    assert active.state == "ACTIVE"
    assert active.reservation_id
    assert active.reserved_fips


def test_create_lease_requires_exactly_one_reservation_target(fake_adapter, run_id):
    with pytest.raises(PreconditionError):
        fake_adapter.create_lease(BM_SITE, duration_hours=1.0, run_id=run_id, idempotency_key="k")
    with pytest.raises(PreconditionError):
        fake_adapter.create_lease(
            BM_SITE, duration_hours=1.0, node_type="x", flavor_id="y",
            run_id=run_id, idempotency_key="k",
        )


def test_kvm_lease_yields_a_reserved_flavor(fake_adapter, run_id):
    lease = fake_adapter.create_lease(
        KVM_SITE, duration_hours=2.0, flavor_id="m1.large",
        run_id=run_id, idempotency_key="k1",
    )
    active = fake_adapter.wait_lease_active(KVM_SITE, lease.lease_id, 5.0)
    assert active.reserved_flavor_id
    assert active.reservation_id is None


def test_launch_requires_an_active_reservation(fake_adapter, run_id):
    with pytest.raises(PreconditionError):
        fake_adapter.launch_instance(
            BM_SITE, image_id="img-ubuntu2204", keypair_name="cc-keypair",
            name="x", reservation_id="nope", run_id=run_id, idempotency_key="k",
        )


def test_launch_validates_its_inputs(fake_adapter, run_id):
    with pytest.raises(PreconditionError):
        fake_adapter.launch_instance(
            BM_SITE, image_id="", keypair_name="cc-keypair", name="x",
            run_id=run_id, idempotency_key="k",
        )
    with pytest.raises(PreconditionError):
        fake_adapter.launch_instance(
            BM_SITE, image_id="img", keypair_name="", name="x",
            run_id=run_id, idempotency_key="k",
        )


def test_attach_refuses_an_unreserved_address(fake_adapter, run_id):
    lease = fake_adapter.create_lease(
        BM_SITE, duration_hours=2.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="k1",
    )
    active = fake_adapter.wait_lease_active(BM_SITE, lease.lease_id, 5.0)
    inst = fake_adapter.launch_instance(
        BM_SITE, image_id="img-ubuntu2204", keypair_name="cc-keypair", name="cc-x",
        reservation_id=active.reservation_id, run_id=run_id, idempotency_key="k2",
    )
    fake_adapter.wait_instance_active(BM_SITE, inst.instance_id, 5.0)

    with pytest.raises(PreconditionError):
        fake_adapter.attach_floating_ip(BM_SITE, inst.instance_id, "203.0.113.7")


def test_ssh_requires_an_attached_address(fake_adapter):
    with pytest.raises(PreconditionError):
        fake_adapter.ssh_exec(BM_SITE, "10.0.0.99", "cc", "true", 5.0)


def test_unknown_site_is_fatal(fake_adapter):
    with pytest.raises(FatalError):
        fake_adapter.list_instances("NOPE@NOWHERE")


def test_an_empty_pool_fails_create_lease_as_blazar_does(run_id):
    sites = default_fake_sites()
    sites[BM_SITE].floating_ip_pool = 0
    adapter = FakeAdapter(sites=sites, config=FAST)
    with pytest.raises(NoAddressError, match="Not enough floating IPs"):
        adapter.create_lease(
            BM_SITE, duration_hours=1.0, node_type="compute_skylake",
            run_id=run_id, idempotency_key="k1",
        )


def test_a_lease_without_a_floating_ip_ignores_the_pool(run_id):
    sites = default_fake_sites()
    sites[BM_SITE].floating_ip_pool = 0
    adapter = FakeAdapter(sites=sites, config=FAST)
    lease = adapter.create_lease(
        BM_SITE, duration_hours=1.0, node_type="compute_skylake",
        with_floating_ip=False, run_id=run_id, idempotency_key="k1",
    )
    assert lease.lease_id


def test_a_hardware_search_counts_the_free_stock(fake_adapter):
    found = fake_adapter.search_hardware(NodeQuery(
        reserve_by="node_type", node_type="compute_skylake", duration_hours=2, intent="any",
    ))

    assert [(c.site, c.compute, c.free) for c in found.items] == [
        (BM_SITE, "compute_skylake", 10)
    ]
