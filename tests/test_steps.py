"""Step functions and the validator, against the fake adapter and a real DB."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.adapter.fake import FakeAdapter, FakeInstance, default_fake_sites
from cloud_conversations.adapter.protocol import Image
from cloud_conversations.schema.description import Check, ImageChoice, Requirement
from cloud_conversations.steps.context import StepContext, StepError
from cloud_conversations.steps.idempotency import derive_key
from cloud_conversations.steps.provision import (
    assign_ip,
    boot,
    ensure_keypair,
    reserve,
    select_image,
)
from cloud_conversations.validator.evaluate import ComparisonError, compare, evaluate_rubric
from cloud_conversations.validator.probe import parse_probe_output
from tests.conftest import BM_SITE, FAST, KVM_SITE

_SRC = Path(__file__).resolve().parent.parent / "src"


def _ctx(run_id, owner_id, requirements, adapter, site=BM_SITE, outputs=None):
    return StepContext(
        run_id=run_id,
        owner_id=owner_id,
        site=site,
        adapter=adapter,
        requirements=requirements,
        settings=FAST.defaults,
        outputs=outputs if outputs is not None else {},
    )


@pytest.fixture()
def one_node() -> FakeAdapter:
    """FAKE@BM with a single skylake node."""
    sites = default_fake_sites()
    sites[BM_SITE].stock = {"compute_skylake": 1}
    return FakeAdapter(sites=sites, config=FAST)


def _run(owner_id, requirements, rubric) -> uuid.UUID:
    did, ver = store.create_description(owner_id, "conv-steps", requirements, rubric)
    return store.create_run(owner_id, did, ver, None)


def test_reserve_creates_one_lease_with_a_floating_ip(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    ctx = _ctx(run_id, owner_a, bm_requirements, fake_adapter)

    out = reserve(ctx, {"step_id": "s1", "node_type": "compute_skylake"})

    assert out["reservation_id"]
    assert out["reserved_fip"], "the floating IP must be reserved in the lease"
    assert out["lease_end"] is not None
    resources = store.live_resources(run_id)
    assert [r.kind for r in resources] == ["lease"]
    assert resources[0].expires_at is not None


def test_reserve_on_kvm_returns_a_reserved_flavor(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id = _run(owner_a, kvm_requirements, kvm_rubric)
    ctx = _ctx(run_id, owner_a, kvm_requirements, fake_adapter, KVM_SITE)

    out = reserve(ctx, {"step_id": "s1", "flavor": "m1.large"})

    assert out["reserved_flavor_id"], "KVM boots the reserved flavor Blazar creates"
    assert out["reservation_id"] is None, "no scheduler hint on the KVM path"


def test_reserve_unknown_flavor_is_a_description_failure(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id = _run(owner_a, kvm_requirements, kvm_rubric)
    ctx = _ctx(run_id, owner_a, kvm_requirements, fake_adapter, KVM_SITE)

    with pytest.raises(StepError) as exc:
        reserve(ctx, {"step_id": "s1", "flavor": "nonexistent.flavor"})
    assert exc.value.failure_kind == "description"


def test_reserve_is_idempotent(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    args = {"step_id": "s1", "node_type": "compute_skylake"}

    first = reserve(_ctx(run_id, owner_a, bm_requirements, fake_adapter), args)
    second = reserve(_ctx(run_id, owner_a, bm_requirements, fake_adapter), args)

    assert first["lease_id"] == second["lease_id"]
    assert len([r for r in store.live_resources(run_id) if r.kind == "lease"]) == 1


def test_select_image_matches_the_cc_naming_convention(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    out = select_image(
        _ctx(run_id, owner_a, bm_requirements, fake_adapter),
        {"step_id": "s2", "os_release": "Ubuntu 22.04"},
    )
    assert out["image_name"] == "CC-Ubuntu22.04"


def test_select_image_honours_a_cpu_arch_requirement(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    fake_adapter._sites[BM_SITE].images = [  # noqa: SLF001
        Image(image_id="x86-id", name="CC-Ubuntu24.04"),
        Image(image_id="arm-id", name="CC-Ubuntu24.04-ARM64"),
    ]
    bm_requirements.items = [
        item for item in bm_requirements.items if item.key != "image_name"
    ] + [Requirement(key="cpu_arch", value="aarch64", source="stated")]
    run_id = _run(owner_a, bm_requirements, bm_rubric)

    out = select_image(
        _ctx(run_id, owner_a, bm_requirements, fake_adapter),
        {"step_id": "s2", "os_release": "Ubuntu 24.04"},
    )
    assert out["image_id"] == "arm-id"


def test_select_image_boots_the_approved_image_and_picks_no_other(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    bm_requirements.image = ImageChoice(
        image_id="img-ubuntu2204", name="CC-Ubuntu22.04", supported=True, testbed="Chameleon"
    )
    run_id = _run(owner_a, bm_requirements, bm_rubric)

    out = select_image(
        _ctx(run_id, owner_a, bm_requirements, fake_adapter),
        {"step_id": "s2", "os_release": "Ubuntu 24.04"},
    )
    assert out["image_id"] == "img-ubuntu2204"


def test_an_approved_image_that_is_gone_is_a_description_failure(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    bm_requirements.image = ImageChoice(
        image_id="img-gone", name="CC-Ubuntu24.04", supported=True, testbed="Chameleon"
    )
    run_id = _run(owner_a, bm_requirements, bm_rubric)

    with pytest.raises(StepError) as exc:
        select_image(
            _ctx(run_id, owner_a, bm_requirements, fake_adapter),
            {"step_id": "s2", "os_release": "Ubuntu 24.04"},
        )
    assert exc.value.failure_kind == "description"
    assert "img-gone" in str(exc.value)


def test_ensure_keypair_finds_the_users_key(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    out = ensure_keypair(
        _ctx(run_id, owner_a, bm_requirements, fake_adapter),
        {"step_id": "s3", "ssh_key_ref": "cc-keypair"},
    )
    assert out["keypair_name"] == "cc-keypair"


def test_ensure_keypair_creates_the_keypair_of_an_imported_key(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    key = store.import_user_key(owner_a, "laptop", "cc-laptop-x7k2p9", "ssh-ed25519 AAAA me")
    run_id = _run(owner_a, bm_requirements, bm_rubric)

    out = ensure_keypair(
        _ctx(run_id, owner_a, bm_requirements, fake_adapter),
        {"step_id": "s3", "ssh_key_ref": key.keypair_name},
    )

    assert out["keypair_name"] == "cc-laptop-x7k2p9"
    found = fake_adapter.find_keypair(BM_SITE, "cc-laptop-x7k2p9")
    assert found is not None and found.public_key == "ssh-ed25519 AAAA me"


def test_ensure_keypair_ignores_a_key_another_owner_imported(
    owner_a, owner_b, bm_requirements, bm_rubric, fake_adapter
):
    store.import_user_key(owner_b, "laptop", "cc-laptop-x7k2p9", "ssh-ed25519 AAAA bob")
    run_id = _run(owner_a, bm_requirements, bm_rubric)

    with pytest.raises(StepError) as exc:
        ensure_keypair(
            _ctx(run_id, owner_a, bm_requirements, fake_adapter),
            {"step_id": "s3", "ssh_key_ref": "cc-laptop-x7k2p9"},
        )
    assert exc.value.failure_kind == "description"


def _reserved(owner_a, requirements, rubric, adapter, site=BM_SITE):
    run_id = _run(owner_a, requirements, rubric)
    ctx = _ctx(run_id, owner_a, requirements, adapter, site)
    key = "node_type" if site == BM_SITE else "flavor"
    val = "compute_skylake" if site == BM_SITE else "m1.large"
    ctx.outputs.update(reserve(ctx, {"step_id": "s1", key: val}))
    ctx.outputs.update(select_image(ctx, {"step_id": "s2", "os_release": "Ubuntu 22.04"}))
    ctx.outputs.update(ensure_keypair(ctx, {"step_id": "s3", "ssh_key_ref": "cc-keypair"}))
    return run_id, ctx


def test_boot_is_idempotent(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id, ctx = _reserved(owner_a, bm_requirements, bm_rubric, fake_adapter)
    args = {"step_id": "s4", "name": f"cc-{run_id}"}
    first = boot(ctx, args)
    second = boot(ctx, args)
    assert first["instance_id"] == second["instance_id"]
    assert len([r for r in store.live_resources(run_id) if r.kind == "instance"]) == 1


def test_assign_ip_attaches_the_reserved_address(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id, ctx = _reserved(owner_a, bm_requirements, bm_rubric, fake_adapter)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    fake_adapter.wait_instance_active(BM_SITE, ctx.outputs["instance_id"], 1.0)

    out = assign_ip(ctx, {"step_id": "s5"})

    assert out["host"] == ctx.outputs["reserved_fip"]
    assert {r.kind for r in store.live_resources(run_id)} == {"lease", "instance"}


def test_assign_ip_refuses_an_unreserved_address(owner_a, bm_requirements, bm_rubric, fake_adapter):
    run_id, ctx = _reserved(owner_a, bm_requirements, bm_rubric, fake_adapter)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    fake_adapter.wait_instance_active(BM_SITE, ctx.outputs["instance_id"], 1.0)
    ctx.outputs["reserved_fip"] = "203.0.113.99"

    with pytest.raises(StepError) as exc:
        assign_ip(ctx, {"step_id": "s5"})
    assert exc.value.failure_kind == "step"


def test_idempotency_key_stable_across_processes():
    run_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    args = {"node_type": "compute_skylake", "count": 1, "duration_hours": 2}
    in_process = derive_key(run_id, "step-0001", "reserve", args)

    code = (
        "from cloud_conversations.steps.idempotency import derive_key; import uuid; "
        "run_id = uuid.UUID('12345678-1234-5678-1234-567812345678'); "
        "args = {'duration_hours': 2, 'count': 1, 'node_type': 'compute_skylake'}; "
        "print(derive_key(run_id, 'step-0001', 'reserve', args))"
    )
    env = {**os.environ, "PYTHONPATH": str(_SRC), "PYTHONHASHSEED": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr
    assert in_process == result.stdout.strip()


def test_parse_probe_output_types_scalars():
    facts = parse_probe_output(
        "cpu_arch=x86_64\ncpu_count=48\nmem_gb=192\ndisk_gb=480\n"
        "kernel=5.15.0\nos_release=Ubuntu 22.04\ndns_resolves=true\n"
    )
    assert facts["cpu_count"] == 48
    assert facts["mem_gb"] == 192
    assert facts["dns_resolves"] is True
    assert facts["os_release"] == "Ubuntu 22.04"


def test_parse_probe_output_ignores_junk():
    facts = parse_probe_output("garbage\ncpu_count=notanumber\ncpu_arch=x86_64\n")
    assert facts == {"cpu_arch": "x86_64"}


@pytest.mark.parametrize(
    ("comparator", "expected", "actual", "want"),
    [
        ("eq", "x86_64", "x86_64", True),
        ("eq", "x86_64", "aarch64", False),
        ("eq", True, True, True),
        ("eq", 4, 4, True),
        ("gte", 48, 24, False),
        ("gte", 24, 24, True),
        ("prefix", "Ubuntu 22.04", "Ubuntu 22.04.4 LTS", True),
        ("prefix", "Ubuntu 24", "Ubuntu 22.04", False),
    ],
)
def test_compare(comparator, expected, actual, want):
    assert compare(comparator, expected, actual) is want


def test_compare_missing_fact_never_passes():
    assert compare("eq", "x86_64", None) is False
    assert compare("gte", 4, None) is False


def test_compare_rejects_a_nonsense_comparison():
    with pytest.raises(ComparisonError):
        compare("gte", "not-a-number", "also-not")


def test_evaluate_rubric_fails_unverifiable_checks_rather_than_raising():
    rubric = [
        Check(id="a", kind="cpu_count", expected=4, comparator="gte"),
        Check(id="b", kind="mem_gb", expected="lots", comparator="gte"),
    ]
    results = evaluate_rubric(rubric, {"cpu_count": 8})
    assert {r.check_id: r.passed for r in results} == {"a": True, "b": False}


def test_evaluate_rubric_records_what_was_seen():
    rubric = [Check(id="a", kind="cpu_count", expected=16, comparator="gte")]
    results = evaluate_rubric(rubric, {"cpu_count": 8})
    assert results[0].actual == 8
    assert results[0].expected == 16
    assert results[0].passed is False


def test_probe_reports_facts_from_the_booted_node_type(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    from cloud_conversations.validator.probe import probe_host

    run_id, ctx = _reserved(owner_a, bm_requirements, bm_rubric, fake_adapter)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    fake_adapter.wait_instance_active(BM_SITE, ctx.outputs["instance_id"], 1.0)
    ctx.outputs.update(assign_ip(ctx, {"step_id": "s5"}))

    facts = probe_host(fake_adapter, BM_SITE, ctx.outputs["host"], "cc", 5.0)
    assert facts["cpu_count"] == 48  # from the FAKE@BM catalog
    assert facts["ssh_reachable"] is True


def test_probe_reports_kvm_flavor_facts(owner_a, kvm_requirements, kvm_rubric, fake_adapter):
    from cloud_conversations.validator.probe import probe_host

    run_id, ctx = _reserved(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    fake_adapter.wait_instance_active(KVM_SITE, ctx.outputs["instance_id"], 1.0)
    ctx.outputs.update(assign_ip(ctx, {"step_id": "s5"}))

    facts = probe_host(fake_adapter, KVM_SITE, ctx.outputs["host"], "cc", 5.0)
    assert facts["cpu_count"] == 4  # m1.large


def test_probe_on_an_unreachable_host_reports_it(fake_adapter):
    from cloud_conversations.validator.probe import probe_host

    assert probe_host(fake_adapter, BM_SITE, "203.0.113.1", "cc", 5.0) == {"ssh_reachable": False}


def test_probe_does_not_swallow_a_bug(fake_adapter, monkeypatch):
    from cloud_conversations.validator.probe import probe_host

    def explode(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(fake_adapter, "ssh_exec", explode)
    with pytest.raises(RuntimeError, match="kaboom"):
        probe_host(fake_adapter, BM_SITE, "203.0.113.1", "cc", 5.0)


def test_boot_attaches_a_security_group_only_where_enforced(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    run_id, ctx = _reserved(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    inst = fake_adapter.instances[ctx.outputs["instance_id"]]
    assert inst.security_groups == [f"cc-{run_id}"]


def test_boot_on_bare_metal_attaches_none(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id, ctx = _reserved(owner_a, bm_requirements, bm_rubric, fake_adapter, BM_SITE)
    ctx.outputs.update(boot(ctx, {"step_id": "s4", "name": f"cc-{run_id}"}))
    inst = fake_adapter.instances[ctx.outputs["instance_id"]]
    assert inst.security_groups == []


def test_kvm_ssh_is_refused_without_a_security_group(
    owner_a, kvm_requirements, kvm_rubric, fake_adapter
):
    """KVM@TACC drops port 22 without a group admitting it."""
    from cloud_conversations.adapter.errors import TransientError

    run_id, ctx = _reserved(owner_a, kvm_requirements, kvm_rubric, fake_adapter, KVM_SITE)
    inst = fake_adapter.launch_instance(
        KVM_SITE,
        image_id=ctx.outputs["image_id"],
        keypair_name="cc-keypair",
        name=f"cc-{run_id}",
        flavor=ctx.outputs["reserved_flavor_id"],
        security_groups=None,
        run_id=run_id,
        idempotency_key="no-sg",
    )
    fake_adapter.wait_instance_active(KVM_SITE, inst.instance_id, 1.0)
    fake_adapter.attach_floating_ip(KVM_SITE, inst.instance_id, ctx.outputs["reserved_fip"])
    with pytest.raises(TransientError, match="admits port 22"):
        fake_adapter.ssh_exec(KVM_SITE, ctx.outputs["reserved_fip"], "cc", "true", 5.0)


def test_the_dns_check_resolves_the_site_s_own_probe_host(fake_adapter, monkeypatch):
    from cloud_conversations.adapter.protocol import ExecResult
    from cloud_conversations.validator.probe import probe_host

    site = fake_adapter.capabilities().sites[0]
    site.dns_probe_host = "resolver.internal"
    seen: list[str] = []

    def capture(site_name, host, user, command, timeout_s):
        seen.append(command)
        return ExecResult(stdout="dns_resolves=true", stderr="", exit_code=0)

    monkeypatch.setattr(fake_adapter, "ssh_exec", capture)
    facts = probe_host(fake_adapter, site.site, "10.0.0.1", "cc", 5.0)

    assert facts["dns_resolves"] is True
    assert "getent hosts resolver.internal" in seen[0]
    assert "example.com" not in seen[0]


def test_the_fake_answers_the_probe_the_real_validator_would_send():
    from cloud_conversations.validator.probe import build_probe_script

    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    site = adapter.capabilities().site(BM_SITE)
    assert site is not None
    adapter.instances["i1"] = FakeInstance(
        instance_id="i1", name="cc-x", site=BM_SITE, image_id="img", keypair_name="k",
        flavor_id="baremetal", state="ACTIVE", floating_ip="10.0.0.9",
        node_type="compute_skylake", run_id=None,
    )
    result = adapter.ssh_exec(
        BM_SITE, "10.0.0.9", "cc", build_probe_script(site.dns_probe_host), timeout_s=1.0
    )
    assert result.exit_code == 0, result.stderr
    assert "cpu_arch=" in result.stdout


def test_the_fake_rejects_a_command_it_does_not_know():
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    adapter.instances["i1"] = FakeInstance(
        instance_id="i1", name="cc-x", site=BM_SITE, image_id="img", keypair_name="k",
        flavor_id="baremetal", state="ACTIVE", floating_ip="10.0.0.9",
        node_type="compute_skylake", run_id=None,
    )
    result = adapter.ssh_exec(BM_SITE, "10.0.0.9", "cc", "rm -rf /", timeout_s=1.0)
    assert result.exit_code == 1
    assert "unrecognised" in result.stderr


def test_reserve_leases_the_window_the_description_asked_for(
    owner_a, bm_requirements, bm_rubric, one_node
):
    """The one skylake node is busy now and free tomorrow."""
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    one_node.create_lease(
        BM_SITE, duration_hours=2.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="occupied-now",
    )
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    ctx = _ctx(run_id, owner_a, bm_requirements, one_node)

    out = reserve(ctx, {"step_id": "s1", "node_type": "compute_skylake",
                        "start_at": tomorrow.isoformat()})

    assert one_node.leases[out["lease_id"]].start == tomorrow


def test_reserve_waits_for_a_lease_to_reach_a_start_that_is_later(
    monkeypatch, owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    later = datetime.now(timezone.utc) + timedelta(minutes=30)
    waited: list[float] = []
    real = fake_adapter.wait_lease_active

    def spy(site, lease_id, timeout_s, on_poll=None):
        waited.append(timeout_s)
        return real(site, lease_id, timeout_s, on_poll)

    monkeypatch.setattr(fake_adapter, "wait_lease_active", spy)
    ctx = _ctx(run_id, owner_a, bm_requirements, fake_adapter)

    reserve(ctx, {"step_id": "s1", "node_type": "compute_skylake",
                  "start_at": later.isoformat()})

    assert waited == [pytest.approx(1800 + FAST.defaults.lease_active_s, abs=10)]


def test_reserve_without_a_start_takes_the_next_free_window(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    ctx = _ctx(run_id, owner_a, bm_requirements, fake_adapter)

    out = reserve(ctx, {"step_id": "s1", "node_type": "compute_skylake",
                        "start_at": None})

    started = fake_adapter.leases[out["lease_id"]].start
    assert started < datetime.now(timezone.utc) + timedelta(minutes=5)


def test_reserve_checks_availability_in_the_window_it_will_lease(
    owner_a, bm_requirements, bm_rubric, one_node
):
    """The one skylake node is busy tomorrow and free now."""
    run_id = _run(owner_a, bm_requirements, bm_rubric)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    one_node.create_lease(
        BM_SITE, duration_hours=2.0, start=tomorrow, node_type="compute_skylake",
        run_id=run_id, idempotency_key="occupied-tomorrow",
    )
    ctx = _ctx(run_id, owner_a, bm_requirements, one_node)

    with pytest.raises(StepError) as exc:
        reserve(ctx, {"step_id": "s1", "node_type": "compute_skylake",
                      "start_at": tomorrow.isoformat()})
    assert exc.value.failure_kind == "plan"

    out = reserve(ctx, {"step_id": "s2", "node_type": "compute_skylake",
                        "start_at": None})
    assert out["lease_id"]


def test_contains_matches_a_substring_anywhere():
    from cloud_conversations.validator.evaluate import compare

    lspci = "NVIDIA Corporation GP100GL [Tesla P100 PCIe 16GB]"
    assert compare("contains", "GP100GL", lspci)
    assert not compare("prefix", "GP100GL", lspci)
    assert not compare("contains", "MI100", lspci)
