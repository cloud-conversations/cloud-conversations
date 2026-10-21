"""ChameleonAdapter's published catalog: node types and flavors from the reference API."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter.chameleon import public
from cloud_conversations.adapter.chameleon.catalog import CatalogError, fetch_node_types
from cloud_conversations.config import Adapter, Config, Settings
from tests.conftest import KINDS
from tests.loopback import serving

KVM_SITE_PUBLISHED = "KVM@TACC"


def _config_at(root: str, sites: dict) -> Config:
    return Config(
        defaults=Settings(),
        adapters={"chameleon": Adapter(site_index_url=f"{root}/sites", kinds=KINDS, sites=sites)},
    )


def test_node_types_come_from_the_published_hardware_catalog():
    payload = {
        "items": [
            {
                "node_type": "gpu_a100",
                "architecture": {"platform_type": "x86_64", "smt_size": 128},
                "main_memory": {"ram_size": 549755813888},
                "storage_devices": [{"size": 960000000000}],
                "gpu": {"gpu": True, "gpu_model": "A100"},
            }
        ]
    }
    with serving(payload) as (root, _):
        sites = {"CHI@UC": {"kinds": ["baremetal"], "node_catalog_url": f"{root}/nodes"}}
        published = public(_config_at(root, sites)).published_node_types("CHI@UC")

    assert [n.name for n in published] == ["gpu_a100"]


def test_a_vm_hypervisor_is_not_a_bare_metal_node_type():
    hardware = {
        "architecture": {"platform_type": "x86_64", "smt_size": 48},
        "main_memory": {"ram_size": 137438953472},
    }
    payload = {"items": [
        {"node_type": "compute_haswell", "lease_mode": "flavor", **hardware},
        {"node_type": "compute_zen3", **hardware},
    ]}
    with serving(payload) as (root, _):
        node_types = fetch_node_types(f"{root}/nodes", 5.0)

    assert [n.name for n in node_types] == ["compute_zen3"]


def test_flavors_come_from_the_published_flavor_list():
    payload = {"items": [
        {"uid": "m1.xlarge", "name": "m1.xlarge", "vcpus": 8,
         "ram_size": 17179869184, "disk_size": 42949672960},
        {"uid": "m1.large", "name": "m1.large", "vcpus": 4,
         "ram_size": 8589934592, "disk_size": 42949672960},
    ]}
    with serving(payload) as (root, _):
        sites = {KVM_SITE_PUBLISHED: {"kinds": ["virtual"]}}
        flavors = public(_config_at(root, sites)).published_flavors(KVM_SITE_PUBLISHED)

    assert [f.name for f in flavors] == ["m1.large", "m1.xlarge"]
    assert (flavors[0].vcpus, flavors[0].mem_gb, flavors[0].disk_gb) == (4, 8, 40)


def test_the_catalog_maps_the_reference_api_schema():
    payload = {
        "items": [
            {
                "node_type": "compute_zen3",
                "architecture": {"platform_type": "x86_64", "smt_size": 256},
                "main_memory": {"ram_size": 274877906944},
                "storage_devices": [{"size": 240000000000}, {"size": 480000000000}],
                "gpu": {"gpu": False},
            },
            {"node_type": "compute_zen3", "architecture": {"smt_size": 1}},
            {
                "node_type": "gpu_a100",
                "architecture": {"platform_type": "x86_64", "smt_size": 128},
                "main_memory": {"ram_size": 549755813888},
                "storage_devices": [],
                "gpu": {"gpu": True, "gpu_model": "A100"},
            },
        ]
    }
    with serving(payload) as (root, _):
        types = fetch_node_types(f"{root}/nodes", 5.0)

    assert [t.name for t in types] == ["compute_zen3", "gpu_a100"], "one per type, sorted"
    zen = types[0]
    assert (zen.cpu_arch, zen.cpu_count, zen.mem_gb) == ("x86_64", 256, 256)
    assert zen.disk_gb == 480, "the largest device, not the sum"
    assert zen.gpu_model is None
    assert types[1].gpu_model == "A100"


def test_the_catalog_rejects_an_unusable_response():
    with serving({"unexpected": "shape"}) as (root, _), pytest.raises(CatalogError):
        fetch_node_types(f"{root}/nodes", 5.0)
