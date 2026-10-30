"""The software a description asks to install, and what each value may be."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal, get_args

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter, ValidationError

from cloud_conversations.validator.rubric import HOME_PATH, IMAGE, NPM_NAME, PACKAGE, PIP_NAME

# In install order.
SetupKey = Literal[
    "system_package",
    "repository",
    "python_version",
    "pip_package",
    "node_version",
    "npm_package",
    "docker",
    "container",
]

SETUP_KEYS: tuple[str, ...] = get_args(SetupKey)

_GIT_URL = r"git\+https://[A-Za-z0-9._~:/?#@%+=-]+"
_SPECIFIER = r"(==|!=|<=|>=|~=|<|>)[A-Za-z0-9.*+!_-]+"

PYTHON_VERSION = re.compile(r"3\.[0-9]+")
NODE_VERSION = re.compile(r"[1-9][0-9]*")
# A name, with extras and version specifiers, or a name and a git URL.
PIP_SPEC = re.compile(
    rf"{PIP_NAME.pattern}(\[[A-Za-z0-9._,-]+\])?({_SPECIFIER}(,{_SPECIFIER})*|@{_GIT_URL})?"
)
# A name, with a version or range, or a name and a git URL.
NPM_SPEC = re.compile(rf"{NPM_NAME.pattern}(@([A-Za-z0-9.*^~<>=|_-]+|{_GIT_URL}))?")
REPOSITORY_URL = re.compile(r"https://[A-Za-z0-9._~:/%+-]+")
GIT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _fits(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"{value!r} is not {what}")
        return value

    return AfterValidator(check)


class Docker(BaseModel):
    model_config = {"extra": "forbid"}

    gpu: bool = False


class Container(BaseModel):
    model_config = {"extra": "forbid"}

    image: Annotated[str, _fits(IMAGE, "a container image")]
    # Each is published on the host at the same number.
    ports: list[Annotated[int, Field(ge=1, le=65535)]] = []
    gpu: bool = False


class Repository(BaseModel):
    model_config = {"extra": "forbid"}

    url: Annotated[str, _fits(REPOSITORY_URL, "an https URL")]
    ref: Annotated[str, _fits(GIT_REF, "a branch, tag or commit")]
    path: Annotated[str, _fits(HOME_PATH, "a folder under the home directory")]


_VALUES: dict[str, TypeAdapter[Any]] = {
    "system_package": TypeAdapter(Annotated[str, _fits(PACKAGE, "a system package name")]),
    "repository": TypeAdapter(Repository),
    "python_version": TypeAdapter(Annotated[str, _fits(PYTHON_VERSION, "a version like 3.12")]),
    "pip_package": TypeAdapter(Annotated[str, _fits(PIP_SPEC, "a pip requirement")]),
    "node_version": TypeAdapter(Annotated[str, _fits(NODE_VERSION, "a major version like 22")]),
    "npm_package": TypeAdapter(Annotated[str, _fits(NPM_SPEC, "an npm package")]),
    "docker": TypeAdapter(Docker),
    "container": TypeAdapter(Container),
}


def parse(key: str, value: Any) -> Any:
    """The value as stored. Raises ValueError if it does not suit the key."""
    try:
        parsed = _VALUES[key].validate_python(value)
    except ValidationError as exc:
        raise ValueError(
            "; ".join(str(e["msg"]).removeprefix("Value error, ") for e in exc.errors())
        ) from None
    return parsed.model_dump() if isinstance(parsed, BaseModel) else parsed


# The repository and ref of a pip or npm package from a git URL.
_GIT_SOURCE = re.compile(r"git\+(https://[^@#]+)(?:[@#](.+))?$")


def git_source(key: str, value: Any) -> tuple[str, str | None] | None:
    """The repository URL and ref of a pip or npm package installed from git."""
    if key not in ("pip_package", "npm_package"):
        return None
    found = _GIT_SOURCE.search(str(value))
    return (found.group(1), found.group(2)) if found else None


def target(key: str, value: Any) -> str | None:
    """The target of a check on this item, such as the package name."""
    if key == "system_package":
        return str(value)
    if key == "pip_package":
        return _prefix(PIP_NAME, value)
    if key == "npm_package":
        return _prefix(NPM_NAME, value)
    if key == "container":
        return str(value["image"])
    if key == "repository":
        return str(value["path"])
    return None


# Each setup key that needs another, and the one it needs.
NEEDS: dict[str, str] = {
    "pip_package": "python_version",
    "npm_package": "node_version",
    "container": "docker",
}

# System packages a setup key installs, and that key.
_INSTALLED_BY = [
    (re.compile(r"python3?(\.[0-9]+)?(-pip|-venv)?|pip3?|python-is-python3"), "python_version"),
    (re.compile(r"nodejs|npm|node"), "node_version"),
    (re.compile(r"docker(\.io|-ce|-ce-cli)?|containerd(\.io)?"), "docker"),
]


def installed_by(package: str) -> str | None:
    """The setup key that installs a system package, if one does."""
    return next((key for pattern, key in _INSTALLED_BY if pattern.fullmatch(package)), None)


# The setup key each software check verifies.
VERIFIES: dict[str, str] = {
    "package_installed": "system_package",
    "git_head": "repository",
    "python_version": "python_version",
    "pip_package": "pip_package",
    "node_version": "node_version",
    "npm_package": "npm_package",
    "docker_running": "docker",
    "docker_gpu": "docker",
    "container_running": "container",
    "port_listening": "container",
}


def checks_for(key: str, value: Any) -> list[tuple[str, str | None]]:
    """The check kinds and targets that show an item was installed."""
    if key == "docker":
        return [("docker_running", None), *([("docker_gpu", None)] if value["gpu"] else [])]
    if key == "container":
        ports = [("port_listening", str(port)) for port in value["ports"]]
        return [("container_running", value["image"]), *ports]
    kind = next(k for k, verified in VERIFIES.items() if verified == key)
    return [(kind, target(key, value))]


def _prefix(pattern: re.Pattern[str], value: str) -> str:
    match = pattern.match(value)
    assert match is not None, value
    return match.group(0)


def line(key: str, value: Any) -> str:
    """The installer's line for one item."""
    if key == "docker":
        return f"docker {int(value['gpu'])}"
    if key == "container":
        ports = ",".join(str(p) for p in value["ports"]) or "-"
        return f"container {value['image']} {int(value['gpu'])} {ports}"
    if key == "repository":
        return f"repository {value['url']} {value['ref']} {value['path']}"
    return f"{key} {value}"
