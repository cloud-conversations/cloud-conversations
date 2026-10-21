"""In-memory testbed adapter."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import cloud_conversations.db.store as store
from cloud_conversations.adapter.errors import (
    CapacityError,
    FatalError,
    NoAddressError,
    PreconditionError,
    ProviderNotFoundError,
    TransientError,
)
from cloud_conversations.adapter.protocol import (
    AvailabilityWindow,
    Capabilities,
    ExecResult,
    Flavor,
    Image,
    Instance,
    Keypair,
    Lease,
    NodeType,
    SiteDescriptor,
)
from cloud_conversations.config import Config, KindFacts, Settings, resource_name
from cloud_conversations.schema.images import ImageRules
from cloud_conversations.schema.search import Candidate, Candidates, NodeQuery
from cloud_conversations.validator.probe import build_probe_script


@dataclass
class FakeSite:
    """One fake site's descriptor, stock, and images."""

    descriptor: SiteDescriptor
    images: list[Image] = field(default_factory=list)
    own_images: list[Image] = field(default_factory=list)
    keypairs: dict[str, str] = field(default_factory=dict)
    # node_type or flavor_id -> how many units exist
    stock: dict[str, int] = field(default_factory=dict)
    floating_ip_pool: int = 8
    floating_ip_quota: int = 4


@dataclass
class FakeLease:
    lease_id: str
    name: str
    site: str
    state: str  # PENDING | ACTIVE | TERMINATED | ERROR
    start: datetime
    end: datetime
    count: int
    node_type: str | None
    flavor_id: str | None
    reservation_id: str
    reserved_flavor_id: str | None
    reserved_fips: list[str]
    run_id: uuid.UUID | None
    reserved_host: str = "fake-node-01"


@dataclass
class FakeInstance:
    instance_id: str
    name: str
    site: str
    image_id: str
    keypair_name: str
    flavor_id: str
    state: str  # BUILD | ACTIVE | ERROR | DELETED
    floating_ip: str | None
    node_type: str | None
    run_id: uuid.UUID | None
    security_groups: list[str] = field(default_factory=list)


@dataclass
class HostFacts:
    """What the probe reports."""

    cpu_arch: str = "x86_64"
    cpu_count: int = 48
    mem_gb: int = 192
    disk_gb: int = 480
    kernel: str = "6.8.0-31-generic"
    os_release: str = "Ubuntu 24.04"
    dns_resolves: bool = True
    gpu_model: str = ""
    gpu_count: int = 0

    def to_probe_stdout(self) -> str:
        return "\n".join(
            [
                f"cpu_arch={self.cpu_arch}",
                f"cpu_count={self.cpu_count}",
                f"mem_gb={self.mem_gb}",
                f"disk_gb={self.disk_gb}",
                f"kernel={self.kernel}",
                f"os_release={self.os_release}",
                f"dns_resolves={'true' if self.dns_resolves else 'false'}",
                f"gpu_model={self.gpu_model}",
                f"gpu_count={self.gpu_count}",
            ]
        )


class FakeAdapter:
    """In-memory adapter that writes to the run_resources table."""

    def __init__(self, sites: dict[str, FakeSite], config: Config) -> None:
        self._sites = sites
        self._config = config

        self.leases: dict[str, FakeLease] = {}
        self.instances: dict[str, FakeInstance] = {}
        self.adhoc_fips: set[str] = set()
        self.security_groups: dict[str, str] = {}
        self._fip_counter = 0

    def settings(self, site: str) -> Settings:
        return self._config.for_site(site)

    def sites_with_login(self) -> list[str]:
        return sorted(self._sites)

    def _site(self, site: str) -> FakeSite:
        s = self._sites.get(site)
        if s is None:
            raise FatalError(f"Unknown site {site!r}", code="unknown_site")
        return s

    def _next_ip(self) -> str:
        self._fip_counter += 1
        return f"10.0.0.{self._fip_counter}"

    def _facts_for(self, inst: FakeInstance) -> HostFacts:
        desc = self._site(inst.site).descriptor
        os_release = next(
            (i.os_release for i in self._site(inst.site).images if i.image_id == inst.image_id),
            None,
        )
        base = HostFacts()
        if os_release:
            base.os_release = os_release

        if inst.node_type:
            nt = next((n for n in desc.node_types if n.name == inst.node_type), None)
            if nt is not None:
                base.cpu_arch = nt.cpu_arch
                base.cpu_count = nt.cpu_count
                base.mem_gb = nt.mem_gb
                base.disk_gb = nt.disk_gb
                base.gpu_model = nt.gpu_model or ""
        else:
            fl = next((f for f in desc.flavors if f.flavor_id == inst.flavor_id), None)
            if fl is not None:
                base.cpu_count = fl.vcpus
                base.mem_gb = fl.mem_gb
                base.disk_gb = fl.disk_gb
                base.gpu_count = fl.gpu_count
        return base

    def capabilities(self) -> Capabilities:
        return Capabilities(sites=[s.descriptor for s in self._sites.values()])

    def find_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        s = self._site(site)
        unit = node_type or flavor_id
        if unit is None:
            raise PreconditionError("node_type_or_flavor_id_required")

        total = s.stock.get(unit, 0)
        start = start or datetime.now(tz=timezone.utc) + timedelta(minutes=2)
        end = start + timedelta(hours=duration_hours)

        in_use = sum(
            lease.count
            for lease in self.leases.values()
            if lease.site == site
            and (lease.node_type == unit or lease.flavor_id == unit)
            and lease.state in ("PENDING", "ACTIVE")
            and lease.start < end
            and start < lease.end
        )
        free = max(0, total - in_use)
        return [AvailabilityWindow(start=start, end=end, count=free)] if free > 0 else []

    def published_node_types(self, site: str) -> list[NodeType]:
        return sorted(self._site(site).descriptor.node_types, key=lambda n: n.name)

    def published_flavors(self, site: str) -> list[Flavor]:
        return sorted(self._site(site).descriptor.flavors, key=lambda f: f.name)

    def search_hardware(self, query: NodeQuery) -> Candidates:
        """Free hardware from the fake stock that matches the query."""
        start = query.start_at or datetime.now(timezone.utc)
        items: list[Candidate] = []
        for site, fake in sorted(self._sites.items()):
            offered = fake.descriptor.reserves_by()
            if query.site not in (None, site) or query.reserve_by not in offered:
                continue
            if query.reserve_by == "node_type":
                units = [(n.name, n.name) for n in self.published_node_types(site)
                         if query.node_type in (None, n.name)]
            else:
                units = [(f.name, f.flavor_id) for f in self.published_flavors(site)
                         if query.flavor in (None, f.name)]
            for name, unit in units:
                windows = self.find_availability(
                    site,
                    unit if query.reserve_by == "node_type" else None,
                    unit if query.reserve_by == "flavor" else None,
                    query.duration_hours,
                    start,
                )
                free = windows[0].count if windows else 0
                items.append(Candidate(site=site, compute=name, free=free))
        end = start + timedelta(hours=query.duration_hours)
        return Candidates(query=query, start=start, end=end, items=items)

    def published_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        return self.find_availability(site, node_type, flavor_id, duration_hours, start)

    def _reservable_fips_free(self, site: str) -> int:
        """The reservable address pool, minus what this process has reserved."""
        reserved = sum(
            len(x.reserved_fips)
            for x in self.leases.values()
            if x.site == site and x.state in ("PENDING", "ACTIVE")
        )
        return max(0, self._site(site).floating_ip_pool - reserved)

    def list_leases(self, site: str) -> list[Lease]:
        self._site(site)
        return [self._to_lease(x) for x in self.leases.values() if x.site == site]

    def list_instances(self, site: str) -> list[Instance]:
        self._site(site)
        return [
            self._to_instance(i)
            for i in self.instances.values()
            if i.site == site and i.state != "DELETED"
        ]

    def get_instance(self, site: str, instance_id: str) -> Instance:
        inst = self.instances.get(instance_id)
        if inst is None or inst.site != site:
            raise ProviderNotFoundError("instance", instance_id)
        return self._to_instance(inst)

    def get_console_log(self, site: str, instance_id: str, lines: int) -> str:
        inst = self.instances.get(instance_id)
        if inst is None or inst.site != site:
            raise ProviderNotFoundError("instance", instance_id)
        return f"[fake console] instance {instance_id} state={inst.state}"

    def find_image(
        self,
        site: str,
        os_release: str | None = None,
        name: str | None = None,
        cpu_arch: str | None = None,
    ) -> Image | None:
        if not (name or os_release):
            raise PreconditionError("image_selector_required")
        return self._site(site).descriptor.images.choose(
            self.list_images(site), name, os_release, cpu_arch
        )

    def list_images(self, site: str) -> list[Image]:
        return sorted(self._site(site).images, key=lambda i: i.name)

    def list_own_images(self, site: str) -> list[Image]:
        return list(self._site(site).own_images)

    def list_flavors(self, site: str) -> list[Flavor]:
        return list(self._site(site).descriptor.flavors)

    def find_flavor(self, site: str, name: str) -> Flavor | None:
        return next((f for f in self.list_flavors(site) if f.name == name), None)

    def find_keypair(self, site: str, name: str) -> Keypair | None:
        s = self._site(site)
        pub = s.keypairs.get(name)
        return Keypair(name=name, public_key=pub) if pub else None

    def import_keypair(self, site: str, name: str, public_key: str) -> Keypair:
        self._site(site).keypairs[name] = public_key
        return Keypair(name=name, public_key=public_key)

    def list_keypairs(self, site: str) -> list[Keypair]:
        keypairs = self._site(site).keypairs
        return [Keypair(name=n, public_key=keypairs[n]) for n in sorted(keypairs)]

    def delete_keypair(self, site: str, name: str) -> bool:
        return self._site(site).keypairs.pop(name, None) is not None

    def _to_lease(self, x: FakeLease) -> Lease:
        active = x.state == "ACTIVE"
        return Lease(
            lease_id=x.lease_id,
            name=x.name,
            state=x.state,
            start=x.start,
            end=x.end,
            count=x.count,
            reservation_id=x.reservation_id if (active and x.node_type) else None,
            reserved_flavor_id=x.reserved_flavor_id if active else None,
            reserved_fips=list(x.reserved_fips) if active else [],
            reservation_status="active",
        )

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
        if bool(node_type) == bool(flavor_id):
            raise PreconditionError("exactly_one_of_node_type_or_flavor_id")

        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            found = self.leases.get(existing.provider_id)
            if found is None:
                raise ProviderNotFoundError("lease", existing.provider_id)
            return self._to_lease(found)

        if with_floating_ip and not self._reservable_fips_free(site):
            raise NoAddressError("Not enough floating IPs available")

        self._site(site)
        t_start = start or (datetime.now(tz=timezone.utc) + timedelta(minutes=2))
        lease_id = str(uuid.uuid4())
        reservation_id = str(uuid.uuid4())
        lease = FakeLease(
            lease_id=lease_id,
            name=resource_name(run_id),
            site=site,
            state="PENDING",
            start=t_start,
            end=t_start + timedelta(hours=duration_hours),
            count=1,
            node_type=node_type,
            flavor_id=flavor_id,
            reservation_id=reservation_id,
            reserved_flavor_id=reservation_id if flavor_id else None,
            reserved_fips=[self._next_ip()] if with_floating_ip else [],
            run_id=run_id,
        )
        self.leases[lease_id] = lease

        store.create_resource_with_event(
            run_id, lease_id, "lease", site, lease.end, idempotency_key=idempotency_key
        )
        return self._to_lease(lease)

    def wait_lease_active(
        self,
        site: str,
        lease_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Lease:
        lease = self.leases.get(lease_id)
        if lease is None or lease.site != site:
            raise ProviderNotFoundError("lease", lease_id)
        if on_poll is not None:
            on_poll()
        if lease.state == "TERMINATED":
            raise FatalError(
                f"Lease {lease_id} is TERMINATED", code="lease_terminated"
            )
        lease.state = "ACTIVE"
        return self._to_lease(lease)

    def reserved_host(self, site: str, reservation_id: str) -> str | None:
        for x in self.leases.values():
            if x.reservation_id == reservation_id:
                return x.reserved_host
        return None

    def delete_lease(self, site: str, lease_id: str) -> None:
        lease = self.leases.get(lease_id)
        if lease is None or lease.site != site:
            raise ProviderNotFoundError("lease", lease_id)
        lease.state = "TERMINATED"
        lease.reserved_fips = []

    def _to_instance(self, i: FakeInstance) -> Instance:
        return Instance(instance_id=i.instance_id, state=i.state, name=i.name)

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
        if not image_id:
            raise PreconditionError("image_required")
        if not keypair_name:
            raise PreconditionError("keypair_required")

        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            found = self.instances.get(existing.provider_id)
            if found is None:
                raise ProviderNotFoundError("instance", existing.provider_id)
            return self._to_instance(found)

        self._site(site)

        node_type: str | None = None
        if reservation_id is not None:
            lease = next(
                (
                    x
                    for x in self.leases.values()
                    if x.reservation_id == reservation_id and x.state == "ACTIVE"
                ),
                None,
            )
            if lease is None:
                raise PreconditionError("active_reservation_required")
            node_type = lease.node_type
            flavor_id = flavor or "baremetal"
        else:
            # The reserved flavor's id is the reservation id.
            lease = next(
                (
                    x
                    for x in self.leases.values()
                    if x.reserved_flavor_id == flavor and x.state == "ACTIVE"
                ),
                None,
            )
            if lease is None or flavor is None:
                raise PreconditionError("active_reservation_required")
            flavor_id = lease.flavor_id or flavor

        instance_id = str(uuid.uuid4())
        inst = FakeInstance(
            instance_id=instance_id,
            name=name,
            site=site,
            image_id=image_id,
            keypair_name=keypair_name,
            flavor_id=flavor_id,
            state="BUILD",
            floating_ip=None,
            node_type=node_type,
            run_id=run_id,
            security_groups=list(security_groups or []),
        )
        self.instances[instance_id] = inst

        store.create_resource_with_event(
            run_id, instance_id, "instance", site, expires_at, idempotency_key=idempotency_key
        )
        return self._to_instance(inst)

    def wait_instance_active(
        self,
        site: str,
        instance_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Instance:
        inst = self.instances.get(instance_id)
        if inst is None or inst.site != site:
            raise ProviderNotFoundError("instance", instance_id)
        if on_poll is not None:
            on_poll()
        inst.state = "ACTIVE"
        return self._to_instance(inst)

    def delete_instance(self, site: str, instance_id: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None or inst.site != site:
            raise ProviderNotFoundError("instance", instance_id)
        inst.state = "DELETED"
        inst.floating_ip = None

    def ensure_security_group(
        self, site: str, *, name: str, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            return name

        self._site(site)
        group_id = str(uuid.uuid4())
        self.security_groups[group_id] = name
        store.create_resource_with_event(
            run_id, group_id, "security_group", site, None, idempotency_key=idempotency_key
        )
        return name

    def delete_security_group(self, site: str, group_id: str) -> None:
        if group_id not in self.security_groups:
            raise ProviderNotFoundError("security_group", group_id)
        name = self.security_groups[group_id]
        bound = [
            i for i in self.instances.values()
            if name in i.security_groups and i.state not in ("DELETED", "SOFT_DELETED")
        ]
        if bound:
            raise FatalError(
                f"Security group {name} is in use by {bound[0].instance_id}",
                code="in_use",
            )
        del self.security_groups[group_id]

    def allocate_floating_ip(
        self, site: str, *, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing.provider_id

        if len(self.adhoc_fips) >= self._site(site).floating_ip_quota:
            raise CapacityError(f"No floating IPs left to allocate at {site}")
        address = self._next_ip()
        self.adhoc_fips.add(address)
        store.create_resource_with_event(
            run_id, address, "floating_ip", site, None, idempotency_key=idempotency_key
        )
        return address

    def delete_floating_ip(self, site: str, address: str) -> None:
        if address not in self.adhoc_fips:
            raise ProviderNotFoundError("floating_ip", address)
        self.adhoc_fips.discard(address)
        for inst in self.instances.values():
            if inst.floating_ip == address:
                inst.floating_ip = None

    def wait_instance_gone(self, site: str, instance_id: str) -> None:
        """Poll until the instance reports DELETED."""
        for _ in range(60):
            try:
                inst = self.get_instance(site, instance_id)
            except ProviderNotFoundError:
                return
            if inst.state in ("DELETED", "SOFT_DELETED"):
                return

    def attach_floating_ip(self, site: str, instance_id: str, address: str) -> None:
        inst = self.instances.get(instance_id)
        if inst is None or inst.site != site:
            raise ProviderNotFoundError("instance", instance_id)
        known = address in self.adhoc_fips or any(
            address in x.reserved_fips for x in self.leases.values()
        )
        if not known:
            raise PreconditionError("reserved_or_allocated_floating_ip_required")
        inst.floating_ip = address

    def ssh_exec(
        self,
        site: str,
        host: str,
        user: str,
        command: str,
        timeout_s: float,
    ) -> ExecResult:
        inst = next(
            (
                i
                for i in self.instances.values()
                if i.site == site and i.floating_ip == host and i.state == "ACTIVE"
            ),
            None,
        )
        if inst is None:
            raise PreconditionError("instance_active_with_fip_required")
        kind = self._site(site).descriptor.kind(
            "physical_host" if inst.node_type else "flavor_instance"
        )
        if kind is not None and kind.uses_security_groups and not inst.security_groups:
            raise TransientError(
                f"SSH connection refused at {host}: no security group admits port 22",
            )

        cmd = command.strip()
        if cmd == "true":
            return ExecResult(stdout="", stderr="", exit_code=0)
        if cmd == build_probe_script(self._site(site).descriptor.dns_probe_host):
            return ExecResult(
                stdout=self._facts_for(inst).to_probe_stdout(), stderr="", exit_code=0
            )
        return ExecResult(stdout="", stderr=f"fake: unrecognised command {command!r}", exit_code=1)


_NODE = NodeType(
    name="compute_skylake",
    cpu_arch="x86_64",
    cpu_count=48,
    mem_gb=192,
    disk_gb=480,
)
FAKE_KEYPAIR = "cc-keypair"

_KVM_FLAVOR = Flavor(flavor_id="m1.large", name="m1.large", vcpus=4, mem_gb=8, disk_gb=40)
# How the fake sites name their images.
FAKE_IMAGES = ImageRules(prefix="CC-", arch_markers={"aarch64": ["arm64", "aarch64"]})

_UBUNTU_24 = Image(
    image_id="img-ubuntu2404", name="CC-Ubuntu24.04", os_release="Ubuntu 24.04", supported=True
)
_UBUNTU_22 = Image(
    image_id="img-ubuntu2204", name="CC-Ubuntu22.04", os_release="Ubuntu 22.04", supported=True
)
_SNAPSHOT = Image(image_id="img-snapshot", name="my-experiment-snapshot")


def default_fake_sites() -> dict[str, FakeSite]:
    """A bare-metal site and a KVM site."""
    return {
        "FAKE@BM": FakeSite(
            descriptor=SiteDescriptor(
                images=FAKE_IMAGES,
                site="FAKE@BM",
                auth_url="https://fake.invalid:5000/v3",
                kinds=[KindFacts(reservation_kind="physical_host")],
                node_types=[_NODE],
            ),
            images=[_UBUNTU_24, _UBUNTU_22],
            own_images=[_SNAPSHOT],
            keypairs={FAKE_KEYPAIR: "ssh-rsa AAAA...fake"},
            stock={"compute_skylake": 10},
        ),
        "FAKE@KVM": FakeSite(
            descriptor=SiteDescriptor(
                images=FAKE_IMAGES,
                site="FAKE@KVM",
                auth_url="https://fake-kvm.invalid:5000/v3",
                kinds=[KindFacts(reservation_kind="flavor_instance", uses_security_groups=True)],
                flavors=[_KVM_FLAVOR],
            ),
            images=[_UBUNTU_24, _UBUNTU_22],
            keypairs={FAKE_KEYPAIR: "ssh-rsa AAAA...fake"},
            stock={"m1.large": 20},
        ),
    }
