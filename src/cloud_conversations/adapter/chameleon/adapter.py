"""Chameleon Cloud testbed adapter. Needs the `integration` extra."""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import chi
import chi.image
import chi.lease
import chi.network
import chi.server
import keystoneauth1.adapter
import keystoneauth1.exceptions
import keystoneauth1.plugin
import keystoneauth1.session
import paramiko

import cloud_conversations.db.store as store
from cloud_conversations.adapter import system_key
from cloud_conversations.adapter.chameleon.catalog import fetch_flavors, fetch_node_types
from cloud_conversations.adapter.chameleon.search import (
    flavor_availability,
    search_hardware,
    search_nodes,
)
from cloud_conversations.adapter.chameleon.sites import (
    load_capabilities,
    published_sites,
    reference_api_root,
)
from cloud_conversations.adapter.errors import (
    AdapterError,
    AdapterTimeoutError,
    CapacityError,
    FatalError,
    HostUnusableError,
    NoAddressError,
    PreconditionError,
    ProviderNotFoundError,
    TransientError,
    UnschedulableError,
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
from cloud_conversations.config import Config, ConfigError, Settings, resource_name
from cloud_conversations.schema.search import Candidates, NodeQuery

log = logging.getLogger(__name__)

_T = TypeVar("_T")

BLAZAR_TIME_FORMAT = "%Y-%m-%d %H:%M"

_RT_HOST = "physical:host"
_RT_FLAVOR = "flavor:instance"
_RT_FIP = "virtual:floatingip"

# Nova found no host to place the instance on.
_UNSCHEDULABLE = ("no valid host",)

# Nova placed the instance and Ironic could not deploy it.
_HOST_UNUSABLE = ("exhausted all hosts", "failed to prepare to deploy")

# Blazar has no reservable address.
_NO_ADDRESS = ("floating ip", "floatingip")

# Bytes kept of each of a command's output streams.
_OUTPUT_BYTES = 1 << 20

# Seconds between reads of a command's output.
_READ_POLL_S = 0.05


@dataclass(frozen=True)
class Login:
    """How one principal authenticates at a site."""

    # The Keystone auth plugin, given the site's auth_url.
    auth: Callable[[str], keystoneauth1.plugin.BaseAuthPlugin]
    # Values redacted from provider errors.
    secrets: tuple[str, ...] = ()


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(BLAZAR_TIME_FORMAT)


def _parse_dt(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime {s!r}")


def _authorized_key_user_data(key: paramiko.PKey) -> str:
    """Cloud-init user data adding key to the default user's authorized_keys."""
    return f"#cloud-config\nssh_authorized_keys:\n  - {system_key.authorized_key(key)}\n"


def _instance_from_nova(server: Any) -> Instance:
    raw_fault = getattr(server, "fault", None) or {}
    fault = None
    if raw_fault:
        fault = " ".join(
            str(raw_fault[k]) for k in ("code", "message", "details") if raw_fault.get(k)
        ).strip() or None
    return Instance(
        instance_id=server.id,
        state=server.status,
        name=server.name,
        fault=fault,
    )


class ChameleonAdapter:
    """Adapter over the Chameleon sites a principal can log in to."""

    def __init__(self, logins: dict[str, Login], config: Config) -> None:
        self._logins = logins
        self._config = config
        self._caps = load_capabilities(config, "chameleon")
        self._sessions: dict[str, keystoneauth1.adapter.Adapter] = {}

    def settings(self, site: str) -> Settings:
        return self._config.for_site(site)

    def sites_with_login(self) -> list[str]:
        return sorted(self._logins)

    def _descriptor(self, site: str) -> SiteDescriptor:
        d = self._caps.site(site)
        if d is None:
            known = ", ".join(s.site for s in self._caps.sites)
            raise FatalError(
                f"Unknown site {site!r}; known sites: {known}", code="unknown_site"
            )
        return d

    def _session(self, site: str) -> keystoneauth1.adapter.Adapter:
        """The site's Keystone session, made on first use."""
        session = self._sessions.get(site)
        if session is None:
            login = self._logins.get(site)
            if login is None:
                known = ", ".join(sorted(self._logins)) or "no sites"
                raise FatalError(
                    f"No login for {site!r}; there are logins for {known}",
                    code="no_credentials",
                )
            session = keystoneauth1.adapter.Adapter(
                keystoneauth1.session.Session(auth=login.auth(self._descriptor(site).auth_url)),
                region_name=site,
                interface="public",
            )
            self._sessions[site] = session
        return session

    def _scrub(self, text: str) -> str:
        """Replace every login's secrets in text with a marker."""
        for login in self._logins.values():
            for secret in login.secrets:
                text = text.replace(secret, "[redacted]")
        return text

    @contextmanager
    def _ctx(self, site: str, op: str) -> Generator[SiteDescriptor, None, None]:
        """The site's descriptor, with the provider call timed and its failure logged."""
        d = self._descriptor(site)
        self._session(site)
        t0 = time.monotonic()
        try:
            yield d
        except Exception as exc:
            log.debug(
                "provider call failed op=%s site=%s elapsed=%.3fs %s: %s",
                op, site, time.monotonic() - t0, type(exc).__name__, self._scrub(str(exc)),
            )
            raise
        log.debug("provider call ok op=%s site=%s elapsed=%.3fs", op, site, time.monotonic() - t0)

    def capabilities(self) -> Capabilities:
        return self._caps

    def published_node_types(self, site: str) -> list[NodeType]:
        descriptor = self._descriptor(site)
        if descriptor.node_catalog_url is None:
            return sorted(descriptor.node_types, key=lambda n: n.name)
        return fetch_node_types(
            descriptor.node_catalog_url, self.settings(site).directory_api_s
        )

    def published_flavors(self, site: str) -> list[Flavor]:
        descriptor = self._descriptor(site)
        uid = published_sites(self._config).get(site)
        root = reference_api_root(self._config, [site])
        if uid is None or root is None:
            return sorted(descriptor.flavors, key=lambda f: f.name)
        return fetch_flavors(
            f"{root}/sites/{uid}/flavors", self.settings(site).directory_api_s
        )

    def search_hardware(self, query: NodeQuery) -> Candidates:
        sites = [
            d.site for d in self._caps.sites
            if query.reserve_by in d.reserves_by() and query.site in (None, d.site)
        ]
        root = reference_api_root(self._config, sites)
        if root is None:
            raise FatalError("no reference API is configured for these sites", code="no_search")
        timeout_s = self._config.for_site(query.site).directory_api_s
        flavors = (
            {site: self.published_flavors(site) for site in sites}
            if query.reserve_by == "flavor" else {}
        )
        return search_hardware(
            root, query, published_sites(self._config), flavors, timeout_s
        )

    def find_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        """Windows in which a unit is free, per the Blazar calendar."""
        start = start or _now_utc() + timedelta(minutes=2)
        end = start + timedelta(hours=duration_hours)

        with self._ctx(site, "find_availability"):
            blazar = chi.lease.blazar(self._session(site))
            if flavor_id:
                raw = self._retry_read(
                    site,
                    "flavor_availability",
                    flavor_id,
                    lambda: blazar.flavor_instance.get_availability(
                        flavor_id=flavor_id,
                        start_date=_fmt(start),
                        end_date=_fmt(end),
                    ),
                )
                free = 0
                for entry in raw.get("flavor_instances", []):
                    if entry.get("flavor_id") != flavor_id:
                        continue
                    free = min(
                        (int(w.get("available", 0)) for w in entry.get("availability", [])),
                        default=0,
                    )
                return [AvailabilityWindow(start=start, end=end, count=free)] if free > 0 else []

            if not node_type:
                raise PreconditionError("node_type_required")
            hosts = [
                h
                for h in self._retry_read(site, "host_list", node_type, blazar.host.list)
                if h.get("node_type") == node_type and h.get("reservable", False)
            ]
            if not hosts:
                return []
            host_ids = {str(h["id"]) for h in hosts}
            allocations = self._retry_read(
                site, "host_allocations", node_type, blazar.host.list_allocations
            )

        busy: set[str] = set()
        for alloc in allocations:
            rid = str(alloc.get("resource_id"))
            if rid not in host_ids:
                continue
            for res in alloc.get("reservations", []):
                try:
                    r_start = _parse_dt(res["start_date"])
                    r_end = _parse_dt(res["end_date"])
                except (KeyError, ValueError):
                    busy.add(rid)
                    break
                if r_start < end and start < r_end:
                    busy.add(rid)
                    break

        free = len(host_ids - busy)
        return [AvailabilityWindow(start=start, end=end, count=free)] if free > 0 else []

    def published_availability(
        self,
        site: str,
        node_type: str | None,
        flavor_id: str | None,
        duration_hours: float,
        start: datetime | None = None,
    ) -> list[AvailabilityWindow]:
        """Free windows, from the reference API."""
        root = reference_api_root(self._config, [site])
        uid = published_sites(self._config).get(site)
        if root is None or uid is None:
            return self.find_availability(site, node_type, flavor_id, duration_hours, start)
        start = start or _now_utc()
        end = start + timedelta(hours=duration_hours)
        timeout_s = self.settings(site).directory_api_s
        if flavor_id:
            free = flavor_availability(root, uid, flavor_id, start, end, timeout_s)
        elif node_type:
            query = NodeQuery(
                reserve_by="node_type", site=site, node_type=node_type, start_at=start,
                duration_hours=math.ceil(duration_hours), intent="availability",
            )
            free = sum(c.free for c in search_nodes(root, query, {site: uid}, timeout_s).items)
        else:
            raise PreconditionError("node_type_required")
        return [AvailabilityWindow(start=start, end=end, count=free)] if free > 0 else []

    def list_leases(self, site: str) -> list[Lease]:
        with self._ctx(site, "list_leases"):
            raws = chi.lease.blazar(self._session(site)).lease.list()
            return [self._lease_from_raw(r) for r in raws]

    def list_instances(self, site: str) -> list[Instance]:
        with self._ctx(site, "list_instances"):
            servers = chi.server.nova(self._session(site)).servers.list()
            return [_instance_from_nova(s) for s in servers]

    def get_instance(self, site: str, instance_id: str) -> Instance:
        with self._ctx(site, "get_instance"):
            return _instance_from_nova(
                self._retry_read(
                    site,
                    "instance",
                    instance_id,
                    lambda: chi.server.nova(self._session(site)).servers.get(instance_id),
                )
            )

    def get_console_log(self, site: str, instance_id: str, lines: int) -> str:
        with self._ctx(site, "get_console_log"):
            try:
                server = chi.server.nova(self._session(site)).servers.get(instance_id)
                out = server.get_console_output(length=lines)
            except Exception as exc:
                raise self._map_provider_error(exc, "instance", instance_id) from exc
        return out if isinstance(out, str) else str(out)

    def find_image(
        self,
        site: str,
        os_release: str | None = None,
        name: str | None = None,
        cpu_arch: str | None = None,
    ) -> Image | None:
        """Match on the CC-<Distro><Version> convention, or return None."""
        if not (name or os_release):
            raise PreconditionError("image_selector_required")

        chosen = self._descriptor(site).images.choose(
            self.list_images(site), name, os_release, cpu_arch
        )
        if chosen is None:
            return None
        return chosen.model_copy(update={"os_release": os_release})

    def list_images(self, site: str) -> list[Image]:
        """Every image the project can see at a site, sorted by name."""
        rules = self._descriptor(site).images
        with self._ctx(site, "list_images"):
            try:
                raw = self._retry_read(
                    site,
                    "image_list",
                    site,
                    lambda: list(chi.image.glance(self._session(site)).images.list()),
                )
            except Exception as exc:
                raise self._map_provider_error(exc, "image_list", site) from exc
        return sorted(
            (
                Image(
                    image_id=i.id,
                    name=i.name,
                    supported=rules.is_supported(i),
                    created_at=_created_at(i),
                )
                for i in raw
            ),
            key=lambda i: i.name,
        )

    def list_own_images(self, site: str) -> list[Image]:
        """Every image this project owns at a site, newest first."""
        with self._ctx(site, "list_own_images"):
            try:
                owner = self._session(site).get_project_id()
                raw = self._retry_read(
                    site,
                    "own_image_list",
                    site,
                    lambda: chi.image.glance(self._session(site)).images.list(
                        filters={"owner": owner}
                    ),
                )
                return [
                    Image(image_id=i.id, name=i.name)
                    for i in sorted(raw, key=lambda i: i.created_at, reverse=True)
                ]
            except Exception as exc:
                raise self._map_provider_error(exc, "own_image_list", site) from exc

    def list_flavors(self, site: str) -> list[Flavor]:
        """Every VM flavor at a site, excluding reserved flavors."""
        flavors = []
        with self._ctx(site, "list_flavors"):
            try:
                for f in self._retry_read(
                    site, "flavor_list", site, chi.server.nova(self._session(site)).flavors.list
                ):
                    extras = getattr(f, "extra_specs", None)
                    if extras is None:
                        extras = f.get_keys() or {}
                    if any(k.startswith("resources:CUSTOM_RESERVATION_") for k in extras):
                        continue
                    flavors.append(
                        Flavor(
                            flavor_id=f.id,
                            name=f.name,
                            vcpus=int(getattr(f, "vcpus", 0) or 0),
                            mem_gb=int((getattr(f, "ram", 0) or 0) / 1024),
                            disk_gb=int(getattr(f, "disk", 0) or 0),
                        )
                    )
            except Exception as exc:
                raise self._map_provider_error(exc, "flavor_list", site) from exc
        return flavors

    def find_flavor(self, site: str, name: str) -> Flavor | None:
        """Resolve a VM flavor by name, excluding reserved flavors."""
        return next((f for f in self.list_flavors(site) if f.name == name), None)

    def find_keypair(self, site: str, name: str) -> Keypair | None:
        with self._ctx(site, "find_keypair"):
            try:
                kp = self._retry_read(
                    site,
                    "keypair",
                    name,
                    lambda: chi.server.nova(self._session(site)).keypairs.get(name),
                )
            except Exception as exc:
                mapped = self._map_provider_error(exc, "keypair", name)
                if isinstance(mapped, ProviderNotFoundError):
                    return None
                raise mapped from exc
        return Keypair(name=kp.name, public_key=kp.public_key)

    def list_keypairs(self, site: str) -> list[Keypair]:
        with self._ctx(site, "list_keypairs"):
            listed = self._retry_read(
                site, "keypair_list", site, chi.server.nova(self._session(site)).keypairs.list
            )
        return sorted(
            (Keypair(name=k.name, public_key=k.public_key) for k in listed), key=lambda k: k.name
        )

    def delete_keypair(self, site: str, name: str) -> bool:
        with self._ctx(site, "delete_keypair"):
            try:
                chi.server.nova(self._session(site)).keypairs.delete(name)
            except Exception as exc:
                mapped = self._map_provider_error(exc, "keypair", name)
                if isinstance(mapped, ProviderNotFoundError):
                    return False
                raise mapped from exc
        log.info("deleted keypair site=%s name=%s", site, name)
        return True

    def import_keypair(self, site: str, name: str, public_key: str) -> Keypair:
        existing = self.find_keypair(site, name)
        if existing is not None and existing.public_key.split()[:2] == public_key.split()[:2]:
            return existing
        with self._ctx(site, "import_keypair"):
            nova = chi.server.nova(self._session(site))
            try:
                if existing is not None:
                    nova.keypairs.delete(name)
                kp = nova.keypairs.create(name, public_key=public_key, key_type="ssh")
            except Exception as exc:
                raise self._map_provider_error(exc, "keypair", name) from exc
        log.info("imported keypair site=%s name=%s replaced=%s", site, name, existing is not None)
        return Keypair(name=kp.name, public_key=kp.public_key)

    def _lease_from_raw(self, raw: dict, reserved_fips: list[str] | None = None) -> Lease:
        active = raw.get("status") == "ACTIVE"
        reservations = raw.get("reservations", [])

        node_res = [r for r in reservations if r.get("resource_type") == _RT_HOST]
        flavor_res = [r for r in reservations if r.get("resource_type") == _RT_FLAVOR]

        count = 1
        if node_res:
            count = int(node_res[0].get("min", 1))
        elif flavor_res:
            count = int(flavor_res[0].get("amount", 1))

        compute_res = node_res or flavor_res
        return Lease(
            lease_id=raw["id"],
            name=raw.get("name", ""),
            state=raw.get("status", "UNKNOWN"),
            start=_parse_dt(raw["start_date"]),
            end=_parse_dt(raw["end_date"]),
            count=count,
            reservation_id=node_res[0]["id"] if node_res and active else None,
            reserved_flavor_id=flavor_res[0].get("flavor_id") if flavor_res and active else None,
            reserved_fips=reserved_fips or [],
            reservation_status=compute_res[0].get("status") if compute_res else None,
        )

    def _reserved_fips(self, site: str, raw: dict) -> list[str]:
        """Addresses this lease reserved, found by Blazar's reservation tag."""
        res_ids = {
            str(r["id"]) for r in raw.get("reservations", []) if r.get("resource_type") == _RT_FIP
        }
        if not res_ids:
            return []
        tags = {f"reservation:{rid}" for rid in res_ids}
        return [
            f["floating_ip_address"]
            for f in self._retry_read(
                site,
                "floating_ip",
                "reserved",
                lambda: chi.network.neutron(self._session(site)).list_floatingips()["floatingips"],
            )
            if tags & set(f.get("tags", []))
        ]

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
            return self.get_lease(site, existing.provider_id)

        t_start = start or (_now_utc() + timedelta(minutes=2))
        t_end = t_start + timedelta(hours=duration_hours)

        with self._ctx(site, "create_lease") as d:
            reservations: list[dict[str, Any]] = []
            if node_type:
                reservations.append(
                    {
                        "resource_type": _RT_HOST,
                        "resource_properties": json.dumps(["==", "$node_type", node_type]),
                        "hypervisor_properties": "",
                        "min": 1,
                        "max": 1,
                    }
                )
            else:
                reservations.append(
                    {
                        "resource_type": _RT_FLAVOR,
                        "flavor_id": flavor_id,
                        "amount": 1,
                        "affinity": None,
                    }
                )
            if with_floating_ip:
                reservations.append(
                    {
                        "resource_type": _RT_FIP,
                        "network_id": self._network_id(site, self._floating_ip_pool(d)),
                        "amount": 1,
                    }
                )

            try:
                raw = chi.lease.blazar(self._session(site)).lease.create(
                    name=resource_name(run_id),
                    start=_fmt(t_start),
                    end=_fmt(t_end),
                    reservations=reservations,
                    events=[],
                )
            except Exception as exc:
                raise self._map_lease_create_error(exc) from exc

        log.info(
            "created lease site=%s lease_id=%s run_id=%s start=%s end=%s reservations=%s",
            site, raw["id"], run_id, _fmt(t_start), _fmt(t_end),
            [r["resource_type"] for r in reservations],
        )
        store.create_resource_with_event(
            run_id, raw["id"], "lease", site, t_end, idempotency_key=idempotency_key
        )
        return self._lease_from_raw(raw)

    def get_lease(self, site: str, lease_id: str) -> Lease:
        with self._ctx(site, "get_lease"):
            raw = self._retry_read(
                site,
                "lease",
                lease_id,
                lambda: chi.lease.blazar(self._session(site)).lease.get(lease_id),
            )
            fips = self._reserved_fips(site, raw) if raw.get("status") == "ACTIVE" else []
        return self._lease_from_raw(raw, fips)

    def _wait_active(
        self,
        site: str,
        what: str,
        get: Callable[[], _T],
        state: Callable[[_T], str],
        failure: Callable[[_T], Exception | None],
        timeout_s: float,
        on_poll: Callable[[], None] | None,
    ) -> _T:
        """Poll until the state is ACTIVE, tolerating transient errors.

        Raises the failure for a state that will never become ACTIVE.
        """
        interval = self.settings(site).poll_interval_s
        waited = 0.0
        last_state = "UNKNOWN"
        while waited < timeout_s:
            try:
                obj = get()
            except TransientError as exc:
                log.debug("still waiting on %s: %s", what, exc)
            else:
                last_state = state(obj)
                if last_state == "ACTIVE":
                    return obj
                error = failure(obj)
                if error is not None:
                    raise error
            if on_poll is not None:
                on_poll()
            time.sleep(interval)
            waited += interval
        raise AdapterTimeoutError(
            f"{what} did not become ACTIVE after {timeout_s:.0f}s",
            waited_s=waited,
            last_state=last_state,
        )

    def wait_lease_active(
        self,
        site: str,
        lease_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Lease:
        """Block until the lease and its compute reservation are both active."""

        def get() -> Lease:
            return self.get_lease(site, lease_id)

        def ended(lease: Lease) -> Exception | None:
            if lease.state not in ("ERROR", "TERMINATED"):
                return None
            return FatalError(f"Lease {lease_id} entered {lease.state}", code="lease_not_active")

        def reservation(lease: Lease) -> str:
            return (lease.reservation_status or "active").upper()

        def unschedulable(lease: Lease) -> Exception | None:
            status = (lease.reservation_status or "").lower()
            if status not in ("error", "failed"):
                return None
            return FatalError(
                f"Lease {lease_id} is ACTIVE but its compute reservation is "
                f"{status!r}; no host will ever be schedulable for it",
                code="reservation_not_active",
            )

        lease = self._wait_active(
            site, f"Lease {lease_id}", get, lambda x: x.state, ended, timeout_s, on_poll
        )
        if reservation(lease) == "ACTIVE":
            return lease
        return self._wait_active(
            site, f"Lease {lease_id} reservation", get, reservation, unschedulable,
            timeout_s, on_poll,
        )

    def delete_lease(self, site: str, lease_id: str) -> None:
        with self._ctx(site, "delete_lease"):
            log.info("deleting lease site=%s lease_id=%s", site, lease_id)
            self._delete(
                "lease", lease_id,
                lambda: chi.lease.blazar(self._session(site)).lease.delete(lease_id),
                lambda: chi.lease.blazar(self._session(site)).lease.get(lease_id),
            )

    def reserved_host(self, site: str, reservation_id: str) -> str | None:
        """Name the host behind a reservation."""
        with self._ctx(site, "reserved_host"):
            try:
                blazar = chi.lease.blazar(self._session(site))
                for alloc in blazar.host.list_allocations():
                    if any(r.get("id") == reservation_id for r in alloc.get("reservations", [])):
                        host = blazar.host.get(alloc["resource_id"])
                        name = host.get("node_name") or host.get("hypervisor_hostname")
                        return str(name) if name else None
            except Exception:  # noqa: BLE001
                return None
        return None

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
            return self.get_instance(site, existing.provider_id)

        with self._ctx(site, "launch_instance") as d:
            flavor_name = flavor
            if not flavor_name and reservation_id:
                flavor_name = self._config.for_site(site, "physical_host").baremetal_flavor
                if flavor_name is None:
                    raise ConfigError(f"set baremetal_flavor for {site}")
            if not flavor_name:
                raise PreconditionError("flavor_required")

            flavor_id = self._resolve_flavor_id(site, flavor_name)

            key = system_key.current()
            kwargs: dict[str, Any] = {
                "name": name,
                "image": image_id,
                "flavor": flavor_id,
                "key_name": keypair_name,
                "nics": [{"net-id": self._network_id(site, self._instance_network(d))}],
                "meta": {
                    "run_id": str(run_id),
                    "cc_ikey": idempotency_key,
                    "cc_system_key": key.fingerprint,
                },
                "userdata": _authorized_key_user_data(key),
            }
            if security_groups:
                kwargs["security_groups"] = security_groups
            if reservation_id:
                kwargs["scheduler_hints"] = {"reservation": reservation_id}

            try:
                server = chi.server.nova(self._session(site)).servers.create(**kwargs)
            except Exception as exc:
                raise self._map_provider_error(exc, "instance", name) from exc

        log.info(
            "launched instance site=%s instance_id=%s name=%s flavor=%s reservation=%s",
            site, server.id, name, flavor_id, reservation_id,
        )
        store.create_resource_with_event(
            run_id, server.id, "instance", site, expires_at, idempotency_key=idempotency_key
        )
        return _instance_from_nova(server)

    def _resolve_flavor_id(self, site: str, flavor: str) -> str:
        """Return a Nova flavor id for a name or id."""
        nova = chi.server.nova(self._session(site))
        try:
            return nova.flavors.get(flavor).id
        except Exception:  # noqa: BLE001
            pass
        for f in nova.flavors.list():
            if f.name == flavor:
                return f.id
        raise ProviderNotFoundError("flavor", flavor)

    def wait_instance_active(
        self,
        site: str,
        instance_id: str,
        timeout_s: float,
        on_poll: Callable[[], None] | None = None,
    ) -> Instance:
        def errored(inst: Instance) -> Exception | None:
            if inst.state != "ERROR":
                return None
            return instance_error(
                f"Instance {instance_id} entered ERROR" + (f": {inst.fault}" if inst.fault else "")
            )

        return self._wait_active(
            site, f"Instance {instance_id}", lambda: self.get_instance(site, instance_id),
            lambda x: x.state, errored, timeout_s, on_poll,
        )

    def delete_instance(self, site: str, instance_id: str) -> None:
        with self._ctx(site, "delete_instance"):
            log.info("deleting instance site=%s instance_id=%s", site, instance_id)
            self._delete(
                "instance", instance_id,
                lambda: chi.server.nova(self._session(site)).servers.delete(instance_id),
                lambda: chi.server.nova(self._session(site)).servers.get(instance_id),
            )

    def ensure_security_group(
        self, site: str, *, name: str, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        """Create a run-owned group admitting SSH and return its name."""
        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            return name

        with self._ctx(site, "ensure_security_group"):
            neutron = chi.network.neutron(self._session(site))
            try:
                group = neutron.create_security_group(
                    {
                        "security_group": {
                            "name": name,
                            "description": f"cloud-conversations run {run_id}",
                        }
                    }
                )["security_group"]
                neutron.create_security_group_rule(
                    {
                        "security_group_rule": {
                            "security_group_id": group["id"],
                            "direction": "ingress",
                            "protocol": "tcp",
                            "port_range_min": 22,
                            "port_range_max": 22,
                            "remote_ip_prefix": "0.0.0.0/0",
                        }
                    }
                )
            except Exception as exc:
                raise self._map_provider_error(exc, "security_group", name) from exc

        log.info("created security group site=%s name=%s id=%s", site, name, group["id"])
        store.create_resource_with_event(
            run_id, group["id"], "security_group", site, None,
            idempotency_key=idempotency_key,
        )
        return name

    def delete_security_group(self, site: str, group_id: str) -> None:
        with self._ctx(site, "delete_security_group"):
            log.info("deleting security group site=%s id=%s", site, group_id)
            neutron = chi.network.neutron(self._session(site))
            self._delete(
                "security_group", group_id,
                lambda: neutron.delete_security_group(group_id),
                lambda: neutron.show_security_group(group_id),
            )

    def allocate_floating_ip(
        self, site: str, *, run_id: uuid.UUID, idempotency_key: str
    ) -> str:
        """Allocate a new public address on demand and record it for teardown."""
        existing = store.resource_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing.provider_id

        with self._ctx(site, "allocate_floating_ip") as d:
            pool = self._floating_ip_pool(d)
            try:
                fip = chi.network.neutron(self._session(site)).create_floatingip(
                    {"floatingip": {"floating_network_id": self._network_id(site, pool)}}
                )["floatingip"]
            except Exception as exc:
                raise self._map_provider_error(exc, "floating_ip", pool) from exc

        address = fip["floating_ip_address"]
        log.info("allocated floating ip site=%s address=%s run_id=%s", site, address, run_id)
        store.create_resource_with_event(
            run_id, address, "floating_ip", site, None, idempotency_key=idempotency_key
        )
        return address

    def delete_floating_ip(self, site: str, address: str) -> None:
        with self._ctx(site, "delete_floating_ip"):
            log.info("deallocating floating ip site=%s address=%s", site, address)
            self._delete(
                "floating_ip", address,
                lambda: chi.network.neutron(self._session(site)).delete_floatingip(
                    self._floating_ip(site, address)["id"]
                ),
                lambda: self._floating_ip(site, address),
            )

    def wait_instance_gone(self, site: str, instance_id: str) -> None:
        """Block until the instance stops being visible."""
        settings = self.settings(site)
        timeout_s, interval = settings.delete_timeout_s, settings.delete_poll_interval_s
        waited = 0.0
        while waited < timeout_s:
            try:
                inst = self.get_instance(site, instance_id)
            except ProviderNotFoundError:
                return
            except TransientError as exc:
                log.debug("waiting for instance %s to go: %s", instance_id, exc)
            else:
                if inst.state in ("DELETED", "SOFT_DELETED"):
                    return
            time.sleep(interval)
            waited += interval
        log.warning(
            "instance %s still visible %.0fs after delete; releasing its group and "
            "address may fail", instance_id, timeout_s,
        )

    def attach_floating_ip(self, site: str, instance_id: str, address: str) -> None:
        with self._ctx(site, "attach_floating_ip"):
            neutron = chi.network.neutron(self._session(site))
            try:
                fip_id = self._floating_ip(site, address)["id"]
                ports = neutron.list_ports(device_id=instance_id)["ports"]
            except Exception as exc:
                raise self._map_provider_error(exc, "floating_ip", address) from exc
            for port in ports:
                try:
                    neutron.update_floatingip(fip_id, {"floatingip": {"port_id": port["id"]}})
                    return
                except Exception as exc:  # noqa: BLE001
                    log.debug("port %s cannot take %s: %s", port["id"], address, exc)
        raise FatalError(
            f"No port on instance {instance_id} can take {address}", code="provider_error"
        )

    def ssh_exec(
        self,
        site: str,
        host: str,
        user: str,
        command: str,
        timeout_s: float,
    ) -> ExecResult:
        """Run one command over SSH with the instance's system key."""
        tagged: str | None = None
        with self._ctx(site, "ssh_exec.precondition"):
            try:
                neutron = chi.network.neutron(self._session(site))
                matches = neutron.list_floatingips(floating_ip_address=host)["floatingips"]
                port_id = matches[0].get("port_id") if matches else None
                if port_id:
                    instance_id = neutron.show_port(port_id)["port"]["device_id"]
                    meta = chi.server.nova(self._session(site)).servers.get(instance_id).metadata
                    tagged = meta.get("cc_system_key")
            except Exception as exc:
                raise self._map_provider_error(exc, "floating_ip", host) from exc
        if not port_id:
            raise PreconditionError("instance_active_with_fip_required")
        if tagged is None:
            raise FatalError(f"{host} was booted without a system key", code="ssh_auth")
        try:
            key = system_key.by_fingerprint(tagged)
        except store.NotFoundError as exc:
            raise FatalError(
                f"{host} trusts system key {tagged}, which the database does not hold",
                code="ssh_auth",
            ) from exc

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                host,
                username=user,
                pkey=key,
                allow_agent=False,
                look_for_keys=False,
                timeout=timeout_s,
                banner_timeout=timeout_s,
                auth_timeout=timeout_s,
            )
            _, stdout, _ = client.exec_command(command, timeout=timeout_s)
            channel = stdout.channel
            out, err = _drain(channel, time.monotonic() + timeout_s)
            if not channel.exit_status_ready():
                raise AdapterTimeoutError(
                    f"the command on {host} did not finish in {timeout_s:g}s",
                    waited_s=timeout_s, last_state="running",
                )
            return ExecResult(
                stdout=out.decode(errors="replace"),
                stderr=err.decode(errors="replace"),
                exit_code=channel.recv_exit_status(),
            )
        except paramiko.AuthenticationException as exc:
            raise FatalError(
                f"SSH authentication failed for {user}@{host}", code="ssh_auth"
            ) from exc
        except paramiko.ssh_exception.NoValidConnectionsError as exc:
            raise TransientError(f"SSH connection refused at {host}") from exc
        except paramiko.SSHException as exc:
            raise TransientError(f"SSH not ready at {host}: {exc}") from exc
        except TimeoutError as exc:
            raise AdapterTimeoutError(
                f"SSH timeout on {host}", waited_s=timeout_s, last_state="connecting"
            ) from exc
        except OSError as exc:
            raise TransientError(
                f"SSH unreachable at {host}: {exc}"
            ) from exc
        finally:
            client.close()

    @staticmethod
    def _status_of(exc: Exception) -> int | None:
        for attr in ("http_status", "status_code", "code"):
            value = getattr(exc, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return value
        return None

    def _is_not_found(self, exc: Exception) -> bool:
        return self._status_of(exc) == 404 or "not found" in str(exc).lower()

    def _delete(
        self, kind: str, ident: str, delete: Callable[[], Any], get: Callable[[], Any]
    ) -> None:
        """Delete at the provider, confirming a not-found delete with a GET."""
        try:
            delete()
        except Exception as exc:
            if not self._is_not_found(exc):
                raise self._map_provider_error(exc, kind, ident) from exc
            try:
                get()
            except Exception:  # noqa: BLE001
                return
            log.warning(
                "delete of %s %s reported not-found but the provider still sees it",
                kind, ident,
            )
            raise FatalError(
                f"Delete of {kind} {ident!r} reported not-found, but it is still "
                f"visible at the provider; refusing to record it released: "
                f"{self._scrub(str(exc))}",
                code="delete_unconfirmed",
            )

    def _instance_network(self, d: SiteDescriptor) -> str:
        if d.instance_network is None:
            raise ConfigError(f"set instance_network for {d.site}")
        return d.instance_network

    def _floating_ip_pool(self, d: SiteDescriptor) -> str:
        if d.floating_ip_pool is None:
            raise ConfigError(f"set floating_ip_pool for {d.site}")
        return d.floating_ip_pool

    def _network_id(self, site: str, name: str) -> str:
        def find() -> str:
            listed = chi.network.neutron(self._session(site)).list_networks(name=name)
            named = [n for n in listed["networks"] if n["name"] == name]
            if not named:
                raise ProviderNotFoundError("network", name)
            if len(named) > 1:
                raise FatalError(f"{len(named)} networks are named {name!r}", code="network")
            return str(named[0]["id"])

        return self._retry_read(site, "network", name, find)

    def _floating_ip(self, site: str, address: str) -> dict[str, Any]:
        """The project's floating IP with this address. Raises ProviderNotFoundError."""
        listed = chi.network.neutron(self._session(site)).list_floatingips(
            floating_ip_address=address
        )["floatingips"]
        if not listed:
            raise ProviderNotFoundError("floating_ip", address)
        return dict(listed[0])

    def _retry_read(self, site: str, kind: str, ident: str, fn: Callable[[], _T]) -> _T:
        attempts = self.settings(site).read_attempts
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:
                mapped = self._map_provider_error(exc, kind, ident)
                if not isinstance(mapped, TransientError) or attempt == attempts:
                    raise mapped from exc
                log.debug(
                    "retrying %s read (attempt %d/%d): %s",
                    kind, attempt, attempts, self._scrub(str(mapped)),
                )
                time.sleep(self.settings(site).read_retry_s)
        raise AssertionError("unreachable")

    def _map_provider_error(self, exc: Exception, kind: str, ident: str) -> Exception:
        """Translate a provider exception into an AdapterError."""
        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, keystoneauth1.exceptions.RetriableConnectionFailure):
            return TransientError(
                f"Connection failure on {kind} {ident!r}: {exc}"
            )
        if self._is_not_found(exc):
            return ProviderNotFoundError(kind, ident)
        status = self._status_of(exc)
        text = str(exc).lower()
        if status in (401, 403) or "unauthorized" in text or "forbidden" in text:
            return FatalError(f"Not authorised for {kind} {ident!r}", code="unauthorized")
        if status in (413, 429) or "quota" in text or "over limit" in text:
            return CapacityError(f"Quota exceeded creating {kind}: {exc}")
        if (
            (status is not None and status >= 500)
            or "timed out" in text
            or "timeout" in text
            or "unavailable" in text
        ):
            return TransientError(f"Provider error on {kind} {ident!r}: {exc}")
        return FatalError(f"Provider error on {kind} {ident!r}: {exc}", code="provider_error")

    def _map_lease_create_error(self, exc: Exception) -> Exception:
        text = str(exc).lower()
        if "not enough" in text or "no available" in text or "not available" in text:
            if any(phrase in text for phrase in _NO_ADDRESS):
                return NoAddressError(f"No floating IP for the requested reservation: {exc}")
            return CapacityError(f"No capacity for the requested reservation: {exc}")
        return self._map_provider_error(exc, "lease", "new")


def instance_error(message: str) -> FatalError:
    """The error for an instance that entered ERROR, typed by Nova's wording."""
    text = message.lower()
    if any(phrase in text for phrase in _UNSCHEDULABLE):
        return UnschedulableError(message)
    if any(phrase in text for phrase in _HOST_UNUSABLE):
        return HostUnusableError(message)
    return FatalError(message, code="instance_error")


def _drain(channel: paramiko.Channel, deadline: float) -> tuple[bytes, bytes]:
    """Read a command's stdout and stderr, up to the deadline and _OUTPUT_BYTES each."""
    out, err = bytearray(), bytearray()
    while time.monotonic() < deadline and max(len(out), len(err)) < _OUTPUT_BYTES:
        if channel.recv_ready():
            out += channel.recv(_OUTPUT_BYTES)
        elif channel.recv_stderr_ready():
            err += channel.recv_stderr(_OUTPUT_BYTES)
        elif channel.exit_status_ready():
            break
        else:
            channel.status_event.wait(min(_READ_POLL_S, deadline - time.monotonic()))
    if not channel.exit_status_ready():
        channel.status_event.wait(max(0.0, deadline - time.monotonic()))
    return bytes(out[:_OUTPUT_BYTES]), bytes(err[:_OUTPUT_BYTES])


def _created_at(image: Any) -> datetime | None:
    """A Glance image's created_at, in UTC."""
    raw = image.get("created_at")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
