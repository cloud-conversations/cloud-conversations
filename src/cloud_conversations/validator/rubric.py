"""The rubric's checks and their results."""

from __future__ import annotations

import re
from typing import Any, Literal, get_args

from pydantic import BaseModel, field_validator, model_validator

# Facts the probe reports for every host.
HostKind = Literal[
    "cpu_arch",
    "cpu_count",
    "mem_gb",
    "disk_gb",
    "os_release",
    "kernel",
    "dns_resolves",
    "ssh_reachable",
    "gpu_model",
    "gpu_count",
]

# Facts the probe reports only if the rubric checks them.
SoftwareKind = Literal[
    "package_installed",
    "python_version",
    "pip_package",
    "node_version",
    "npm_package",
    "docker_running",
    "docker_gpu",
    "container_running",
    "port_listening",
    "git_head",
]

CheckKind = Literal[HostKind, SoftwareKind]

SOFTWARE_KINDS: frozenset[str] = frozenset(get_args(SoftwareKind))

Comparator = Literal["eq", "gte", "prefix", "contains", "version_eq", "version_gte"]

PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
PIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
NPM_NAME = re.compile(r"(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
PORT = re.compile(r"[1-9][0-9]{0,4}")
# A path under the home directory.
HOME_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*")

# Each software kind's target pattern, and what to call it in an error.
TARGETS: dict[str, tuple[re.Pattern[str], str]] = {
    "package_installed": (PACKAGE, "a system package name"),
    "pip_package": (PIP_NAME, "a Python package name"),
    "npm_package": (NPM_NAME, "an npm package name"),
    "container_running": (IMAGE, "a container image"),
    "port_listening": (PORT, "a port number"),
    "git_head": (HOME_PATH, "a folder under the home directory"),
}


class Check(BaseModel):
    """One rubric entry: a verifiable condition that defines run success."""

    id: str
    kind: CheckKind
    expected: Any
    comparator: Comparator
    from_requirement: str | None = None
    # What a software check measures, such as a package name.
    target: str | None = None

    @field_validator("target", mode="before")
    @classmethod
    def _number_as_text(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def _target_fits_kind(self) -> Check:
        if self.kind not in TARGETS:
            if self.target is not None:
                raise ValueError(f"a {self.kind} check takes no target; remove it")
            return self
        pattern, what = TARGETS[self.kind]
        if self.target is None or not pattern.fullmatch(self.target):
            raise ValueError(f"set the {self.kind} check's target to {what}")
        return self

    def fact(self) -> str:
        """The probe's name for the fact this check reads."""
        return fact_name(self.kind, self.target)


def fact_name(kind: str, target: str | None) -> str:
    return kind if target is None else f"{kind} {target}"


class CheckResult(BaseModel):
    """Result of one rubric check, stored in the runs.results JSONB array."""

    check_id: str
    kind: str
    target: str | None = None
    expected: Any
    actual: Any
    passed: bool
