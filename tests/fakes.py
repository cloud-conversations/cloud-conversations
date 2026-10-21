"""Fakes shared by the tests: a model, a testbed, a knowledge base, and their replies."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations.adapter.chameleon.search import search_hardware
from cloud_conversations.adapter.chameleon.sites import published_sites
from cloud_conversations.adapter.errors import (
    AdapterTimeoutError,
    CapacityError,
    FatalError,
    HostUnusableError,
    TransientError,
    UnschedulableError,
)
from cloud_conversations.adapter.fake import FakeAdapter, FakeInstance, FakeSite, HostFacts
from cloud_conversations.adapter.protocol import (
    AvailabilityWindow,
    ExecResult,
    Flavor,
    Instance,
    Lease,
    NodeType,
    SiteDescriptor,
)
from cloud_conversations.config import Config, Settings
from cloud_conversations.knowledge import chunks, index
from cloud_conversations.llm.client import LLMError, Reply
from cloud_conversations.schema.knowledge import BlogSource, Document, Knowledge, SphinxSource
from cloud_conversations.schema.search import Candidates, NodeQuery
from tests.conftest import BARE_METAL, FAST, PUBLISHED_CONFIG, VMS, LoggedOut

UC = "CHI@UC"
KVM = "KVM@TACC"

SITES = [
    SiteDescriptor(
        site=UC,
        auth_url="https://chi.uc.invalid:5000/v3",
        kinds=BARE_METAL,
        node_types=[
            NodeType(name="compute_skylake", cpu_arch="x86_64", cpu_count=48,
                     mem_gb=192, disk_gb=240),
            NodeType(name="gpu_rtx_6000", cpu_arch="x86_64", cpu_count=48,
                     mem_gb=192, disk_gb=240, gpu_model="RTX 6000"),
        ],
    ),
    SiteDescriptor(
        site=KVM,
        auth_url="https://kvm.tacc.invalid:5000/v3",
        kinds=VMS,
        flavors=[Flavor(flavor_id="1", name="m1.large", vcpus=4, mem_gb=8, disk_gb=40)],
    ),
]

FOUR_ROUNDS = Config(defaults=Settings(max_rounds=4))

KNOWLEDGE = Knowledge(
    query_prefix="",
    sources={
        "readthedocs": SphinxSource(
            kind="sphinx", url="https://docs.invalid/", label="Chameleon Docs",
            primary=True,
        ),
        "blog": BlogSource(
            kind="blog", url="https://blog.invalid", label="Chameleon Blog"
        ),
    },
)

LEASES = Document(
    source="readthedocs",
    url="https://docs.invalid/leases",
    title="Leases",
    content="Reserve a bare-metal node by creating a lease in Blazar.",
    fetched_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
)


def publish(documents: list[Document], knowledge: Knowledge, embedder) -> None:
    prepared = chunks.split(documents, knowledge)
    index.embed(prepared, embedder, knowledge)
    store.replace_knowledge(prepared, knowledge.embedding_model)


class Shelf:
    """A knowledge base that answers every question the same way."""

    def __init__(self, text: str = "CC-Ubuntu24.04 ships CUDA 12.") -> None:
        self.text = text
        self.asked: list[str] = []

    def search(self, question: str) -> str:
        self.asked.append(question)
        return self.text


def knowledge_search(question: str = "what does CC-Ubuntu24.04 hold?") -> str:
    return json.dumps({"action": "search_knowledge_base", "question": question})


def reference_node(node_type: str, availability: str = "available") -> dict:
    return {
        "node_type": node_type,
        "site_id": "uc",
        "availability": availability,
        "architecture": {"platform_type": "x86_64", "smt_size": 48},
        "main_memory": {"ram_size": 206158430208},
        "storage_devices": [{"size": 240000000000}],
        "gpu": {"gpu": node_type.startswith("gpu")},
    }


class Published(LoggedOut):
    """A logged-out FakeAdapter whose search reads the reference API at root."""

    def __init__(self, root: str, descriptors: list[SiteDescriptor]) -> None:
        super().__init__(
            sites={d.site: FakeSite(descriptor=d) for d in descriptors}, config=FOUR_ROUNDS
        )
        self.root = root

    def search_hardware(self, query: NodeQuery) -> Candidates:
        flavors = {
            site: self.published_flavors(site)
            for site in self._sites
            if "flavor" in self._sites[site].descriptor.reserves_by()
        }
        return search_hardware(self.root, query, published_sites(PUBLISHED_CONFIG), flavors, 5.0)


def search_reply(**over) -> str:
    return json.dumps({"action": "search", "query": {
        "reserve_by": "node_type",
        "site": UC, "node_type": "compute_skylake", "duration_hours": 2,
        "intent": "a skylake node", **over,
    }})


def answer_reply(**over) -> str:
    return json.dumps({"action": "answer", "description": {
        "site": UC,
        "duration_hours": 2,
        "items": [
            {"key": "node_type", "value": "compute_skylake", "source": "stated"},
            {"key": "os_release", "value": "Ubuntu 24.04", "source": "default"},
        ],
        "rubric": [{"id": "ssh", "kind": "ssh_reachable", "expected": True,
                    "comparator": "eq"},
                   {"id": "os", "kind": "os_release", "expected": "Ubuntu 24.04",
                    "comparator": "prefix", "from_requirement": "os_release"}],
        **over,
    }})


class FakeModel:
    """Canned replies, returned in order."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]]) -> Reply:
        self.calls.append([dict(m) for m in messages])
        if not self._replies:
            raise LLMError("the fake model has no replies left")
        return Reply(
            text=self._replies.pop(0), usage={}, finish_reason="stop",
            model="fake", served_model="fake", temperature=0.0,
        )


@dataclass
class FailureConfig:
    # The compute reservation's status once the lease is ACTIVE.
    reservation_status: str = "active"
    instance_boot_fails: bool = False
    # ERROR with a fault set.
    instance_boot_errors: bool = False
    # Reject the first N boots, then place.
    unschedulable_boots: int = 0
    # Placed, then failed to deploy.
    deploy_fails: bool = False
    fip_attach_fails: bool = False
    # The first N addresses attached never answer.
    ssh_dead_addresses: int = 0
    capacity_exhausted: bool = False
    # Report DELETING for this many get_instance calls before DELETED.
    instance_delete_polls: int = 0


class FaultyAdapter(FakeAdapter):
    """A FakeAdapter that fails as its FailureConfig says."""

    def __init__(
        self,
        sites: dict[str, FakeSite],
        failures: FailureConfig | None = None,
        facts_overrides: dict[str, HostFacts] | None = None,
        config: Config = FAST,
    ) -> None:
        super().__init__(sites, config)
        self.failures = failures or FailureConfig()
        # instance-name prefix -> facts.
        self.facts_overrides = facts_overrides or {}
        self._ssh_seen: list[str] = []
        # instance id -> get_instance calls left before DELETED.
        self._deleting: dict[str, int] = {}

    def _facts_for(self, inst: FakeInstance) -> HostFacts:
        for prefix, facts in self.facts_overrides.items():
            if inst.name.startswith(prefix):
                return facts
        return super()._facts_for(inst)

    def find_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        if self.failures.capacity_exhausted:
            raise CapacityError(f"No capacity at {site}")
        return super().find_availability(site, node_type, flavor_id, duration_hours, start)

    def create_lease(self, site: str, *, idempotency_key: str, **kwargs: Any) -> Lease:
        replay = store.resource_by_idempotency_key(idempotency_key)
        if self.failures.capacity_exhausted and replay is None:
            raise CapacityError(f"No capacity at {site}")
        return super().create_lease(site, idempotency_key=idempotency_key, **kwargs)

    def wait_lease_active(
        self,
        site: str,
        lease_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Lease:
        lease = super().wait_lease_active(site, lease_id, timeout_s, on_poll)
        status = self.failures.reservation_status.lower()
        if status in ("error", "failed"):
            raise FatalError(
                f"Lease {lease_id} is ACTIVE but its compute reservation is "
                f"{status!r}; no host will ever be schedulable for it",
                code="reservation_not_active",
            )
        if status not in ("active", ""):
            raise AdapterTimeoutError(
                f"Lease {lease_id} reservation did not become active after "
                f"{timeout_s:.0f}s",
                waited_s=timeout_s,
                last_state=status,
            )
        return lease

    def wait_instance_active(
        self,
        site: str,
        instance_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Instance:
        active = super().wait_instance_active(site, instance_id, timeout_s, on_poll)
        failure = self._boot_failure(instance_id, timeout_s)
        if failure is None:
            return active
        self.instances[instance_id].state = "ERROR"
        raise failure

    def _boot_failure(self, instance_id: str, timeout_s: float) -> Exception | None:
        if self.failures.unschedulable_boots > 0:
            self.failures.unschedulable_boots -= 1
            return UnschedulableError(
                f"Instance {instance_id} entered ERROR: 500 No valid host was found."
            )
        if self.failures.deploy_fails:
            return HostUnusableError(
                f"Instance {instance_id} entered ERROR: 500 Exceeded maximum number "
                f"of retries. Exhausted all hosts available for retrying build "
                f"failures for instance {instance_id}. Last exception: Failed to "
                f"provision instance {instance_id}: Failed to prepare to deploy"
            )
        if self.failures.instance_boot_errors:
            return FatalError(
                f"Instance {instance_id} entered ERROR: 500 Build of instance "
                f"{instance_id} aborted: Failure prepping block device",
                code="instance_error",
            )
        if self.failures.instance_boot_fails:
            return AdapterTimeoutError(
                f"Instance {instance_id} did not become ACTIVE after {timeout_s:.0f}s",
                waited_s=timeout_s,
                last_state="ERROR",
            )
        return None

    def get_instance(self, site: str, instance_id: str) -> Instance:
        found = super().get_instance(site, instance_id)
        if instance_id not in self._deleting:
            return found
        self._deleting[instance_id] -= 1
        if self._deleting[instance_id] <= 0:
            del self._deleting[instance_id]
            inst = self.instances[instance_id]
            inst.state = "DELETED"
            inst.floating_ip = None
        return super().get_instance(site, instance_id)

    def delete_instance(self, site: str, instance_id: str) -> None:
        if not self.failures.instance_delete_polls:
            super().delete_instance(site, instance_id)
            return
        super().get_instance(site, instance_id)
        self.instances[instance_id].state = "DELETING"
        self._deleting[instance_id] = self.failures.instance_delete_polls

    def attach_floating_ip(self, site: str, instance_id: str, address: str) -> None:
        if self.failures.fip_attach_fails:
            raise TransientError(f"could not attach {address}")
        super().attach_floating_ip(site, instance_id, address)

    def ssh_exec(
        self,
        site: str,
        host: str,
        user: str,
        command: str,
        timeout_s: float,
    ) -> ExecResult:
        if self.failures.ssh_dead_addresses:
            if host not in self._ssh_seen:
                self._ssh_seen.append(host)
            if self._ssh_seen.index(host) < self.failures.ssh_dead_addresses:
                raise AdapterTimeoutError(
                    f"SSH timeout on {host}", waited_s=timeout_s,
                    last_state="connecting",
                )
        return super().ssh_exec(site, host, user, command, timeout_s)
