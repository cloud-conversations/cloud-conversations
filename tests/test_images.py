"""ImageRules: a testbed's image naming, read from its config table."""

from __future__ import annotations

import pytest

from cloud_conversations.schema.images import Image, ImageRules

_RULES = ImageRules(prefix="X-", arch_markers={"aarch64": ["arm"]}, default_arch="x86_64")


def test_a_release_is_named_with_the_testbeds_prefix():
    assert _RULES.name_for("Ubuntu 24.04.1 LTS") == "X-Ubuntu24.04"


def test_a_release_with_no_minor_version_is_named_by_its_major():
    assert _RULES.name_for("CentOS 8") == "X-CentOS8"


def test_an_arch_marker_names_the_architecture():
    assert _RULES.arch_of("X-Ubuntu24.04-ARM") == "aarch64"


def test_a_name_without_a_marker_is_the_default_architecture():
    assert _RULES.arch_of("X-Ubuntu24.04") == "x86_64"


_IMAGES = [
    Image(image_id="x86-id", name="X-Ubuntu24.04"),
    Image(image_id="arm-id", name="X-Ubuntu24.04-ARM"),
    Image(image_id="cuda-id", name="X-Ubuntu24.04-CUDA"),
]


@pytest.mark.parametrize(
    ("cpu_arch", "expected"),
    [
        ("x86_64", "x86-id"),
        ("aarch64", "arm-id"),
        (None, "x86-id"),
    ],
)
def test_an_os_release_resolves_to_the_build_for_the_architecture(cpu_arch, expected):
    chosen = _RULES.choose(_IMAGES, None, "Ubuntu 24.04", cpu_arch)
    assert chosen is not None
    assert chosen.image_id == expected


def test_an_arm_image_is_not_offered_to_an_x86_node():
    assert _RULES.choose(_IMAGES, "X-Ubuntu24.04-ARM", None, "x86_64") is None


def test_a_cuda_build_suits_either_a_gpu_node_or_a_plain_one():
    cuda_only = [_IMAGES[2]]
    for cpu_arch in ("x86_64", None):
        chosen = _RULES.choose(cuda_only, None, "Ubuntu 24.04", cpu_arch)
        assert chosen is not None and chosen.image_id == "cuda-id"


def test_the_arm_build_is_reachable_by_name_on_an_arm_node():
    chosen = _RULES.choose(_IMAGES, "X-Ubuntu24.04-ARM", None, "aarch64")
    assert chosen is not None and chosen.image_id == "arm-id"
