"""Settings, read from a TOML file and resolved for one site."""

from __future__ import annotations

import os
import tomllib
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from cloud_conversations.schema.description import Requirements, ReservationKind
from cloud_conversations.schema.images import ImageRules
from cloud_conversations.schema.knowledge import Knowledge
from cloud_conversations.schema.service import Service

CONFIG_FILE_ENV = "CC_CONFIG_FILE"
_DEFAULT_CONFIG_FILE = Path("etc/config.toml")

OWNER_ENV = "CC_OWNER_ID"

# Comma-separated <testbed>:<username> entries, in place of every testbed's admins.
ADMINS_ENV = "CC_ADMINS"

DEFAULT_DNS_PROBE_HOST = "example.com"
RESOURCE_PREFIX = "cc"

# The User-Agent of every HTTP request Cloud Conversations makes.
USER_AGENT = "cloud-conversations"

MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

_M = TypeVar("_M", bound=BaseModel)


def resource_name(run_id: uuid.UUID) -> str:
    """The provider-side name for every resource a run creates."""
    return f"{RESOURCE_PREFIX}-{run_id}"


def owner_id(config: Config) -> str:
    """Return ccx's user_id.

    Raises ConfigError if CC_OWNER_ID names no testbed, or is unset with other than one.
    """
    owner = os.environ.get(OWNER_ENV)
    if owner is None:
        if len(config.adapters) != 1:
            raise ConfigError(f"set {OWNER_ENV} to <testbed>:<name>, as in chameleon:alice")
        return account_id(next(iter(config.adapters)), "cli")
    if ":" not in owner:
        raise ConfigError(f"{OWNER_ENV} names no testbed; set it to <testbed>:<name>")
    return owner


def account_id(testbed: str, subject: str) -> str:
    return f"{testbed}:{subject}"


def testbed_of(user_id: str) -> str:
    return user_id.partition(":")[0]


class ConfigError(Exception):
    """The config file is missing or invalid."""


class Settings(BaseModel):
    """Tunable values for a run at one site."""

    model_config = {"extra": "forbid", "frozen": True}

    ssh_user: str | None = None
    image_name: str | None = None
    os_release: str | None = None
    baremetal_flavor: str | None = None

    lease_active_s: float = Field(default=1800.0, gt=0)
    instance_active_s: float = Field(default=2400.0, gt=0)
    ssh_s: float = Field(default=600.0, gt=0)
    ssh_poll_interval_s: float = Field(default=10.0, gt=0)
    boot_attempts: int = Field(default=2, ge=1)
    boot_retry_s: float = Field(default=20.0, ge=0)
    reservation_attempts: int = Field(default=2, ge=1)
    address_attempts: int = Field(default=2, ge=1)

    poll_interval_s: float = Field(default=10.0, gt=0)
    delete_timeout_s: float = Field(default=180.0, gt=0)
    delete_poll_interval_s: float = Field(default=5.0, gt=0)
    read_attempts: int = Field(default=3, ge=1)
    read_retry_s: float = Field(default=5.0, ge=0)
    directory_api_s: float = Field(default=15.0, gt=0)
    probe_s: float = Field(default=60.0, gt=0)
    max_lease_hours: int = Field(default=168, ge=1)
    max_gpu_lease_hours: int = Field(default=168, ge=1)
    max_lead_days: int = Field(default=14, ge=0)

    console_lines: int = Field(default=100, ge=1)
    console_chars: int = Field(default=4000, ge=1)

    # Both required, with CC_LLM_API_KEY, to reach a model.
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_temperature: float = Field(
        default=0.0, ge=MIN_TEMPERATURE, le=MAX_TEMPERATURE
    )
    llm_timeout_s: float = Field(default=120.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    max_rounds: int = Field(default=6, ge=1)
    max_candidates_rendered: int = Field(default=40, ge=1)
    max_images_rendered: int = Field(default=10, ge=1)
    max_build_rounds: int = Field(default=4, ge=1)
    max_leases_per_build: int = Field(default=3, ge=1)
    max_events_rendered: int = Field(default=40, ge=1)
    # Deployment policy, appended to the agents' instructions.
    agent_policy: str = ""


class SiteFacts(BaseModel):
    """A site's fixed network and login settings."""

    model_config = {"extra": "forbid", "frozen": True}

    # The network instances boot on.
    instance_network: str | None = None
    # The external network public addresses come from.
    floating_ip_pool: str | None = None
    dns_probe_host: str = DEFAULT_DNS_PROBE_HOST
    # Looked up from the adapter's site index when unset.
    auth_url: str | None = None
    node_catalog_url: str | None = None
    # How the testbed federates a logged-in user's token.
    identity_provider: str | None = None
    federation_protocol: str | None = None
    project_domain: str | None = None


class KindFacts(BaseModel):
    """The properties of one reservation kind at a site."""

    model_config = {"extra": "forbid", "frozen": True}

    reservation_kind: ReservationKind
    uses_security_groups: bool = False


class Retirement(BaseModel):
    model_config = {"extra": "forbid", "frozen": True}

    retires_on: date
    # Leases ending on or after this day get the notice.
    warn_from: date
    notice: str = Field(min_length=1)
    retired_notice: str = Field(min_length=1)

    @model_validator(mode="after")
    def _warns_before_retiring(self) -> Retirement:
        if self.warn_from > self.retires_on:
            raise ValueError("warn_from is later than retires_on")
        return self

    def passed(self) -> bool:
        return self.retires_on <= today()


class LoginClaims(BaseModel):
    """Names of the userinfo claims used at login."""

    model_config = {"extra": "forbid", "frozen": True}

    username_claim: str = "preferred_username"
    projects_claim: str = "projects"
    email_claim: str = "email"


class Adapter(BaseModel):
    """A testbed, and the sites it provides."""

    model_config = {"extra": "allow", "frozen": True}

    label: str | None = None
    # Usernames at the testbed that may manage the system key.
    admins: list[str] = []
    site_index_url: str | None = None
    # Present if users log in through this testbed's OIDC provider.
    login: LoginClaims | None = None
    images: ImageRules = ImageRules()
    kinds: dict[str, dict[str, Any]] = {}
    sites: dict[str, dict[str, Any]] = {}

    @field_validator("admins")
    @classmethod
    def _each_admin_once(cls, admins: list[str]) -> list[str]:
        repeated = sorted({a for a in admins if admins.count(a) > 1})
        if repeated:
            raise ValueError(f"listed more than once: {', '.join(repeated)}")
        if any(not a.strip() for a in admins):
            raise ValueError("an admin is blank")
        return admins

    @model_validator(mode="after")
    def _each_site_names_its_kinds(self) -> Adapter:
        for site, table in self.sites.items():
            kinds = table.get("kinds")
            if not isinstance(kinds, list) or not kinds:
                raise ValueError(f"site {site} lists no kinds")
            unknown = sorted(str(k) for k in kinds if k not in self.kinds)
            if unknown:
                raise ValueError(
                    f"site {site} names kind(s) {', '.join(unknown)}, which are not defined"
                )
            reserved = [self.kinds[k].get("reservation_kind") for k in kinds]
            if len(set(reserved)) != len(reserved):
                raise ValueError(f"site {site} lists a reservation kind more than once")
        return self


class Config(BaseModel):
    model_config = {"frozen": True}

    defaults: Settings = Settings()
    adapters: dict[str, Adapter] = {}
    knowledge: Knowledge = Knowledge()
    service: Service = Service()
    overrides: dict[str, Any] = {}

    def adapter_for(self, site: str) -> str | None:
        return self.sites().get(site)

    def sites(self) -> dict[str, str]:
        """Return every site not yet retired, mapped to its adapter."""
        return {s: name for s, name in self._declared().items() if not self._retired(s)}

    def sites_at(self, testbed: str) -> list[str]:
        return sorted(s for s, name in self.sites().items() if name == testbed)

    def retired_at(self, testbed: str) -> dict[str, str]:
        """Return each retired site of a testbed, mapped to its retired_notice."""
        retired: dict[str, str] = {}
        for site, name in self._declared().items():
            retirement = self.retirement(site)
            if name == testbed and retirement is not None and retirement.passed():
                retired[site] = retirement.retired_notice
        return retired

    def retirement(self, site: str) -> Retirement | None:
        name = self._declared().get(site)
        table = self.adapters[name].sites[site].get("retirement") if name else None
        return Retirement.model_validate(table) if table is not None else None

    def retirement_notice(self, requirements: Requirements) -> str | None:
        """Return the site's notice if the lease ends on or after warn_from."""
        retirement = self.retirement(requirements.site) if requirements.site else None
        if retirement is None:
            return None
        start = requirements.start_at or datetime.now(timezone.utc)
        end = start + timedelta(hours=requirements.duration_hours)
        return retirement.notice if end.date() >= retirement.warn_from else None

    def _declared(self) -> dict[str, str]:
        return {s: name for name, a in self.adapters.items() for s in a.sites}

    def _retired(self, site: str) -> bool:
        retirement = self.retirement(site)
        return retirement is not None and retirement.passed()

    def label(self, testbed: str) -> str:
        adapter = self.adapters.get(testbed)
        return (adapter.label if adapter else None) or testbed

    def logins(self) -> dict[str, LoginClaims]:
        """Each testbed users can log in through."""
        return {name: a.login for name, a in self.adapters.items() if a.login is not None}

    def is_admin(self, testbed: str, username: str) -> bool:
        adapter = self.adapters.get(testbed)
        return adapter is not None and username in adapter.admins

    def for_site(self, site: str | None, kind: ReservationKind | None = None) -> Settings:
        """Return the settings for a site and kind. Without a site, the defaults."""
        merged = self.defaults.model_dump()
        layers = self._layers(site, kind) if site else []
        for layer in [*layers, self.overrides]:
            merged.update(
                {k: v for k, v in layer.items() if k in Settings.model_fields}
            )
        return _validate(Settings, merged, f"settings for {site}")

    def facts_for(self, site: str) -> SiteFacts:
        merged: dict[str, Any] = {}
        for layer in self._layers(site, None):
            merged.update(
                {k: v for k, v in layer.items() if k in SiteFacts.model_fields}
            )
        return _validate(SiteFacts, merged, f"site {site}")

    def kinds_at(self, site: str) -> list[KindFacts]:
        """Return a site's kinds, in the order it lists them."""
        name = self.adapter_for(site)
        if name is None:
            return []
        adapter = self.adapters[name]
        return [
            _validate(KindFacts, _only(adapter.kinds[kind], KindFacts), f"kind {kind}")
            for kind in adapter.sites[site]["kinds"]
        ]

    def _layers(self, site: str, kind: ReservationKind | None) -> list[dict[str, Any]]:
        """Return the tables that apply to a site and kind, weakest first."""
        name = self.adapter_for(site)
        if name is None:
            return []
        adapter = self.adapters[name]
        table = adapter.sites[site]
        layers = [adapter.model_extra or {}]
        if kind is not None:
            matching = [
                adapter.kinds[k] for k in table["kinds"]
                if adapter.kinds[k].get("reservation_kind") == kind
            ]
            if not matching:
                raise ConfigError(f"site {site} has no {kind} kind")
            layers.append(matching[0])
        layers.append(table)
        return layers


def today() -> date:
    return datetime.now(timezone.utc).date()


def load_config(path: Path | None = None, **overrides: Any) -> Config:
    """Read the config file if there is one. An override of None is ignored."""
    if path is None:
        env = os.environ.get(CONFIG_FILE_ENV)
        path = Path(env) if env else _DEFAULT_CONFIG_FILE
        if not env and not path.exists():
            adapters = _adapters({}, "the built-in config")
            return _with_overrides(Config(adapters=adapters), overrides)
    if not path.exists():
        raise ConfigError(f"no config file at {path}")
    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    _reject_unknown(raw, str(path), _TABLES)
    config = Config(
        defaults=_validate(Settings, raw.get("defaults", {}), f"{path} [defaults]"),
        adapters=_adapters(raw.get("adapters", {}), str(path)),
        knowledge=_validate(Knowledge, raw.get("knowledge", {}), f"{path} [knowledge]"),
        service=_validate(Service, raw.get("service", {}), f"{path} [service]"),
    )
    declared_by: dict[str, str] = {}
    for name, adapter in config.adapters.items():
        for kind, table in adapter.kinds.items():
            where = f"{path} [adapters.{name}.kinds.{kind}]"
            _reject_unknown(table, where, _KIND_KEYS)
            _validate(KindFacts, _only(table, KindFacts), where)
        for site, table in adapter.sites.items():
            first = declared_by.setdefault(site, name)
            if first != name:
                raise ConfigError(
                    f'{path}: site "{site}" is declared by both {first} and {name}'
                )
            where = f'{path} [adapters.{name}.sites."{site}"]'
            _reject_unknown(table, where, _SITE_KEYS)
            if "retirement" in table:
                _validate(Retirement, table["retirement"], f"{where} retirement")
        _reject_unknown(adapter.model_extra or {}, f"{path} [adapters.{name}]", _ADAPTER_KEYS)
    try:
        for site in config.sites():
            config.facts_for(site)
            for facts in config.kinds_at(site):
                config.for_site(site, facts.reservation_kind)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return _with_overrides(config, overrides)


# The top-level tables of the config file.
_TABLES = frozenset({"defaults", "adapters", "knowledge", "service"})

# The keys allowed at each level of an adapter's tables.
_ADAPTER_KEYS = frozenset(Settings.model_fields) | frozenset(SiteFacts.model_fields)
_KIND_KEYS = frozenset(Settings.model_fields) | frozenset(KindFacts.model_fields)
_SITE_KEYS = _ADAPTER_KEYS | {"kinds", "retirement"}


def _reject_unknown(table: dict[str, Any], where: str, known: frozenset[str]) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(f"{where}: unknown {'keys' if len(unknown) > 1 else 'key'} "
                          f"{', '.join(unknown)}")


def _adapters(tables: dict[str, Any], where: str) -> dict[str, Adapter]:
    """The [adapters] tables, with CC_ADMINS in place of their admins if that is set."""
    named = os.environ.get(ADMINS_ENV)
    suffix = f" with {ADMINS_ENV}" if named else ""
    if named:
        admins: dict[str, list[str]] = {name: [] for name in tables}
        for entry in (e.strip() for e in named.split(",")):
            testbed, _, username = entry.partition(":")
            if testbed not in admins:
                raise ConfigError(
                    f"{where}{suffix}: {entry!r} names no configured testbed; write it as "
                    f"<testbed>:<username>, with one of: {', '.join(sorted(admins)) or 'none'}"
                )
            admins[testbed].append(username)
        tables = {name: {**table, "admins": admins[name]} for name, table in tables.items()}
    return {
        name: _validate(Adapter, table, f"{where} [adapters.{name}]{suffix}")
        for name, table in tables.items()
    }


def _only(table: dict[str, Any], model: type[BaseModel]) -> dict[str, Any]:
    return {k: v for k, v in table.items() if k in model.model_fields}


def _validate(model: type[_M], data: dict[str, Any], where: str) -> _M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{where}: {exc}") from exc


def _with_overrides(config: Config, overrides: dict[str, Any]) -> Config:
    applied = {k: v for k, v in overrides.items() if v is not None}
    if not applied:
        return config
    _validate(Settings, {**config.defaults.model_dump(), **applied}, "CLI overrides")
    return Config(
        defaults=config.defaults,
        adapters=config.adapters,
        knowledge=config.knowledge,
        service=config.service,
        overrides=applied,
    )
