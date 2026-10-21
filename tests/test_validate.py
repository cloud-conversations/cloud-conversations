"""What the model is allowed to ask for, and allowed to answer with."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from cloud_conversations.agent.catalog import Catalog, get_catalog
from cloud_conversations.agent.contract import ProposedDescription
from cloud_conversations.agent.validate import to_requirements, validate_description, validate_query
from cloud_conversations.schema.description import Check, Requirement
from cloud_conversations.schema.search import Candidate, Candidates, NodeQuery
from tests.conftest import BM_SITE, FAST, KVM_SITE, PUBLISHED_CONFIG, VMS, logged_out

SOON = datetime.now(timezone.utc) + timedelta(days=1)


@pytest.fixture()
def catalog(fake_adapter):
    return get_catalog(logged_out(*fake_adapter.capabilities().sites), FAST)


def _query(**over) -> NodeQuery:
    return NodeQuery(**{
        "reserve_by": "node_type", "site": BM_SITE, "node_type": "compute_skylake",
        "duration_hours": 2, "intent": "a skylake node", **over,
    })


def _found(compute: str = "compute_skylake", site: str = BM_SITE, free: int = 3,
           start: datetime | None = None, hours: int = 2, **over) -> Candidates:
    """One search result: `compute` free at `site` for a window."""
    begin = start or datetime.now(timezone.utc)
    return Candidates(
        query=_query(site=site, start_at=start, duration_hours=hours, **over),
        start=begin,
        end=begin + timedelta(hours=hours),
        items=[Candidate(site=site, compute=compute, free=free)],
    )


def _limited(catalog: Catalog, **limits: object) -> Catalog:
    return catalog.model_copy(
        update={"sites": [s.model_copy(update=limits) for s in catalog.sites]}
    )


def _answer(**over) -> ProposedDescription:
    return ProposedDescription(**{
        "site": BM_SITE,
        "duration_hours": 2,
        "items": [
            Requirement(key="node_type", value="compute_skylake", source="stated"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
        ],
        "rubric": [
            Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
            Check(id="os", kind="os_release", expected="Ubuntu 24.04",
                  comparator="prefix", from_requirement="os_release"),
        ],
        **over,
    })


def test_an_invented_field_is_refused():
    with pytest.raises(ValidationError):
        ProposedDescription.model_validate(_answer().model_dump() | {"ssh_key_ref": "mine"})
    with pytest.raises(ValidationError):
        NodeQuery.model_validate(_query().model_dump() | {"sneaky": True})


def test_a_usable_query_is_approved(catalog):
    assert validate_query(_query(), catalog, []) == []


def test_a_query_naming_an_unknown_site(catalog):
    reasons = validate_query(_query(site="CHI@NOWHERE"), catalog, [])
    assert any("unknown site" in r for r in reasons)


def test_a_query_naming_an_unknown_node_type(catalog):
    reasons = validate_query(_query(node_type="compute_vapourware"), catalog, [])
    assert any("unknown node_type" in r for r in reasons)


def test_a_query_naming_an_unknown_arch(catalog):
    reasons = validate_query(_query(node_type=None, arch="risc-v"), catalog, [])
    assert any("unknown arch" in r for r in reasons)


def test_a_query_over_the_lease_cap(catalog):
    capped = _limited(catalog, max_lease_hours={"node_type": 24, "flavor": 24})
    reasons = validate_query(_query(duration_hours=25), capped, [])
    assert any("outside 1 to 24" in r for r in reasons)


def test_a_query_starting_in_the_past(catalog):
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    reasons = validate_query(_query(start_at=past), catalog, [])
    assert any("in the past" in r for r in reasons)


def test_a_query_starting_beyond_the_lead_time(catalog):
    far = datetime.now(timezone.utc) + timedelta(days=10)
    reasons = validate_query(_query(start_at=far), _limited(catalog, max_lead_days=7), [])
    assert any("more than 7 days ahead" in r for r in reasons)


def test_an_answer_starting_beyond_its_sites_lead_time(catalog):
    short = _limited(catalog, max_lead_days=3)
    start = datetime.now(timezone.utc) + timedelta(days=5)
    reasons = validate_description(
        _answer(start_at=start), short, [_found(start=start)]
    )
    assert any("more than 3 days ahead" in r for r in reasons)


def test_a_query_that_narrows_nothing(catalog):
    bare = NodeQuery(reserve_by="node_type", duration_hours=2, intent="anything at all")
    reasons = validate_query(bare, catalog, [])
    assert any("must narrow" in r for r in reasons)


def test_a_query_already_run_is_refused(catalog):
    first = _query()
    assert validate_query(first, catalog, [first]) == [
        "this search has already been run; widen it or move the window"
    ]


def test_rewording_the_intent_does_not_buy_another_round(catalog):
    first = _query(intent="a skylake node")
    again = _query(intent="I would like a skylake node please")
    reasons = validate_query(again, catalog, [first])
    assert any("already been run" in r for r in reasons)


def test_moving_the_window_is_a_new_query(catalog):
    first = _query()
    later = _query(start_at=SOON)
    assert validate_query(later, catalog, [first]) == []


def test_a_flavor_query_against_a_bare_metal_site(catalog):
    query = NodeQuery(reserve_by="flavor", site=BM_SITE, flavor="m1.large", duration_hours=2,
                      intent="a VM")
    assert validate_query(query, catalog, []) == [
        "FAKE@BM takes node_type, not flavor"
    ]


def test_a_node_type_query_against_a_kvm_site(catalog):
    query = NodeQuery(reserve_by="node_type", site=KVM_SITE, node_type="compute_skylake",
                      duration_hours=2, intent="a node")
    assert validate_query(query, catalog, []) == [
        "FAKE@KVM takes flavor, not node_type"
    ]


def test_a_flavor_query_with_a_bare_metal_filter(catalog):
    query = NodeQuery(reserve_by="flavor", site=KVM_SITE, gpu=True, infiniband=True,
                      duration_hours=2, intent="a VM on infiniband")
    assert validate_query(query, catalog, []) == [
        "a search by flavor takes flavor, gpu, min_ram_gb, not infiniband"
    ]


def test_a_query_naming_an_unknown_flavor(catalog):
    query = NodeQuery(reserve_by="flavor", site=KVM_SITE, flavor="m1.enormous", duration_hours=2,
                      intent="a big VM")
    reasons = validate_query(query, catalog, [])
    assert any("unknown flavor" in r for r in reasons)


def test_a_usable_flavor_query_is_approved(catalog):
    query = NodeQuery(reserve_by="flavor", site=KVM_SITE, flavor="m1.large", duration_hours=2,
                      intent="a medium VM")
    assert validate_query(query, catalog, []) == []


def test_an_answer_for_hardware_a_search_found_free_is_approved(catalog):
    assert validate_description(
        _answer(), catalog, [_found()]
    ) == []


def test_an_answer_naming_an_unknown_site(catalog):
    reasons = validate_description(_answer(site="CHI@NOWHERE"), catalog, [_found()])
    assert any("unknown site" in r for r in reasons)


def test_an_answer_whose_compute_is_not_in_the_catalog(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_vapourware", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("unknown node_type" in r for r in reasons)


def test_an_answer_using_the_wrong_compute_key_for_the_site(catalog):
    answer = _answer(site=KVM_SITE)
    reasons = validate_description(answer, catalog, [_found()])
    assert "FAKE@KVM takes flavor, not node_type" in reasons


def test_an_answer_with_no_image_or_os_release(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("image_name or an os_release" in r for r in reasons)


def test_an_answer_asking_for_two_nodes(catalog):
    answer = _answer(
        rubric=[],
        items=[
            Requirement(key="node_type", value="compute_skylake", source="stated"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
            Requirement(key="count", value=2, source="stated"),
        ],
    )
    reasons = validate_description(answer, catalog, [_found()])

    assert any("count=2 is not supported" in r for r in reasons)
    assert any("at least one check" in r for r in reasons)


def test_an_answer_over_the_lease_cap(catalog):
    capped = _limited(catalog, max_lease_hours={"node_type": 24, "flavor": 24})
    reasons = validate_description(_answer(duration_hours=25), capped, [_found(hours=25)])
    assert any("outside 1 to 24" in r for r in reasons)


def test_an_answer_whose_check_reads_an_absent_requirement(catalog):
    answer = _answer(rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="mem", kind="mem_gb", expected=64, comparator="gte",
              from_requirement="mem_gb"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("not a requirement here" in r for r in reasons)


def test_an_answer_whose_rubric_never_checks_ssh(catalog):
    answer = _answer(rubric=[
        Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("ssh_reachable" in r for r in reasons)


def test_every_requirement_key_is_actionable_or_verifiable():
    from typing import get_args

    from cloud_conversations.schema.description import PLANNER_KEYS, VERIFIABLE_KEYS, RequirementKey

    assert set(get_args(RequirementKey)) - PLANNER_KEYS - VERIFIABLE_KEYS == set()


def test_an_answer_for_hardware_a_search_found_busy(catalog):
    reasons = validate_description(_answer(), catalog, [_found(free=0)])
    assert any("no search found compute_skylake" in r for r in reasons)


def test_an_answer_for_a_window_no_search_asked_about(catalog):
    tuesday = datetime.now(timezone.utc) + timedelta(days=1)
    wednesday = tuesday + timedelta(days=1)
    reasons = validate_description(_answer(start_at=wednesday), catalog,
                                   [_found(start=tuesday)])
    assert any("was found free for" in r for r in reasons)


def test_an_answer_with_no_start_needs_a_search_with_no_start(catalog):
    reasons = validate_description(_answer(), catalog, [_found(start=SOON)])
    assert any("was found free for" in r for r in reasons)
    assert any(
        "give start_at to answer for one of those, or search with it omitted" in r
        for r in reasons
    )

    assert validate_description(
        _answer(start_at=SOON), catalog, [_found(start=SOON)]
    ) == []


def test_an_answer_for_a_shorter_lease_than_the_search(catalog):
    reasons = validate_description(_answer(duration_hours=4), catalog, [_found(hours=2)])
    assert any("was found free for" in r for r in reasons)


def test_hardware_found_by_the_second_of_two_searches_is_usable(catalog):
    seen = [_found(compute="compute_haswell", free=0), _found()]
    assert validate_description(_answer(), catalog, seen) == []


def test_a_kvm_answer_matches_a_flavor_candidate(catalog):
    answer = _answer(site=KVM_SITE, items=[
        Requirement(key="flavor", value="m1.large", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
    ])
    begin = datetime.now(timezone.utc)
    found = Candidates(
        query=NodeQuery(reserve_by="flavor", site=KVM_SITE, flavor="m1.large", duration_hours=2,
                        intent="a medium VM"),
        start=begin,
        end=begin + timedelta(hours=2),
        items=[Candidate(site=KVM_SITE, compute="m1.large", free=1)],
    )
    assert validate_description(answer, catalog, [found]) == []


def test_to_requirements_keeps_the_window(catalog):
    requirements = to_requirements(_answer(start_at=SOON), catalog)

    assert requirements.site == BM_SITE
    assert requirements.start_at == SOON
    assert requirements.duration_hours == 2
    assert requirements.value_of("node_type") == "compute_skylake"


def test_to_requirements_keeps_where_the_length_and_start_came_from(catalog):
    requirements = to_requirements(
        _answer(duration_source="stated", start_source="inferred", start_at=SOON),
        catalog,
    )

    assert requirements.duration_source == "stated"
    assert requirements.start_source == "inferred"


def test_an_os_release_that_names_no_image_is_refused(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="os_release", value="24", source="stated"),
    ])
    reasons = validate_description(answer, catalog, [_found()])

    assert any("names no distribution and version" in r for r in reasons)
    assert any("Ubuntu 24.04" in r for r in reasons), "the reason shows the form"


def test_an_os_release_naming_a_distro_and_version_is_accepted(catalog):
    for release in ("Ubuntu 22.04.4 LTS", "CentOS 8"):
        answer = _answer(items=[
            Requirement(key="node_type", value="compute_skylake", source="stated"),
            Requirement(key="os_release", value=release, source="stated"),
        ])
        reasons = validate_description(answer, catalog, [_found()])
        assert not any("names no distribution" in r for r in reasons), release


def test_an_exact_image_name_needs_no_os_release(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu24.04", source="stated"),
    ], rubric=[Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq")])
    assert validate_description(answer, catalog, [_found()]) == []


def test_a_check_that_verifies_no_requirement_takes_no_from_requirement(catalog):
    answer = _answer(rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq",
              from_requirement="node_type"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("verifies no requirement" in r for r in reasons)


def test_a_rubric_that_ignores_a_measurable_requirement_is_refused(catalog):
    answer = _answer(rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("does not verify the os_release requirement" in r for r in reasons)


def test_a_check_may_read_a_requirement_of_another_name(catalog):
    answer = _answer(rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="cpus", kind="cpu_count", expected=24, comparator="gte",
              from_requirement="node_type"),
    ])
    assert validate_description(answer, catalog, [_found()]) == []


@pytest.fixture()
def kvm_catalog():
    """A KVM site with one plain flavor and one GPU flavor, on Chameleon's limits."""
    from cloud_conversations.adapter.protocol import Flavor, SiteDescriptor

    return get_catalog(
        logged_out(SiteDescriptor(
            site="KVM@TACC", auth_url="https://kvm.tacc.invalid:5000/v3",
            kinds=VMS,
            flavors=[
                Flavor(flavor_id="1", name="m1.large", vcpus=4, mem_gb=8, disk_gb=40),
                Flavor(flavor_id="2", name="g1.h100.pci.1", vcpus=48, mem_gb=244,
                       disk_gb=40, gpu=True, gpu_count=1),
            ],
        )),
        PUBLISHED_CONFIG,
    )


def _kvm_answer(flavor: str, hours: int, gpus: int = 0) -> ProposedDescription:
    answer = ProposedDescription(
        site="KVM@TACC", duration_hours=hours,
        items=[
            Requirement(key="flavor", value=flavor, source="stated"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
        ],
        rubric=[
            Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
            Check(id="os", kind="os_release", expected="Ubuntu 24.04",
                  comparator="prefix", from_requirement="os_release"),
        ],
    )
    if gpus:
        answer.items.append(Requirement(key="gpu_count", value=gpus, source="inferred"))
        answer.rubric.append(Check(id="gpus", kind="gpu_count", expected=gpus,
                                   comparator="gte", from_requirement="gpu_count"))
    return answer


def _kvm_found(flavor: str, hours: int) -> Candidates:
    begin = datetime.now(timezone.utc)
    return Candidates(
        query=NodeQuery(reserve_by="flavor", site="KVM@TACC", flavor=flavor, duration_hours=hours,
                        intent="a VM"),
        start=begin, end=begin + timedelta(hours=hours),
        items=[Candidate(site="KVM@TACC", compute=flavor, free=4)],
    )


def test_a_plain_vm_may_be_leased_for_six_months(kvm_catalog):
    reasons = validate_description(
        _kvm_answer("m1.large", 4320), kvm_catalog, [_kvm_found("m1.large", 4320)]
    )
    assert reasons == []


def test_a_gpu_vm_is_capped_at_seven_days(kvm_catalog):
    capped = _limited(kvm_catalog, max_gpu_lease_hours={"flavor": 168})
    reasons = validate_description(
        _kvm_answer("g1.h100.pci.1", 4320), capped,
        [_kvm_found("g1.h100.pci.1", 4320)])
    assert any("outside 1 to 168" in r for r in reasons)

    assert validate_description(
        _kvm_answer("g1.h100.pci.1", 168, gpus=1), capped,
        [_kvm_found("g1.h100.pci.1", 168)]) == []


def test_a_gpu_vm_records_its_gpu_by_count_not_model(kvm_catalog):
    answer = _kvm_answer("g1.h100.pci.1", 4)
    answer.items.append(Requirement(key="gpu_model", value="H100", source="stated"))
    answer.rubric.append(Check(id="gpu", kind="gpu_model", expected="H100",
                               comparator="contains", from_requirement="gpu_model"))

    reasons = validate_description(
        answer, kvm_catalog, [_kvm_found("g1.h100.pci.1", 4)]
    )

    assert reasons == [
        "a flavor names no GPU model; give gpu_count, not gpu_model",
        "g1.h100.pci.1 holds 1 GPU(s); give a gpu_count requirement of 1",
    ]


def test_a_seven_day_bare_metal_lease_is_allowed(catalog):
    capped = _limited(catalog, max_lease_hours={"node_type": 168, "flavor": 168})
    reasons = validate_description(
        _answer(duration_hours=168), capped, [_found(hours=168)]
    )
    assert reasons == []


def test_a_search_for_a_gpu_vm_is_capped_at_seven_days(kvm_catalog):
    query = NodeQuery(reserve_by="flavor", site="KVM@TACC", flavor="g1.h100.pci.1",
                      duration_hours=4320, intent="a GPU VM for six months")
    capped = _limited(kvm_catalog, max_gpu_lease_hours={"flavor": 168})
    reasons = validate_query(query, capped, [])
    assert any("outside 1 to 168" in r for r in reasons)


def test_an_image_the_site_does_not_have_is_refused(fake_adapter):
    with_images = get_catalog(fake_adapter, FAST)
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="os_release", value="Ubuntu 26.04", source="stated"),
    ])
    reasons = validate_description(answer, with_images, [_found()])

    assert any("no image named CC-Ubuntu26.04" in r for r in reasons)
    assert any("CC-Ubuntu24.04" in r for r in reasons), "it names what there is"


def test_an_image_the_site_does_have_is_accepted(fake_adapter):
    with_images = get_catalog(fake_adapter, FAST)
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu22.04", source="stated"),
        Requirement(key="os_release", value="Ubuntu 22.04", source="stated"),
    ], rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="os", kind="os_release", expected="Ubuntu 22.04",
              comparator="prefix", from_requirement="os_release"),
    ])
    assert validate_description(answer, with_images, [_found()]) == []


def test_an_os_release_alone_is_sent_back_with_the_image_to_name(fake_adapter):
    with_images = get_catalog(fake_adapter, FAST)
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="os_release", value="Ubuntu 22.04", source="stated"),
    ])
    reasons = validate_description(answer, with_images, [_found()])

    assert any("give image_name 'CC-Ubuntu22.04'" in r for r in reasons)


@pytest.fixture()
def arm_catalog(fake_adapter):
    """A bare-metal site with an aarch64 node type and both image builds."""
    from cloud_conversations.adapter.protocol import Image, NodeType

    site = fake_adapter._sites[BM_SITE]  # noqa: SLF001
    site.descriptor.node_types = [
        NodeType(name="compute_arm64", cpu_arch="aarch64", cpu_count=48, mem_gb=32,
                 disk_gb=512),
        NodeType(name="compute_skylake", cpu_arch="x86_64", cpu_count=48, mem_gb=192,
                 disk_gb=240),
    ]
    site.images = [
        Image(image_id="x86-id", name="CC-Ubuntu24.04"),
        Image(image_id="arm-id", name="CC-Ubuntu24.04-ARM64"),
    ]
    return get_catalog(fake_adapter, FAST)


def _arm_answer(**over) -> ProposedDescription:
    return ProposedDescription(**{
        "site": BM_SITE, "duration_hours": 2,
        "items": [
            Requirement(key="node_type", value="compute_arm64", source="stated"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="inferred"),
        ],
        "rubric": [
            Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
            Check(id="os", kind="os_release", expected="Ubuntu 24.04",
                  comparator="prefix", from_requirement="os_release"),
        ],
        **over,
    })


def test_naming_the_arm_image_explicitly_is_accepted(arm_catalog):
    answer = _arm_answer(items=[
        Requirement(key="node_type", value="compute_arm64", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu24.04-ARM64", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="inferred"),
    ])
    assert validate_description(
        answer, arm_catalog, [_found(compute="compute_arm64")]
    ) == []


def test_an_x86_node_still_takes_the_plain_image(arm_catalog):
    answer = _arm_answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu24.04", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="inferred"),
    ])
    assert validate_description(
        answer, arm_catalog, [_found()]
    ) == []


def test_the_plain_image_on_an_arm_node_is_refused(arm_catalog):
    answer = _arm_answer(items=[
        Requirement(key="node_type", value="compute_arm64", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu24.04", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="inferred"),
    ])
    reasons = validate_description(
        answer, arm_catalog, [_found(compute="compute_arm64")]
    )

    assert any("give image_name 'CC-Ubuntu24.04-ARM64'" in r for r in reasons)
    assert any("aarch64" in r for r in reasons)


def test_the_arm_image_on_an_x86_node_is_refused(arm_catalog):
    answer = _arm_answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="image_name", value="CC-Ubuntu24.04-ARM64", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="inferred"),
    ])
    reasons = validate_description(
        answer, arm_catalog, [_found()]
    )

    assert any("no image named CC-Ubuntu24.04-ARM64 built for x86_64" in r for r in reasons)


def test_a_stated_arch_must_match_the_node_type(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="cpu_arch", value="aarch64", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
    ], rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="arch", kind="cpu_arch", expected="aarch64", comparator="eq",
              from_requirement="cpu_arch"),
    ])
    reasons = validate_description(answer, catalog, [_found()])
    assert any("does not match compute_skylake, which is x86_64" in r for r in reasons)


def test_the_arch_the_node_type_has_is_accepted(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="cpu_arch", value="x86_64", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
    ], rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq",
              from_requirement="cpu_arch"),
    ])
    assert validate_description(answer, catalog, [_found()]) == []


def test_a_gpu_request_can_be_recorded_and_verified(catalog):
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="gpu_model", value="P100", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
    ], rubric=[
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="gpu", kind="gpu_model", expected="P100", comparator="contains",
              from_requirement="gpu_model"),
    ])
    assert validate_description(answer, catalog, [_found()]) == []


def test_to_requirements_records_the_image_and_whether_it_is_supported(fake_adapter):
    with_images = get_catalog(fake_adapter, FAST)

    image = to_requirements(_answer(), with_images).image

    assert image is not None
    assert (image.name, image.supported) == ("CC-Ubuntu24.04", True)


def test_a_refused_image_names_only_the_supported_images(fake_adapter):
    from cloud_conversations.schema.images import Image

    fake_adapter._sites[BM_SITE].images.append(  # noqa: SLF001
        Image(image_id="mine", name="stranger-image")
    )
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="image_name", value="CC-Nothing", source="stated"),
    ])

    reasons = validate_description(answer, get_catalog(fake_adapter, FAST), [_found()])

    assert (
        "no image named CC-Nothing built for x86_64 at FAKE@BM; it supports "
        "CC-Ubuntu22.04, CC-Ubuntu24.04, "
        "and 1 other images (search_images to look)"
    ) in reasons


def test_an_image_the_user_names_exactly_is_accepted_though_unsupported(fake_adapter):
    from cloud_conversations.schema.images import Image

    fake_adapter._sites[BM_SITE].images.append(  # noqa: SLF001
        Image(image_id="mine", name="stranger-image")
    )
    answer = _answer(items=[
        Requirement(key="node_type", value="compute_skylake", source="stated"),
        Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
        Requirement(key="image_name", value="stranger-image", source="stated"),
    ])

    assert validate_description(answer, get_catalog(fake_adapter, FAST), [_found()]) == []


def test_an_image_search_already_run_is_refused(fake_adapter):
    from cloud_conversations.agent.contract import ImageSearchStep
    from cloud_conversations.agent.validate import validate_image_search

    step = ImageSearchStep(action="search_images", site=BM_SITE, text="CUDA")

    assert validate_image_search(step, get_catalog(fake_adapter, FAST), [(BM_SITE, "cuda")]) == [
        "this image search has already been run; search for something else"
    ]
