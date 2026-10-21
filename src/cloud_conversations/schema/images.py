"""How a testbed names its images, from its [adapters.<name>.images] table."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from pydantic import BaseModel

_RELEASE = re.compile(r"([A-Za-z][A-Za-z ]*?)\s*([0-9]+(?:\.[0-9]+)?)")

# The build time of an image with none recorded.
EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class Image(BaseModel):
    image_id: str
    name: str
    os_release: str | None = None
    # Whether the testbed marks the image as one it supports.
    supported: bool = False
    created_at: datetime | None = None


class ImageRules(BaseModel):
    """A testbed's image names are <prefix><Distro><Version>, with an arch marker."""

    model_config = {"extra": "forbid", "frozen": True}

    prefix: str = ""
    # Name markers for each CPU architecture other than default_arch.
    arch_markers: dict[str, list[str]] = {}
    default_arch: str = "x86_64"
    # Properties every supported image carries.
    supported_properties: dict[str, str] = {}

    def name_for(self, os_release: str) -> str:
        """The image name for an OS release: Ubuntu 22.04.4 LTS -> <prefix>Ubuntu22.04."""
        parsed = parse_release(os_release)
        if parsed is None:
            return f"{self.prefix}{_without_lts(os_release).replace(' ', '')}"
        distro, version = parsed
        return f"{self.prefix}{distro}{version}"

    def arch_of(self, name: str) -> str:
        lowered = name.lower()
        for arch, markers in self.arch_markers.items():
            if any(marker.lower() in lowered for marker in markers):
                return arch
        return self.default_arch

    def choose(
        self,
        images: list[Image],
        image_name: str | None,
        os_release: str | None,
        cpu_arch: str | None,
    ) -> Image | None:
        """The image to boot for cpu_arch."""
        wanted = image_name or (self.name_for(os_release) if os_release else None)
        if wanted is None:
            return None
        pool = [i for i in images if cpu_arch is None or self.arch_of(i.name) == cpu_arch]
        target = wanted.strip().lower()
        exact = [i for i in pool if i.name.strip().lower() == target]
        prefixed = [i for i in pool if i.name.strip().lower().startswith(target)]
        return max(
            exact or prefixed,
            key=lambda i: (i.supported, -len(i.name), i.created_at or EPOCH, i.name),
            default=None,
        )

    def is_supported(self, properties: dict[str, object]) -> bool:
        """Whether an image with these properties carries every supported_properties value."""
        return bool(self.supported_properties) and all(
            str(properties.get(key)) == value for key, value in self.supported_properties.items()
        )


def parse_release(os_release: str) -> tuple[str, str] | None:
    """Split an OS release into distro and version: Ubuntu 22.04.4 LTS -> (Ubuntu, 22.04)."""
    match = _RELEASE.match(_without_lts(os_release))
    if match is None:
        return None
    return match.group(1).strip().replace(" ", ""), match.group(2)


def _without_lts(os_release: str) -> str:
    return re.sub(r"\bLTS\b", "", os_release, flags=re.I).strip()
