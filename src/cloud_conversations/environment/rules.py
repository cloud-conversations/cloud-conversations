"""The rules a set of software and its checks must meet before it is installed."""

from __future__ import annotations

import json
from collections.abc import Collection, Sequence
from typing import Any
from urllib.parse import urlsplit

from cloud_conversations.environment.items import (
    NEEDS,
    SETUP_KEYS,
    VERIFIES,
    checks_for,
    git_source,
    installed_by,
)
from cloud_conversations.environment.repo import Repository
from cloud_conversations.validator.rubric import Check

# A requirement's key and value.
Item = tuple[str, Any]

# The files that make a repository installable by each installer.
_MANIFESTS = {
    "pip_package": ("pip", ("pyproject.toml", "setup.py", "setup.cfg")),
    "npm_package": ("npm", ("package.json",)),
}


def setup_problems(items: Sequence[Item], checks: Sequence[Check]) -> list[str]:
    """A reason for each piece of software missing a check or a prerequisite.

    Items with keys other than the setup keys are ignored.
    """
    reasons: list[str] = []
    software = [(key, value) for key, value in items if key in SETUP_KEYS]
    keys = [key for key, _ in software]

    for check in checks:
        verified = VERIFIES.get(check.kind)
        if verified is None or check.from_requirement is None:
            continue
        if check.from_requirement != verified:
            reasons.append(
                f"rubric check {check.id!r} is kind {check.kind}, which verifies the "
                f"{verified} requirement; set its from_requirement to {verified}"
            )
        elif verified not in keys:
            reasons.append(
                f"rubric check {check.id!r} reads from_requirement "
                f"{check.from_requirement!r}, which is not a requirement here"
            )

    checked = {(c.kind, c.target): c for c in checks}
    for key, value in software:
        for kind, target in checks_for(key, value):
            if (kind, target) not in checked:
                on = f" with target {target}" if target else ""
                reasons.append(
                    f"the rubric does not check the {key} requirement {_shown(value)}; "
                    f"add a {kind} check{on}"
                )
        if key == "repository":
            head = checked.get(("git_head", value["path"]))
            if head is not None and head.expected != value["ref"]:
                reasons.append(
                    f"set the expected value of rubric check {head.id!r} to the repository's "
                    f"ref, {value['ref']}"
                )
        if key == "container" and value["gpu"] and not any(
            k == "docker" and v["gpu"] for k, v in software
        ):
            reasons.append("a container with GPU access needs a docker requirement with gpu true")
        instead = installed_by(value) if key == "system_package" else None
        if instead is not None:
            reasons.append(f"ask for {value} with a {instead} requirement, not a system_package")

    for key, needed in NEEDS.items():
        if key in keys and needed not in keys:
            reasons.append(f"a {key} requirement needs a {needed} requirement; add one")
    for key in ("python_version", "node_version", "docker"):
        if keys.count(key) > 1:
            reasons.append(f"give one {key} requirement, not {keys.count(key)}")
    return reasons


def repository_problems(
    items: Sequence[Item], read: Sequence[Repository], readable_hosts: Collection[str]
) -> list[str]:
    """A reason for each way the software disagrees with the repositories read."""
    reasons: list[str] = []
    python = next((value for key, value in items if key == "python_version"), None)
    for repository in read:
        wanted = repository.facts.get("python_version")
        if wanted is not None and python is not None and python != wanted.value:
            reasons.append(
                f"{wanted.path} in {repository.url} asks for Python {wanted.value}; "
                f"set python_version to {wanted.value}"
            )

    by_url = {_repository_key(r.url): r for r in read}
    used = {_repository_key(value["url"]) for key, value in items if key == "repository"}
    for key, value in items:
        source = git_source(key, value)
        if source is None:
            continue
        used.add(_repository_key(source[0]))
        origin = by_url.get(_repository_key(source[0]))
        installer, manifests = _MANIFESTS[key]
        if origin is None and urlsplit(source[0]).hostname in readable_hosts:
            reasons.append(f"read {source[0]} before installing {value} from it")
        elif origin is not None and not any(p.lower() in manifests for p in origin.files):
            reasons.append(
                f"{origin.url} has no {_either(manifests)}, so {installer} cannot "
                "install it; clone it with a repository requirement, and say in unsupported "
                "that it is cloned but not built"
            )
    for url, repository in by_url.items():
        if url not in used:
            reasons.append(
                f"the request names {repository.url}; clone it with a repository requirement, "
                "or install a package from it"
            )
    return reasons


def _shown(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _either(names: tuple[str, ...]) -> str:
    return " or ".join(names) if len(names) < 3 else f"{', '.join(names[:-1])} or {names[-1]}"


def _repository_key(url: str) -> str:
    return url.lower().removesuffix("/").removesuffix(".git")
