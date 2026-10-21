"""Logins for each site, and the adapter built from them."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType

import cloud_conversations.db.store as store
from cloud_conversations import crypt, oidc
from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
from cloud_conversations.adapter.protocol import TestbedAdapter
from cloud_conversations.audit import audited
from cloud_conversations.config import Config, testbed_of
from cloud_conversations.schema.service import Service


class CredentialsError(Exception):
    """There is no usable login for a site."""


@dataclass(frozen=True)
class UserScope:
    """A logged-in user at their testbed, building in one of their projects."""

    owner_id: str
    testbed: str
    project: str | None


def build_adapter(site: str, config: Config, user: UserScope | None = None) -> TestbedAdapter:
    """Build the adapter for this site, logged in as user or as ccx.

    Raises ValueError for an unknown site, and CredentialsError if there is no login for it.
    """
    fake_sites = default_fake_sites()
    if site in fake_sites:
        return audited(FakeAdapter(sites=fake_sites, config=config))

    testbed = config.adapter_for(site)
    retirement = config.retirement(site)
    if testbed is None and retirement is not None:
        raise ValueError(retirement.retired_notice)
    if testbed is None:
        known = ", ".join(sorted([*config.sites(), *fake_sites]))
        raise ValueError(f"unknown site {site!r}; known sites: {known}")
    if user is not None and testbed != user.testbed:
        raise CredentialsError(f"{site} is not a site of {config.label(user.testbed)}")
    package = _package(testbed)
    adapter: TestbedAdapter = package.connect(config, user)
    if not adapter.sites_with_login():
        raise CredentialsError(package.LOGIN_HINT)
    return audited(adapter)


def testbed_adapter(
    config: Config, testbed: str, user: UserScope | None = None
) -> tuple[TestbedAdapter, str | None]:
    """The testbed's adapter, and the reason it has no login if it has none."""
    package = _package(testbed)
    try:
        return audited(package.connect(config, user)), None
    except CredentialsError as exc:
        return audited(package.public(config)), str(exc)


def user_scope(owner_id: str, project: str | None) -> UserScope:
    """The user in project, or in their default project.

    Raises CredentialsError if the user has never logged in.
    """
    try:
        user = store.get_user(owner_id)
    except store.NotFoundError as exc:
        raise CredentialsError("log in to the page first") from exc
    return UserScope(owner_id, user.testbed, project or user.project())


def access_token(owner_id: str, service: Service) -> str:
    """A fresh access token for the user, rotating their stored refresh token.

    Raises CredentialsError if they have not logged in. Ends their sessions and raises
    CredentialsError if the login provider no longer accepts their login.
    """
    with store.transaction() as conn:
        stored = store.locked_refresh_token(owner_id, conn)
        if stored is None:
            raise CredentialsError("log in to the page first")
        try:
            tokens = oidc.refresh(service, oidc.client(testbed_of(owner_id)), crypt.decrypt(stored))
        except oidc.LoginExpired as exc:
            store.end_sessions(owner_id)
            raise CredentialsError("your login has expired; log in to the page again") from exc
        rotated = tokens.get("refresh_token")
        store.save_refresh_token(owner_id, crypt.encrypt(rotated) if rotated else stored, conn)
        logged_in_until = oidc.refresh_expiry(tokens)
        if logged_in_until:
            store.end_sessions_at(owner_id, logged_in_until, service.session_s, conn)
    return str(tokens["access_token"])


def public_adapter(config: Config, testbed: str) -> TestbedAdapter:
    """The testbed's adapter with no login, for catalog and search calls."""
    adapter: TestbedAdapter = _package(testbed).public(config)
    return adapter


def _package(testbed: str) -> ModuleType:
    """Raises ValueError if no package builds this testbed's adapter."""
    name = f"cloud_conversations.adapter.{testbed}"
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
        raise ValueError(f"testbed {testbed!r} has no adapter") from exc
