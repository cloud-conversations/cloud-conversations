"""Return types and structural Protocol for testbed adapters."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel

from cloud_conversations.config import DEFAULT_DNS_PROBE_HOST, KindFacts, Settings
from cloud_conversations.schema.description import RESERVATION_KINDS, ComputeKey, ReservationKind
from cloud_conversations.schema.images import Image, ImageRules
from cloud_conversations.schema.search import Candidates, NodeQuery


class NodeType(BaseModel):
    """A bare-metal node type from the testbed's hardware catalog."""

    name: str
    cpu_arch: str
    cpu_count: int
    mem_gb: int
    disk_gb: int
    gpu_model: str | None = None


class Flavor(BaseModel):
    """A VM flavor that can be reserved."""

    flavor_id: str
    name: str
    vcpus: int
    mem_gb: int
    disk_gb: int
    gpu: bool = False
    gpu_count: int = 0
    gpu_allocation: str | None = None  # e.g. pcie_passthrough, mig_slice


class SiteDescriptor(BaseModel):
    """A site and what it offers."""

    site: str
    auth_url: str
    kinds: list[KindFacts]
    instance_network: str | None = None
    floating_ip_pool: str | None = None
    dns_probe_host: str = DEFAULT_DNS_PROBE_HOST
    node_catalog_url: str | None = None
    node_types: list[NodeType] = []
    flavors: list[Flavor] = []
    images: ImageRules = ImageRules()

    def kind(self, reservation_kind: ReservationKind) -> KindFacts | None:
        return next((k for k in self.kinds if k.reservation_kind == reservation_kind), None)

    def reserves_by(self) -> list[ComputeKey]:
        """In the order the site lists its kinds."""
        return [
            key
            for k in self.kinds
            for key, kind in RESERVATION_KINDS.items()
            if kind == k.reservation_kind
        ]


class Capabilities(BaseModel):
    """The sites a run may be provisioned against."""

    sites: list[SiteDescriptor]

    def site(self, name: str) -> SiteDescriptor | None:
        return next((s for s in self.sites if s.site == name), None)


class AvailabilityWindow(BaseModel):
    start: datetime
    end: datetime
    count: int


class Lease(BaseModel):
    lease_id: str
    name: str
    state: str
    start: datetime
    end: datetime
    count: int
    reservation_id: str | None = None  # bare metal
    reserved_flavor_id: str | None = None  # a VM
    reserved_fips: list[str] = []  # public addresses
    reservation_status: str | None = None


class Instance(BaseModel):
    instance_id: str
    state: str
    name: str
    fault: str | None = None  # the provider's reason, once the state is ERROR


class Keypair(BaseModel):
    name: str
    public_key: str


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int


class TestbedAdapter(Protocol):

    def capabilities(self) -> Capabilities:
        """Return a descriptor for every site this adapter provides."""
        ...

    def sites_with_login(self) -> list[str]: ...

    def published_node_types(self, site: str) -> list[NodeType]:
        """The site's bare-metal node types, from the testbed's public catalog."""
        ...

    def published_flavors(self, site: str) -> list[Flavor]:
        """The site's VM flavors, from the testbed's public catalog."""
        ...

    def search_hardware(self, query: NodeQuery) -> Candidates:
        """Free hardware matching a describe agent search. Raises AdapterError if it cannot."""
        ...

    def settings(self, site: str) -> Settings: ...

    def list_instances(self, site: str) -> list[Instance]: ...

    def get_instance(self, site: str, instance_id: str) -> Instance: ...

    def get_console_log(self, site: str, instance_id: str, lines: int) -> str: ...

    def find_image(
        self,
        site: str,
        os_release: str | None = None,
        name: str | None = None,
        cpu_arch: str | None = None,
    ) -> Image | None:
        """Resolve an image by name or OS release."""
        ...

    def list_images(self, site: str) -> list[Image]:
        """Every image the testbed publishes at a site."""
        ...

    def list_own_images(self, site: str) -> list[Image]:
        """Every image the caller's own project owns at a site."""
        ...

    def find_flavor(self, site: str, name: str) -> Flavor | None: ...

    def find_keypair(self, site: str, name: str) -> Keypair | None: ...

    def import_keypair(self, site: str, name: str, public_key: str) -> Keypair:
        """Create the keypair, or replace one of that name that has another key."""
        ...

    def list_keypairs(self, site: str) -> list[Keypair]: ...

    def delete_keypair(self, site: str, name: str) -> bool:
        """Delete the keypair. False if the site has none of that name."""
        ...

    def launch_instance(
        self,
        site: str,
        *,
        image_id: str,
        keypair_name: str,
        name: str,
        flavor: str | None = None,
        reservation_id: str | None = None,
        security_groups: list[str] | None = None,
        expires_at: datetime | None = None,
        run_id: uuid.UUID,
        idempotency_key: str,
    ) -> Instance:
        """Boot an instance.

        Bare metal needs reservation_id. A VM needs its reserved flavor id as flavor.
        """
        ...

    def wait_instance_active(
        self,
        site: str,
        instance_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Instance: ...

    def delete_instance(self, site: str, instance_id: str) -> None: ...

    def wait_instance_gone(self, site: str, instance_id: str) -> None:
        """Block until the instance stops being visible."""
        ...

    def ensure_security_group(
        self, site: str, *, name: str, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        """Create a run-owned group admitting SSH and return its name."""
        ...

    def delete_security_group(self, site: str, group_id: str) -> None: ...

    def allocate_floating_ip(
        self, site: str, *, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        """Allocate a public address outside any lease."""
        ...

    def delete_floating_ip(self, site: str, address: str) -> None: ...

    def attach_floating_ip(self, site: str, instance_id: str, address: str) -> None:
        """Attach a reserved or allocated address to an instance."""
        ...

    def ssh_exec(
        self,
        site: str,
        host: str,
        user: str,
        command: str,
        timeout_s: float,
    ) -> ExecResult:
        """Run one command. The instance at host must be ACTIVE with that IP."""
        ...

    def find_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        """Windows in which a unit is free, per the calendar."""
        ...

    def published_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        """Free windows, from the testbed's published availability."""
        ...

    def list_leases(self, site: str) -> list[Lease]: ...

    def create_lease(
        self,
        site: str,
        *,
        duration_hours: float,
        start: datetime | None = None,
        node_type: str | None = None,
        flavor_id: str | None = None,
        with_floating_ip: bool = True,
        run_id: uuid.UUID,
        idempotency_key: str,
    ) -> Lease:
        """Create one lease with every reservation the run needs.

        Exactly one of node_type (bare metal) or flavor_id (a VM) must be given.
        """
        ...

    def wait_lease_active(
        self,
        site: str,
        lease_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Lease:
        """Block until the lease and its compute reservation are active, or raise."""
        ...

    def delete_lease(self, site: str, lease_id: str) -> None: ...

    def reserved_host(self, site: str, reservation_id: str) -> str | None:
        """Name the host behind a reservation."""
        ...
