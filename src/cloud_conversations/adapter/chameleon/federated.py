"""Reaching Chameleon as a logged-in user, with their Keycloak token."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from keystoneauth1 import access, session
from keystoneauth1.identity import v3

from cloud_conversations.adapter.chameleon.adapter import Login
from cloud_conversations.adapter.credentials import UserScope, access_token
from cloud_conversations.config import Config, ConfigError, SiteFacts


class _FreshToken(v3.OidcAccessToken):
    """A federated login that takes a fresh access token each time Keystone asks for one."""

    def __init__(self, fresh: Callable[[], str], **kwargs: Any) -> None:
        super().__init__(access_token="", **kwargs)
        self._fresh = fresh

    def get_unscoped_auth_ref(self, session: session.Session) -> access.AccessInfoV3:
        self.access_token = self._fresh()
        return super().get_unscoped_auth_ref(session)


def user_logins(user: UserScope, config: Config) -> dict[str, Login]:
    """A login as the user, in their project, at each site of their testbed."""
    fresh = functools.partial(access_token, user.owner_id, config.service)
    return {
        site: Login(
            auth=functools.partial(_login_at, fresh, user.project, _federation(config, site))
        )
        for site in config.sites_at(user.testbed)
    }


def _federation(config: Config, site: str) -> SiteFacts:
    facts = config.facts_for(site)
    if None in (facts.identity_provider, facts.federation_protocol, facts.project_domain):
        raise ConfigError(
            f"set identity_provider, federation_protocol and project_domain for {site}"
        )
    return facts


def _login_at(
    fresh: Callable[[], str], project: str | None, facts: SiteFacts, auth_url: str
) -> _FreshToken:
    return _FreshToken(
        fresh,
        auth_url=auth_url,
        identity_provider=facts.identity_provider,
        protocol=facts.federation_protocol,
        project_name=project,
        project_domain_name=facts.project_domain,
    )
