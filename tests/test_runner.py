"""Runner tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.adapter.fake import (
    FAKE_KEYPAIR,
    FakeAdapter,
    FakeSite,
    HostFacts,
    default_fake_sites,
)
from cloud_conversations.adapter.protocol import SiteDescriptor
from cloud_conversations.config import KindFacts
from cloud_conversations.runner import sequential
from cloud_conversations.schema.runs import RunState
from tests.conftest import BARE_METAL, BM_SITE, FAST, KVM_SITE
from tests.fakes import FailureConfig, FaultyAdapter


def _describe(owner, requirements, rubric):
    return store.create_description(owner, "conv-runner", requirements, rubric)


def _provision(
    owner, requirements, rubric, adapter, site=BM_SITE, settings=None, keypair=FAKE_KEYPAIR
):
    did, ver = _describe(owner, requirements, rubric)
    return sequential.provision(
        owner_id=owner,
        description_id=did,
        version=ver,
        project_id=None,
        keypair=keypair,
        adapter=adapter,
        site=site,
        settings=settings or FAST.defaults,
    )


def _scopes(owner_id, run_id) -> list[str]:
    """The scope of every retry recorded on a run."""
    return [
        e.payload["scope"]
        for e in store.read_events(owner_id, run_id)
        if e.kind == "step_retried"
    ]


def _kinds(owner, run_id):
    return [e.kind for e in store.read_events(owner, run_id)]


def test_kvm_run_passes(owner_a, kvm_requirements, kvm_rubric, fake_adapter):
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure


def test_passed_requires_results_to_be_written(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    run = store.get_run(owner_a, run_id)

    assert run.state == RunState.passed
    assert run.results is not None
    assert len(run.results) == len(bm_rubric)
    assert all(r.passed for r in run.results)

    kinds = _kinds(owner_a, run_id)
    assert kinds.count("check_observed") == len(bm_rubric)
    assert "validation_completed" in kinds


def test_plan_proposed_carries_the_plan_body(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    proposed = [e for e in store.read_events(owner_a, run_id) if e.kind == "plan_proposed"]
    assert len(proposed) == 1
    assert proposed[0].payload["plan"]["steps"], "the plan must be reconstructable from the log"


def test_instance_carries_lease_expiry(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    by_kind = {r.kind: r for r in store.live_resources(run_id)}
    assert by_kind["instance"].expires_at is not None
    assert by_kind["instance"].expires_at == by_kind["lease"].expires_at


def test_hardware_mismatch_fails_the_run(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(),
        facts_overrides={"cc-": HostFacts(cpu_count=2, cpu_arch="aarch64")},
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)

    assert run.state == RunState.failed
    assert run.results is not None
    failed = {r.check_id for r in run.results if not r.passed}
    assert failed == {"arch", "cpus"}
    assert run.failure is not None and "arch" in run.failure.message


def test_failure_names_the_failing_check(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(),
        facts_overrides={"cc-": HostFacts(os_release="CentOS 8")},
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)
    assert run.failure is not None
    assert run.failure.kind == "description"
    assert run.failure.message == "rubric checks failed: ['os']"


def test_missing_image_is_a_description_failure(owner_a, bm_requirements, bm_rubric):
    sites = default_fake_sites()
    sites[BM_SITE].images = []
    adapter = FakeAdapter(sites=sites, config=FAST)
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)
    assert run.failure is not None and run.failure.kind == "description"


def test_a_missing_keypair_fails_before_anything_is_reserved(owner_a, bm_requirements, bm_rubric):
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)

    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter, keypair="nowhere-key")

    run = store.get_run(owner_a, run_id)
    assert run.failure is not None
    assert (run.failure.kind, run.failure.action) == ("description", "ensure_keypair")
    assert store.live_resources(run_id) == []
    assert adapter.leases == {}


def test_boot_failure_is_a_step_failure_with_console_log(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(instance_boot_fails=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)

    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.kind == "step"
    assert run.failure.action == "boot" and run.failure.step_id is not None
    failures = [e for e in store.read_events(owner_a, run_id) if e.kind == "step_failed"]
    assert any("console_log" in e.payload for e in failures)


def test_unexpected_exception_still_reaches_a_terminal_state(
    owner_a, bm_requirements, bm_rubric, monkeypatch
):
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)

    def explode(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(adapter, "find_image", explode)
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and "kaboom" in run.failure.message


def test_a_failed_run_keeps_its_resources_for_inspection(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(fip_attach_fails=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    assert store.get_run(owner_a, run_id).state == RunState.failed
    assert store.live_resources(run_id), "teardown is explicit, never implied by failure"


def test_requirement_nothing_can_act_on_is_rejected(owner_a, bm_requirements, bm_rubric):
    from cloud_conversations.schema.description import Requirement

    bm_requirements.items.append(
        Requirement(key="gpu_model", value="A100", source="stated")
    )
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.kind == "description"
    assert "gpu_model" in run.failure.message
    assert not store.live_resources(run_id), "nothing should have been built"


def test_requirement_bound_to_a_check_is_accepted(owner_a, bm_requirements, bm_rubric):
    from cloud_conversations.schema.description import Check, Requirement

    bm_requirements.items.append(Requirement(key="mem_gb", value=64, source="stated"))
    bm_rubric.append(
        Check(id="mem", kind="mem_gb", expected=64, comparator="gte", from_requirement="mem_gb")
    )
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    assert store.get_run(owner_a, run_id).state == RunState.passed


def test_multi_node_is_rejected(owner_a, bm_requirements, bm_rubric, fake_adapter):
    for item in bm_requirements.items:
        if item.key == "count":
            item.value = 4
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and "count=4" in run.failure.message
    assert not store.live_resources(run_id)


def test_wrong_reservation_kind_for_the_site_is_rejected(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, fake_adapter, BM_SITE)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert run.failure.message == "FAKE@BM takes node_type, not flavor"


def test_unknown_site_fails_before_building(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter, "NOPE@NOWHERE")
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert not store.live_resources(run_id)


def test_teardown_is_idempotent(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    sequential.teardown(run_id, fake_adapter)
    events = _kinds(owner_a, run_id)
    sequential.teardown(run_id, fake_adapter)
    assert _kinds(owner_a, run_id) == events


def test_teardown_releases_the_instance_before_the_lease(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)

    outcomes = sequential.teardown(run_id, fake_adapter)

    assert [o.kind for o in outcomes] == ["instance", "lease"]
    assert all(o.success for o in outcomes)


def test_teardown_records_each_release_once(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    built = len(store.live_resources(run_id))

    sequential.teardown(run_id, fake_adapter)

    assert _kinds(owner_a, run_id).count("resource_released") == built


def test_teardown_preserves_a_passed_outcome(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    assert store.get_run(owner_a, run_id).state == RunState.passed

    sequential.teardown(run_id, fake_adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed
    assert store.live_resources(run_id) == []
    kinds = _kinds(owner_a, run_id)
    assert kinds.count("run_terminal") == 1


@pytest.mark.parametrize(
    "interrupted_at", [RunState.provisioning, RunState.configuring, RunState.validating]
)
def test_resume_rewinds_from_any_interrupted_phase(
    owner_a, bm_requirements, bm_rubric, fake_adapter, interrupted_at
):
    did, ver = _describe(owner_a, bm_requirements, bm_rubric)
    run_id = store.create_run(owner_a, did, ver, None)
    plan = sequential.build_plan(
        bm_requirements,
        frozenset(c.kind for c in bm_rubric),
        run_id,
        fake_adapter.capabilities().site(BM_SITE),
        1,
        keypair=FAKE_KEYPAIR,
    )
    store.update_run(owner_a, run_id, plan=plan)
    for state in (RunState.provisioning, RunState.configuring, RunState.validating):
        store.update_run(owner_a, run_id, state=state)
        if state == interrupted_at:
            break

    sequential.resume(run_id, fake_adapter, FAST.defaults)
    assert store.get_run(owner_a, run_id).state == RunState.passed


def test_resume_refuses_a_settled_run(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    with pytest.raises(ValueError, match="nothing to resume"):
        sequential.resume(run_id, fake_adapter, FAST.defaults)


def test_failed_teardown_does_not_strand_the_run(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(fip_attach_fails=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    assert store.get_run(owner_a, run_id).state == RunState.failed

    stateless = FakeAdapter(sites=default_fake_sites(), config=FAST)  # has never seen these ids
    outcomes = sequential.teardown(run_id, stateless)

    assert outcomes and all(not o.success for o in outcomes)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed, "must not be abandoned when nothing was released"
    assert store.live_resources(run_id), "resources must stay visible for teardown"

    adapter.failures.fip_attach_fails = False
    sequential.resume(run_id, adapter, FAST.defaults)
    assert store.get_run(owner_a, run_id).state == RunState.passed


def test_a_host_unreachable_at_validation_still_records_a_result_set(
    owner_a, bm_requirements, bm_rubric
):
    from cloud_conversations.adapter.errors import TransientError
    from cloud_conversations.config import DEFAULT_DNS_PROBE_HOST
    from cloud_conversations.validator.probe import build_probe_script

    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    real_ssh = adapter.ssh_exec
    marker = build_probe_script(DEFAULT_DNS_PROBE_HOST).splitlines()[0]

    def flaky_ssh(site, host, user, command, timeout_s):
        if command.strip().startswith(marker):
            raise TransientError(f"SSH connection refused at {host}")
        return real_ssh(site, host, user, command, timeout_s)

    adapter.ssh_exec = flaky_ssh  # type: ignore[method-assign]

    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)

    assert run.state == RunState.failed
    assert run.results is not None, "the rubric must still have been evaluated"
    assert len(run.results) == len(bm_rubric)
    assert not any(r.passed for r in run.results)
    assert next(r.actual for r in run.results if r.kind == "ssh_reachable") is False

    kinds = _kinds(owner_a, run_id)
    assert kinds.count("check_observed") == len(bm_rubric)
    assert "validation_completed" in kinds

    assert run.failure is not None and run.failure.kind == "step"


def test_teardown_settles_when_every_release_succeeds(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(fip_attach_fails=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    outcomes = sequential.teardown(run_id, adapter)

    assert all(o.success for o in outcomes)
    assert store.get_run(owner_a, run_id).state == RunState.abandoned
    assert store.live_resources(run_id) == []


def test_a_plan_needs_an_image_selector(owner_a, bm_requirements, bm_rubric, fake_adapter):
    bm_requirements.items = [
        i for i in bm_requirements.items if i.key not in ("image_name", "os_release")
    ]
    bm_rubric = [c for c in bm_rubric if c.kind != "os_release"]
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert "image_name or os_release" in run.failure.message
    assert not store.live_resources(run_id), "nothing should have been built"


def test_image_name_alone_is_enough_to_plan(owner_a, bm_requirements, bm_rubric, fake_adapter):
    bm_requirements.items = [i for i in bm_requirements.items if i.key != "os_release"]
    bm_rubric = [c for c in bm_rubric if c.kind != "os_release"]
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    assert store.get_run(owner_a, run_id).state == RunState.passed


def _seed_foreign_resources(adapter, site=BM_SITE):
    """Provider resources with no run_resources row."""
    from datetime import datetime, timedelta, timezone

    from cloud_conversations.adapter.fake import FakeInstance, FakeLease

    now = datetime.now(tz=timezone.utc)
    for i, name in enumerate(["someone-elses-lease", "cc-lookalike-not-ours", "prod-lease"]):
        adapter.leases[f"foreign-lease-{i}"] = FakeLease(
            lease_id=f"foreign-lease-{i}", name=name, site=site, state="ACTIVE",
            start=now, end=now + timedelta(hours=8), count=1,
            node_type="compute_skylake", flavor_id=None,
            reservation_id=f"foreign-res-{i}", reserved_flavor_id=None,
            reserved_fips=[f"10.9.9.{i}"], run_id=None,
        )
    for i, name in enumerate(["someone-elses-vm", "cc-lookalike-vm", "prod-web-01"]):
        adapter.instances[f"foreign-inst-{i}"] = FakeInstance(
            instance_id=f"foreign-inst-{i}", name=name, site=site,
            image_id="img-ubuntu2404", keypair_name="their-key", flavor_id="baremetal",
            state="ACTIVE", floating_ip=f"10.9.9.{i}", node_type="compute_skylake",
            run_id=None,
        )
    return ([f"foreign-lease-{i}" for i in range(3)],
            [f"foreign-inst-{i}" for i in range(3)])


def test_teardown_cannot_touch_a_resource_it_did_not_create(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    ours = {(r.kind, r.provider_id) for r in store.live_resources(run_id)}
    assert ours, "the run must have built something to tear down"

    f_leases, f_insts = _seed_foreign_resources(fake_adapter)

    sequential.teardown(run_id, fake_adapter)

    assert store.live_resources(run_id) == []
    for lid in {p for k, p in ours if k == "lease"}:
        assert fake_adapter.leases[lid].state == "TERMINATED"
    for iid in {p for k, p in ours if k == "instance"}:
        assert fake_adapter.instances[iid].state == "DELETED"

    for lid in f_leases:
        lease = fake_adapter.leases[lid]
        assert lease.state == "ACTIVE", f"teardown terminated a foreign lease {lease.name}"
        assert lease.reserved_fips, "teardown released a foreign lease's floating IP"
    for iid in f_insts:
        inst = fake_adapter.instances[iid]
        assert inst.state == "ACTIVE", f"teardown deleted a foreign instance {inst.name}"
        assert inst.floating_ip is not None


def test_teardown_of_one_run_leaves_another_runs_resources_alone(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_a = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    run_b = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    b_before = {(r.kind, r.provider_id) for r in store.live_resources(run_b)}
    assert b_before

    sequential.teardown(run_a, fake_adapter)

    assert store.live_resources(run_a) == []
    assert {(r.kind, r.provider_id) for r in store.live_resources(run_b)} == b_before
    for kind, pid in b_before:
        if kind == "lease":
            assert fake_adapter.leases[pid].state == "ACTIVE"
        else:
            assert fake_adapter.instances[pid].state != "DELETED"


def _no_reservable_fips():
    sites = default_fake_sites()
    for s in sites.values():
        s.floating_ip_pool = 0
    return FakeAdapter(sites=sites, config=FAST)


def test_a_run_falls_back_to_an_on_demand_address(owner_a, bm_requirements, bm_rubric):
    adapter = _no_reservable_fips()
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure
    assert run.connection is not None and run.connection.host

    kinds = {r.kind for r in store.live_resources(run_id)}
    assert kinds == {"lease", "instance", "floating_ip"}, (
        "the allocated address must be recorded; the lease will not reclaim it"
    )


def test_the_on_demand_address_is_released_on_teardown(owner_a, bm_requirements, bm_rubric):
    adapter = _no_reservable_fips()
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    address = next(
        r.provider_id for r in store.live_resources(run_id) if r.kind == "floating_ip"
    )
    assert address in adapter.adhoc_fips

    outcomes = sequential.teardown(run_id, adapter)

    assert all(o.success for o in outcomes)
    assert store.live_resources(run_id) == []
    assert address not in adapter.adhoc_fips, "the address leaked at the provider"


def test_a_reserved_address_is_still_preferred(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, fake_adapter)
    assert store.get_run(owner_a, run_id).state == RunState.passed
    kinds = {r.kind for r in store.live_resources(run_id)}
    assert kinds == {"lease", "instance"}, "no ad-hoc address should exist"
    assert not fake_adapter.adhoc_fips
    assert not fake_adapter.security_groups


def test_no_address_anywhere_is_a_plan_failure(owner_a, bm_requirements, bm_rubric):
    sites = default_fake_sites()
    for s in sites.values():
        s.floating_ip_pool = 0
        s.floating_ip_quota = 0
    adapter = FakeAdapter(sites=sites, config=FAST)
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.kind == "plan"
    assert run.failure.action == "assign_ip"


def test_a_compute_shortage_does_not_trigger_the_fallback(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(capacity_exhausted=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    run = store.get_run(owner_a, run_id)
    assert run.failure is not None and run.failure.kind == "plan"
    assert run.failure.action == "reserve", "it must fail at reserve, not limp onward"


def test_resume_does_not_allocate_a_second_address(owner_a, bm_requirements, bm_rubric):
    sites = default_fake_sites()
    for s in sites.values():
        s.floating_ip_pool = 0
    # fip_attach_fails fails after the address is allocated and recorded.
    adapter = FaultyAdapter(sites=sites, failures=FailureConfig(fip_attach_fails=True))
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)
    assert store.get_run(owner_a, run_id).state == RunState.failed
    before = {(r.kind, r.provider_id) for r in store.live_resources(run_id)}
    assert any(k == "floating_ip" for k, _ in before)

    adapter.failures.fip_attach_fails = False
    sequential.resume(run_id, adapter, FAST.defaults)

    assert store.get_run(owner_a, run_id).state == RunState.passed
    assert {(r.kind, r.provider_id) for r in store.live_resources(run_id)} == before


def test_a_kvm_run_creates_and_owns_a_security_group(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure
    kinds = {r.kind for r in store.live_resources(run_id)}
    assert "security_group" in kinds
    assert fake_adapter.security_groups, "the group must exist at the provider"


MIXED_SITE = "FAKE@MIXED"


@pytest.fixture()
def mixed_adapter() -> FakeAdapter:
    sites = default_fake_sites()
    bm, vm = sites[BM_SITE], sites[KVM_SITE]
    mixed = FakeSite(
        descriptor=SiteDescriptor(
            site=MIXED_SITE,
            auth_url="https://fake-mixed.invalid:5000/v3",
            kinds=[KindFacts(reservation_kind="flavor_instance", uses_security_groups=True),
                   *BARE_METAL],
            node_types=bm.descriptor.node_types,
            flavors=vm.descriptor.flavors,
        ),
        images=bm.images,
        keypairs=bm.keypairs,
        stock={**bm.stock, **vm.stock},
    )
    return FakeAdapter(sites={MIXED_SITE: mixed}, config=FAST)


def test_a_vm_at_a_site_with_both_kinds_gets_a_security_group(
    owner_a, kvm_requirements, kvm_rubric, mixed_adapter
):
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, mixed_adapter, MIXED_SITE)
    assert store.get_run(owner_a, run_id).state == RunState.passed
    assert "security_group" in {r.kind for r in store.live_resources(run_id)}


def test_bare_metal_at_a_site_with_both_kinds_gets_no_security_group(
    owner_a, bm_requirements, bm_rubric, mixed_adapter
):
    run_id = _provision(owner_a, bm_requirements, bm_rubric, mixed_adapter, MIXED_SITE)
    assert store.get_run(owner_a, run_id).state == RunState.passed
    assert "security_group" not in {r.kind for r in store.live_resources(run_id)}


def test_the_security_group_is_released_on_teardown(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    group_id = next(
        r.provider_id for r in store.live_resources(run_id) if r.kind == "security_group"
    )
    outcomes = sequential.teardown(run_id, fake_adapter)

    assert all(o.success for o in outcomes)
    assert store.live_resources(run_id) == []
    assert group_id not in fake_adapter.security_groups, "the group leaked"


def test_resume_does_not_create_a_second_security_group(
    owner_a, kvm_requirements, kvm_rubric
):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(fip_attach_fails=True)
    )
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, adapter, KVM_SITE)
    assert store.get_run(owner_a, run_id).state == RunState.failed
    before = {(r.kind, r.provider_id) for r in store.live_resources(run_id)}
    assert any(k == "security_group" for k, _ in before)

    adapter.failures.fip_attach_fails = False
    sequential.resume(run_id, adapter, FAST.defaults)

    assert store.get_run(owner_a, run_id).state == RunState.passed
    assert {(r.kind, r.provider_id) for r in store.live_resources(run_id)} == before
    assert len(adapter.security_groups) == 1


def test_teardown_waits_out_an_asynchronous_instance_delete(
    owner_a, kvm_requirements, kvm_rubric
):
    """Nova's delete returns before the server is gone, and its group stays bound."""
    adapter = FaultyAdapter(
        sites=default_fake_sites(),
        failures=FailureConfig(instance_delete_polls=3),
    )
    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, adapter, KVM_SITE)
    assert store.get_run(owner_a, run_id).state == RunState.passed
    assert {r.kind for r in store.live_resources(run_id)} >= {"instance", "security_group"}

    outcomes = sequential.teardown(run_id, adapter)

    assert all(o.success for o in outcomes), [
        (o.kind, o.error) for o in outcomes if not o.success
    ]
    assert store.live_resources(run_id) == []
    assert not adapter.security_groups, "the group leaked behind a deleting instance"


def test_a_group_bound_to_a_live_instance_cannot_be_deleted(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    from cloud_conversations.adapter.errors import FatalError

    run_id = _provision(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    group_id = next(
        r.provider_id for r in store.live_resources(run_id) if r.kind == "security_group"
    )
    with pytest.raises(FatalError, match="in use"):
        fake_adapter.delete_security_group(KVM_SITE, group_id)


def test_a_boot_error_attaches_the_console_log(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(instance_boot_errors=True)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.action == "boot"
    assert "Failure prepping block device" in run.failure.message

    failures = [e for e in store.read_events(owner_a, run_id) if e.kind == "step_failed"]
    assert any("console_log" in e.payload for e in failures), (
        "an ERROR boot must attach the console, not only a timeout"
    )


def test_boot_retries_an_unschedulable_placement(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(unschedulable_boots=1)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure

    kinds = [e.kind for e in store.read_events(owner_a, run_id)]
    assert _scopes(owner_a, run_id) == ["step"], "the boot retry is recorded as one"
    live = [r for r in store.live_resources(run_id) if r.kind == "instance"]
    assert len(live) == 1
    assert kinds.count("resource_released") >= 1, "the rejected instance must be released"


def test_an_unplaceable_host_is_a_plan_failure_naming_the_node(
    owner_a, bm_requirements, bm_rubric
):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(unschedulable_boots=99)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert run.failure.action == "boot"
    assert run.failure.kind == "plan", "re-asking the same reservation cannot help"
    assert "fake-node-01" in run.failure.message, run.failure.message
    assert "attempt(s)" in run.failure.message

    assert not [r for r in store.live_resources(run_id) if r.kind == "instance"]


def test_an_errored_reservation_fails_before_booting(owner_a, bm_requirements, bm_rubric):
    """A lease can be ACTIVE while its reservation will never be schedulable."""
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(reservation_status="error")
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.action == "reserve"
    assert "reservation is 'error'" in run.failure.message
    assert not [r for r in store.live_resources(run_id) if r.kind == "instance"], (
        "it must fail before booting anything"
    )


def test_an_unusable_host_is_retried_on_a_new_reservation(
    owner_a, bm_requirements, bm_rubric
):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(unschedulable_boots=2)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure
    assert _scopes(owner_a, run_id).count("reservation") == 1

    leases = [r for r in store.live_resources(run_id) if r.kind == "lease"]
    assert len(leases) == 1
    released = [
        e.payload["provider_id"]
        for e in store.read_events(owner_a, run_id)
        if e.kind == "resource_released" and e.payload["kind"] == "lease"
    ]
    assert len(released) == 1, "the unusable host's lease must be released"
    assert released[0] != leases[0].provider_id, "the run must boot on a new lease"
    assert adapter.leases[released[0]].state == "TERMINATED", (
        "the lease must be deleted at the provider, not just marked released"
    )


def _quick_ssh():
    return FAST.defaults.model_copy(update={"ssh_s": 0.05})


def test_an_address_that_never_answers_is_replaced(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(ssh_dead_addresses=1)
    )
    run_id = _provision(
        owner_a, bm_requirements, bm_rubric, adapter, settings=_quick_ssh()
    )

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.passed, run.failure
    assert _scopes(owner_a, run_id).count("address") == 1
    assert run.connection is not None
    lease = next(r for r in store.live_resources(run_id) if r.kind == "lease")
    reserved = adapter.leases[lease.provider_id].reserved_fips
    assert run.connection.host not in reserved, "it connected on another address"
    assert reserved, "the lease keeps the address it reserved"


def test_the_address_budget_is_the_limit(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(ssh_dead_addresses=2)
    )
    run_id = _provision(
        owner_a, bm_requirements, bm_rubric, adapter, settings=_quick_ssh()
    )

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None and run.failure.kind == "step"
    assert run.failure.action == "wait_ssh"
    assert _scopes(owner_a, run_id).count("address") == 1


def test_one_reservation_attempt_does_not_retry(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(sites=default_fake_sites(), failures=FailureConfig(deploy_fails=True))
    settings = FAST.defaults.model_copy(update={"reservation_attempts": 1})
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter, settings=settings)

    assert store.get_run(owner_a, run_id).state == RunState.failed
    assert "reservation" not in _scopes(owner_a, run_id)


def test_a_host_that_fails_to_deploy_is_a_plan_failure_naming_the_node(
    owner_a, bm_requirements, bm_rubric
):
    adapter = FaultyAdapter(sites=default_fake_sites(), failures=FailureConfig(deploy_fails=True))
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert run.failure.action == "boot"
    assert run.failure.kind == "plan"
    assert "fake-node-01" in run.failure.message, run.failure.message
    assert "after 1 attempt(s)" in run.failure.message, "Nova already exhausted its retries"
    assert not [r for r in store.live_resources(run_id) if r.kind == "instance"]
    assert _scopes(owner_a, run_id).count("reservation") == 1


def test_a_start_time_reaches_the_reserve_step(bm_requirements, bm_rubric, fake_adapter):
    tuesday = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    requirements = bm_requirements.model_copy(update={"start_at": tuesday})

    plan = sequential.build_plan(
        requirements,
        frozenset(c.kind for c in bm_rubric),
        uuid.uuid4(),
        fake_adapter.capabilities().site(BM_SITE),
        1,
        keypair=FAKE_KEYPAIR,
    )

    (reserve,) = [s for s in plan.steps if s.action == "reserve"]
    assert reserve.args["start_at"] == "2026-09-08T14:00:00+00:00"


def test_no_start_time_leaves_the_reserve_step_to_choose(
    bm_requirements, bm_rubric, fake_adapter
):
    plan = sequential.build_plan(
        bm_requirements,
        frozenset(c.kind for c in bm_rubric),
        uuid.uuid4(),
        fake_adapter.capabilities().site(BM_SITE),
        1,
        keypair=FAKE_KEYPAIR,
    )
    (reserve,) = [s for s in plan.steps if s.action == "reserve"]
    assert reserve.args["start_at"] is None
