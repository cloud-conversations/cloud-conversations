"""The reference API client, against recorded responses."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cloud_conversations.adapter.chameleon.catalog import CatalogError
from cloud_conversations.adapter.chameleon.search import (
    flavor_availability,
    search_flavors,
    search_nodes,
)
from cloud_conversations.adapter.protocol import Flavor
from cloud_conversations.schema.search import NodeQuery
from tests.loopback import node, page, serving

_FIXTURES = Path(__file__).parent / "fixtures"

# The three sites the recorded responses cover.
UID_FOR = {"CHI@TACC": "tacc", "CHI@UC": "uc", "KVM@TACC": "kvm"}

START = datetime(2026, 9, 3, 17, 25, 1, tzinfo=timezone.utc)


def _recorded(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def test_a_search_builds_the_documented_query():
    query = NodeQuery(
        reserve_by="node_type",
        site="CHI@UC",
        node_type="compute_cascadelake_r",
        arch="x86_64",
        gpu=False,
        infiniband=True,
        min_ram_gb=128,
        start_at=START,
        duration_hours=4,
        intent="a big cascade lake node tomorrow afternoon",
    )
    with serving(page()) as (root, asked):
        search_nodes(root, query, UID_FOR, 5.0)

    path, params = asked[0]
    assert path == "/nodes/search"
    assert params == {
        "site_id": ["uc"],
        "node_type": ["compute_cascadelake_r"],
        "arch": ["x86_64"],
        "gpu": ["false"],
        "infiniband": ["true"],
        "min_ram": ["137438953472"],
        "start": ["2026-09-03T17:25:01+00:00"],
        "end": ["2026-09-03T21:25:01+00:00"],
        "offset": ["0"],
        "limit": ["500"],
    }


def test_a_search_without_a_window_runs_from_now():
    query = NodeQuery(reserve_by="node_type", gpu=True, duration_hours=2, intent="a GPU now")
    before = datetime.now(timezone.utc)
    with serving(page()) as (root, asked):
        candidates = search_nodes(root, query, UID_FOR, 5.0)

    assert before <= candidates.start <= datetime.now(timezone.utc)
    assert candidates.end - candidates.start == timedelta(hours=2)
    assert "site_id" not in asked[0][1], "no site means every site"


def test_only_available_nodes_are_counted():
    with serving(
        page(
            node("compute_zen3", "uc", "available"),
            node("compute_zen3", "uc", "available"),
            node("compute_zen3", "uc", "reserved"),
            node("compute_zen3", "uc", "maintenance"),
            node("compute_zen3", "uc", "unknown"),
        )
    ) as (root, _):
        candidates = search_nodes(
            root,
            NodeQuery(reserve_by="node_type", min_ram_gb=64, duration_hours=2,
                      intent="anything roomy"),
            UID_FOR,
            5.0,
        )

    assert [(c.site, c.compute, c.free) for c in candidates.items] == [
        ("CHI@UC", "compute_zen3", 2)
    ]


def test_a_vm_hypervisor_is_not_free_bare_metal():
    with serving(
        page(
            node("compute_haswell", "tacc", "available") | {"lease_mode": "flavor"},
            node("compute_zen3", "tacc", "available") | {"lease_mode": "baremetal"},
        )
    ) as (root, _):
        candidates = search_nodes(
            root,
            NodeQuery(reserve_by="node_type", min_ram_gb=64, duration_hours=2,
                      intent="anything roomy"),
            UID_FOR,
            5.0,
        )

    assert [(c.site, c.compute) for c in candidates.items] == [("CHI@TACC", "compute_zen3")]


def test_a_recorded_search_groups_and_counts_by_site_and_node_type():
    with serving(_recorded("nodes_search_gpu.json")) as (root, _):
        candidates = search_nodes(
            root,
            NodeQuery(reserve_by="node_type", gpu=True, start_at=START, duration_hours=4,
                      intent="a GPU node"),
            UID_FOR,
            5.0,
        )

    counts = {(c.site, c.compute): c.free for c in candidates.items}
    assert counts == {
        ("CHI@TACC", "compute_gigaio"): 1,
        ("CHI@TACC", "gpu_k80"): 2,
        ("CHI@TACC", "gpu_m40"): 2,
        ("CHI@TACC", "gpu_mi100"): 4,
        ("CHI@TACC", "gpu_p100"): 8,
        ("CHI@TACC", "gpu_p100_v100"): 1,
        ("CHI@UC", "gpu_rtx_6000"): 7,
        ("CHI@UC", "gpu_v100"): 1,
    }, "9 rtx_6000 came back; 2 are in maintenance"
    assert ("KVM@TACC", "gpu_h100") not in counts, "KVM reports unknown, not available"
    assert ("CHI@TACC", "compute_liqid") not in counts, "every liqid node is in maintenance"


def test_a_truncated_page_is_followed():
    first = {"total": 3, "offset": 0, "items": [
        node("compute_zen3", "uc", "available"),
        node("compute_zen3", "uc", "available"),
    ]}
    second = {"total": 3, "offset": 2, "items": [node("compute_zen3", "uc", "available")]}

    with serving(first, second) as (root, asked):
        candidates = search_nodes(
            root,
            NodeQuery(reserve_by="node_type", min_ram_gb=64, duration_hours=2,
                      intent="anything roomy"),
            UID_FOR,
            5.0,
        )

    assert [params["offset"] for _, params in asked] == [["0"], ["2"]]
    assert candidates.items[0].free == 3


def test_an_unreachable_api_is_not_an_empty_result():
    with serving(page()) as (root, _):
        pass

    with pytest.raises(CatalogError):
        search_nodes(
            root,
            NodeQuery(reserve_by="node_type", gpu=True, duration_hours=2, intent="a GPU node"),
            UID_FOR,
            5.0,
        )


def test_an_unknown_site_is_not_a_silent_empty_search():
    with serving(page()) as (root, asked), pytest.raises(CatalogError, match="CHI@NOWHERE"):
        search_nodes(
            root,
            NodeQuery(reserve_by="node_type", site="CHI@NOWHERE", gpu=True, duration_hours=2,
                      intent="a GPU"),
            UID_FOR,
            5.0,
        )
    assert asked == [], "nothing was asked of the API"


def test_a_flavor_is_free_only_across_every_segment():
    timeline = {
        "flavor_id": "m1.xlarge",
        "site_id": "kvm",
        "availability": [
            {"start": "2026-09-03T17:25:00", "end": "2026-09-03T19:00:00", "available": 5},
            {"start": "2026-09-03T19:00:00", "end": "2026-09-03T21:25:00", "available": 0},
        ],
    }
    with serving(timeline) as (root, asked):
        assert flavor_availability(
            root, "kvm", "m1.xlarge", START, START + timedelta(hours=4), 5.0
        ) == 0

    path, params = asked[0]
    assert path == "/sites/kvm/flavors/m1.xlarge/availability"
    assert params == {
        "start_date": ["2026-09-03T17:25:01+00:00"],
        "end_date": ["2026-09-03T21:25:01+00:00"],
    }


def test_a_flavor_free_throughout_reports_the_tightest_segment():
    timeline = {
        "flavor_id": "m1.xlarge",
        "site_id": "kvm",
        "availability": [{"available": 549}, {"available": 3}, {"available": 550}],
    }
    with serving(timeline) as (root, _):
        assert flavor_availability(
            root, "kvm", "m1.xlarge", START, START + timedelta(hours=4), 5.0
        ) == 3


def test_a_flavor_query_does_not_go_to_the_node_search():
    with serving(page()) as (root, asked), pytest.raises(CatalogError, match="search_flavors"):
        search_nodes(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", flavor="m1.xlarge", duration_hours=2,
                      intent="a medium VM"),
            UID_FOR,
            5.0,
        )
    assert asked == [], "nothing was asked of the API"


# KVM@TACC's flavors.
_KVM_FLAVORS = [
    Flavor(flavor_id="g1.h100.pci.1", name="g1.h100.pci.1", vcpus=48,
           mem_gb=244, disk_gb=40, gpu=True),
    Flavor(flavor_id="m1.large", name="m1.large", vcpus=4, mem_gb=8, disk_gb=40),
    Flavor(flavor_id="m1.tiny", name="m1.tiny", vcpus=1, mem_gb=0, disk_gb=1),
]


def _timeline(available: int) -> dict:
    return {"availability": [{"available": available, "total": available}]}


def test_a_gpu_search_asks_only_about_the_flavors_that_hold_one():
    with serving(_timeline(4)) as (root, asked):
        found = search_flavors(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", gpu=True, duration_hours=2,
                      intent="a GPU VM"),
            {"KVM@TACC": ("kvm", _KVM_FLAVORS)},
            5.0,
        )

    assert [path for path, _ in asked] == ["/sites/kvm/flavors/g1.h100.pci.1/availability"]
    assert [(i.compute, i.free) for i in found.items] == [("g1.h100.pci.1", 4)]


def test_a_memory_search_keeps_every_flavor_that_is_big_enough():
    with serving(_timeline(7)) as (root, asked):
        found = search_flavors(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", min_ram_gb=8, duration_hours=2,
                      intent="8 GB"),
            {"KVM@TACC": ("kvm", _KVM_FLAVORS)},
            5.0,
        )

    assert [path for path, _ in asked] == [
        "/sites/kvm/flavors/g1.h100.pci.1/availability",
        "/sites/kvm/flavors/m1.large/availability",
    ], "m1.tiny holds less than a gigabyte"
    assert [i.compute for i in found.items] == ["g1.h100.pci.1", "m1.large"]


def test_a_search_matching_no_flavor_asks_nothing():
    with serving(_timeline(1)) as (root, asked):
        found = search_flavors(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", min_ram_gb=4096, duration_hours=2,
                      intent="4 TB of ram"),
            {"KVM@TACC": ("kvm", _KVM_FLAVORS)},
            5.0,
        )

    assert asked == []
    assert found.items == []


def test_a_search_naming_a_flavor_asks_about_that_one():
    with serving(_timeline(2)) as (root, asked):
        found = search_flavors(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", flavor="m1.large", duration_hours=2,
                      intent="a VM"),
            {"KVM@TACC": ("kvm", _KVM_FLAVORS)},
            5.0,
        )

    assert [path for path, _ in asked] == ["/sites/kvm/flavors/m1.large/availability"]
    assert [i.compute for i in found.items] == ["m1.large"]


def test_a_site_the_reference_api_does_not_publish_cannot_be_searched():
    with serving(_timeline(1)) as (root, asked), pytest.raises(CatalogError):
        search_flavors(
            root,
            NodeQuery(reserve_by="flavor", site="KVM@TACC", gpu=True, duration_hours=2,
                      intent="a GPU VM"),
            {"KVM@TACC": (None, _KVM_FLAVORS)},
            5.0,
        )
    assert asked == []
