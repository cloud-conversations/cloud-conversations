"""Chameleon's sites, from the config file and its site index."""

from __future__ import annotations

import functools

from cloud_conversations.adapter.chameleon.catalog import fetch_sites
from cloud_conversations.adapter.protocol import Capabilities, SiteDescriptor
from cloud_conversations.config import Config, ConfigError


def load_capabilities(config: Config, testbed: str) -> Capabilities:
    """Return every configured site of the testbed."""
    return Capabilities(
        sites=[_descriptor(site, testbed, config) for site in config.sites_at(testbed)]
    )


def published_sites(config: Config) -> dict[str, str]:
    """Region name to reference-api site id, for each site an index publishes."""
    uids: dict[str, str] = {}
    for site, adapter in config.sites().items():
        published = _published(
            config.adapters[adapter].site_index_url, config.for_site(site).directory_api_s
        ).get(site)
        if published is not None:
            uids[site] = published.uid
    return uids


def reference_api_root(config: Config, sites: list[str]) -> str | None:
    """The API root serving these sites, if exactly one index serves them all."""
    indexes = {
        config.adapters[adapter].site_index_url
        for site, adapter in config.sites().items()
        if site in sites
    }
    roots = {url.rsplit("/sites", 1)[0] for url in indexes if url}
    return roots.pop() if len(roots) == 1 else None


def _descriptor(site: str, adapter: str, config: Config) -> SiteDescriptor:
    facts = config.facts_for(site)
    kinds = config.kinds_at(site)
    auth_url, catalog_url = facts.auth_url, facts.node_catalog_url
    bare_metal = any(k.reservation_kind == "physical_host" for k in kinds)

    if auth_url is None or (catalog_url is None and bare_metal):
        published = _published(
            config.adapters[adapter].site_index_url, config.for_site(site).directory_api_s
        ).get(site)
        if published is not None:
            auth_url = auth_url or published.auth_url
            if bare_metal:
                catalog_url = catalog_url or published.node_catalog_url

    if auth_url is None:
        raise ConfigError(
            f"site {site} has no auth_url, and {adapter}'s site index does not list it"
        )
    return SiteDescriptor(
        site=site,
        auth_url=auth_url,
        node_catalog_url=catalog_url,
        kinds=kinds,
        images=config.adapters[adapter].images,
        **facts.model_dump(exclude={"auth_url", "node_catalog_url"}),
    )


@functools.lru_cache(maxsize=4)
def _published(index_url: str | None, timeout_s: float) -> dict:
    """Fetch an adapter's site index, once per process."""
    return fetch_sites(index_url, timeout_s) if index_url else {}
