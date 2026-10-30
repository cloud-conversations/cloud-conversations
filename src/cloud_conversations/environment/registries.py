"""Look up packages, container images and git refs where they are published."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.message import Message
from typing import Any

from cloud_conversations.environment import repo
from cloud_conversations.environment.items import git_source, target
from cloud_conversations.environment.repo import COMMIT

log = logging.getLogger(__name__)

_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')
_MANIFESTS = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


@dataclass
class Lookups:
    """Where packages and images are looked up, and the answers so far."""

    pypi_url: str
    npm_registry_url: str
    docker_registry_url: str
    timeout_s: float
    user_agent: str
    # The reason found for each item looked up. None means it is published.
    _found: dict[str, str | None] = field(default_factory=dict, init=False, repr=False)

    def missing(self, items: Sequence[tuple[str, Any]]) -> list[str]:
        """A reason for each package, image or git ref that is not published.

        A registry that does not answer gives no reason, and is asked again next time.
        """
        reasons: list[str] = []
        for key, value in items:
            asked = json.dumps([key, value], sort_keys=True)
            if asked not in self._found:
                try:
                    self._found[asked] = _missing(key, value, self)
                except _Unreachable as exc:
                    log.warning("could not look up %s %s: %s", key, value, exc)
                    continue
            reason = self._found[asked]
            if reason is not None:
                reasons.append(reason)
        return reasons


class _Unreachable(Exception):
    """The registry did not answer."""


def _missing(key: str, value: Any, lookups: Lookups) -> str | None:
    if key == "repository":
        return _repository(value["url"], value["ref"], lookups)
    if key == "container":
        return _image(value["image"], lookups)
    if key not in ("pip_package", "npm_package"):
        return None
    git = git_source(key, value)
    if git is not None:
        return _git_package(*git, lookups)
    name = target(key, value)
    assert name is not None, value
    if key == "pip_package":
        status, _, _ = _get(f"{lookups.pypi_url}/{name}/json", lookups)
        return f"PyPI has no package {name}" if status == 404 else None
    quoted = urllib.parse.quote(name, safe="@")
    status, _, _ = _get(f"{lookups.npm_registry_url}/{quoted}", lookups)
    return f"the npm registry has no package {name}" if status == 404 else None


def _repository(url: str, ref: str, lookups: Lookups) -> str | None:
    """A reason to reject the repository's ref."""
    refs = _refs_at(url, lookups)
    if refs is None:
        return f"there is no public git repository at {url}"
    if COMMIT.fullmatch(ref):
        return None
    if ref not in refs:
        return f"{url} has no branch or tag {ref}"
    return (
        f"the repository ref {ref} is a branch or tag; set the ref, and the expected value "
        f"of its git_head check, to its commit {refs[ref]}"
    )


def _git_package(url: str, ref: str | None, lookups: Lookups) -> str | None:
    refs = _refs_at(url, lookups)
    if refs is None:
        return f"there is no public git repository at {url}"
    if ref is None or COMMIT.fullmatch(ref) or ref in refs:
        return None
    return f"{url} has no branch or tag {ref}"


def _refs_at(url: str, lookups: Lookups) -> dict[str, str] | None:
    try:
        return repo.refs(url, timeout_s=lookups.timeout_s, user_agent=lookups.user_agent)
    except repo.UnreachableError as exc:
        raise _Unreachable(str(exc)) from exc


def _image(image: str, lookups: Lookups) -> str | None:
    registry, repository, reference = _image_parts(image, lookups)
    url = f"{registry}/v2/{repository}/manifests/{reference}"
    status, _, headers = _get(url, lookups, method="HEAD", accept=_MANIFESTS)
    if status == 401:
        token = _token(headers.get("WWW-Authenticate", ""), lookups)
        status, _, _ = _get(url, lookups, method="HEAD", accept=_MANIFESTS, token=token)
    return f"{image} is not published" if status == 404 else None


def _image_parts(image: str, lookups: Lookups) -> tuple[str, str, str]:
    """The registry URL, repository, and tag or digest of an image."""
    name, at, digest = image.partition("@")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, name = f"https://{first}", rest
    else:
        registry = lookups.docker_registry_url
        name = name if "/" in name else f"library/{name}"
    repository, colon, tag = name.rpartition(":")
    if not colon or "/" in tag:
        repository, tag = name, "latest"
    return registry, repository, digest if at else tag


def _token(challenge: str, lookups: Lookups) -> str | None:
    """An anonymous pull token for a Bearer challenge."""
    fields = dict(_CHALLENGE.findall(challenge))
    realm = fields.pop("realm", None)
    if realm is None:
        return None
    status, body, _ = _get(f"{realm}?{urllib.parse.urlencode(fields)}", lookups)
    if status != 200:
        return None
    reply = json.loads(body)
    return reply.get("token") or reply.get("access_token")


def _get(
    url: str,
    lookups: Lookups,
    *,
    method: str = "GET",
    accept: str | None = None,
    token: str | None = None,
) -> tuple[int, bytes, Message]:
    """The status, body and headers of one request. Raises _Unreachable."""
    headers = {"User-Agent": lookups.user_agent}
    if accept is not None:
        headers["Accept"] = accept
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=lookups.timeout_s) as reply:
            return reply.status, reply.read(), reply.headers
    except urllib.error.HTTPError as exc:
        return exc.code, b"", exc.headers
    except (urllib.error.URLError, TimeoutError) as exc:
        raise _Unreachable(str(exc)) from exc
