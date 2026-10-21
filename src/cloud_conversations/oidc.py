"""OIDC login."""

from __future__ import annotations

import functools
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cloud_conversations.config import USER_AGENT
from cloud_conversations.schema.service import Service

# CC_OIDC_<TESTBED>_ISSUER, _CLIENT_ID and _CLIENT_SECRET, for each testbed with a login.
ENV_PREFIX = "CC_OIDC_"


class OidcError(Exception):
    """A call to the login provider failed, or the login is not configured."""


class LoginExpired(OidcError):
    """The login provider no longer accepts the user's refresh token."""


class _Refused(OidcError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Client:
    """The OIDC client users log in through."""

    issuer: str
    client_id: str
    secret: str


def env_names(testbed: str) -> tuple[str, str, str]:
    prefix = f"{ENV_PREFIX}{testbed.upper()}_"
    return f"{prefix}ISSUER", f"{prefix}CLIENT_ID", f"{prefix}CLIENT_SECRET"


def client(testbed: str) -> Client:
    """The testbed's login settings, from the environment.

    Raises OidcError naming every one that is unset or invalid.
    """
    issuer_env, id_env, secret_env = env_names(testbed)
    found = {env: os.environ.get(env, "") for env in (issuer_env, id_env, secret_env)}
    problems = [f"{env} is unset" for env, value in found.items() if not value]
    if found[issuer_env] and not _web_address(found[issuer_env]):
        problems.append(f"{issuer_env} is not an http or https address")
    if problems:
        raise OidcError("; ".join(problems))
    return Client(issuer=found[issuer_env], client_id=found[id_env], secret=found[secret_env])


def exchange_code(
    service: Service, login: Client, code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    return call(
        endpoints(login.issuer, service.oidc_s)["token_endpoint"],
        service.oidc_s,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": login.client_id,
            "client_secret": login.secret,
            "code_verifier": verifier,
        },
    )


def refresh(service: Service, login: Client, refresh_token: str) -> dict[str, Any]:
    """New tokens for a refresh token. Raises LoginExpired if the provider refuses it."""
    try:
        return call(
            endpoints(login.issuer, service.oidc_s)["token_endpoint"],
            service.oidc_s,
            form={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": login.client_id,
                "client_secret": login.secret,
            },
        )
    except _Refused as exc:
        if exc.status == 400:
            raise LoginExpired(str(exc)) from exc
        raise


def userinfo(service: Service, login: Client, access_token: str) -> dict[str, Any]:
    return call(
        endpoints(login.issuer, service.oidc_s)["userinfo_endpoint"],
        service.oidc_s,
        bearer=access_token,
    )


def refresh_expiry(tokens: dict[str, Any]) -> datetime | None:
    """When a token response's refresh token expires, or None if it has no limit."""
    seconds = tokens.get("refresh_expires_in")
    return datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None


@functools.lru_cache(maxsize=4)
def endpoints(issuer: str, timeout_s: float) -> dict[str, Any]:
    """The issuer's published OpenID configuration."""
    published = call(f"{issuer}/.well-known/openid-configuration", timeout_s)
    if published.get("issuer") != issuer:
        raise OidcError(f"{issuer} publishes issuer {published.get('issuer')!r}")
    return published


def call(
    url: str,
    timeout_s: float,
    *,
    form: dict[str, str] | None = None,
    bearer: str | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode() if form is not None else None,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        | ({"Authorization": f"Bearer {bearer}"} if bearer else {}),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as reply:
            body: dict[str, Any] = json.load(reply)
    except urllib.error.HTTPError as exc:
        said = exc.read().decode(errors="replace")[:300]
        raise _Refused(exc.code, f"the login server answered {exc.code} at {url}: {said}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OidcError(f"the login server is unreachable at {url}: {exc}") from exc
    return body


def _web_address(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.netloc)
