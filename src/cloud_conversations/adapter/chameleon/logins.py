"""ccx's OpenStack logins at Chameleon sites, from clouds.yaml or the OS_* variables."""

from __future__ import annotations

import importlib.util
import logging
import os
from typing import Any

from cloud_conversations.adapter.chameleon.adapter import Login
from cloud_conversations.adapter.credentials import CredentialsError
from cloud_conversations.config import Config

log = logging.getLogger(__name__)


def cli_logins(config: Config) -> dict[str, Login]:
    """ccx's login at each configured site."""
    by_site = _clouds_yaml_logins(config, os.environ.get("OS_CLOUD") or None)
    if not by_site:
        single = _environment_login()
        by_site = {} if single is None else dict.fromkeys(config.sites(), single)
    return by_site


def _clouds_yaml_logins(config: Config, only: str | None) -> dict[str, Login]:
    """Each clouds.yaml entry's login, by site."""
    from openstack.config.loader import OpenStackConfig

    try:
        regions = OpenStackConfig().get_all()
    except Exception as exc:  # noqa: BLE001
        log.debug("no clouds.yaml: %s", exc)
        return {}

    if only is not None:
        regions = [r for r in regions if r.name == only]
        if not regions:
            raise CredentialsError(f"cloud {only!r} is not in clouds.yaml")

    known = set(config.sites())
    by_site: dict[str, Login] = {}
    for region in regions:
        site = region.config.get("region_name")
        if site in known:
            by_site[site] = _region_login(region)
        else:
            log.debug("cloud %s names region %r, which is not configured", region.name, site)
    if only is not None and not by_site:
        raise CredentialsError(
            f"cloud {only!r} names no configured site; known sites: "
            f"{', '.join(sorted(known))}"
        )
    return by_site


def _region_login(region: Any) -> Login:
    """The login a clouds.yaml entry describes."""
    cloud = region.name
    auth_type = region.config.get("auth_type")
    if not auth_type:
        raise CredentialsError(f"cloud {cloud!r} names no auth_type")
    if auth_type == "v3chameleonoidc" and importlib.util.find_spec("ccauth") is None:
        raise CredentialsError(
            f"cloud {cloud!r} needs auth_type {auth_type!r}; install the "
            "integration extra, which provides it"
        )
    plugin = region.get_auth()
    if plugin is None:
        raise CredentialsError(f"cloud {cloud!r} has no usable auth")
    auth = region.config.get("auth") or {}
    return Login(
        auth=lambda _auth_url: plugin,
        secrets=tuple(
            auth[k] for k in ("application_credential_secret", "password") if auth.get(k)
        ),
    )


def _environment_login() -> Login | None:
    """The login the OS_* variables describe."""
    from keystoneauth1.identity import v3

    cred_id = os.environ.get("OS_APPLICATION_CREDENTIAL_ID")
    cred_secret = os.environ.get("OS_APPLICATION_CREDENTIAL_SECRET")
    if cred_id and cred_secret:
        log.debug("credentials from OS_APPLICATION_CREDENTIAL_ID")
        return Login(
            auth=lambda auth_url: v3.ApplicationCredential(
                auth_url=auth_url,
                application_credential_id=cred_id,
                application_credential_secret=cred_secret,
            ),
            secrets=(cred_secret,),
        )
    username = os.environ.get("OS_USERNAME")
    password = os.environ.get("OS_PASSWORD")
    project = os.environ.get("OS_PROJECT_NAME")
    if not (username and password and project):
        return None
    return Login(
        auth=lambda auth_url: v3.Password(
            auth_url=auth_url,
            username=username,
            password=password,
            user_domain_name=os.environ.get("OS_USER_DOMAIN_NAME", "default"),
            project_name=project,
            project_domain_name=os.environ.get("OS_PROJECT_DOMAIN_NAME", "chameleon"),
        ),
        secrets=(password,),
    )
