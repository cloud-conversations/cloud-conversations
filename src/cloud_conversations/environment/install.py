"""Install a description's software on a host, in the background."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

from cloud_conversations.environment.items import SETUP_KEYS, line
from cloud_conversations.validator.rubric import HOME_PATH

# Runs a command on the host: (command, timeout_s) -> (exit code, stdout).
Run = Callable[[str, float], tuple[int, str]]

_SETUP = Path(__file__).with_name("setup.sh")
_STATE = "/var/lib/environment-setup"
_UNIT = re.compile(r"[a-z0-9][a-z0-9.-]*")

# "unknown" means the software named does not exist.
FailureKind = Literal["unknown", "error", "timeout"]


def _under_home(value: str) -> str:
    if not HOME_PATH.fullmatch(value):
        raise ValueError(f"{value!r} is not a folder under the home directory")
    return value


HttpsUrl = Annotated[str, Field(pattern=r"^https://\S+$")]
HomeFolder = Annotated[str, AfterValidator(_under_home)]


class InstallSettings(BaseModel):
    """The user to install for, and where the installer downloads from."""

    model_config = {"extra": "forbid", "frozen": True}

    user: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$")
    venv_dir: HomeFolder
    uv_url: HttpsUrl
    node_dist_url: HttpsUrl
    docker_rpm_repo_url: HttpsUrl
    nvidia_toolkit_url: HttpsUrl
    # Seconds to wait for another process to release the package manager's lock.
    install_lock_s: int = Field(ge=1)


class InstallSeconds(BaseModel):
    """Seconds to allow for installing each kind of setup requirement."""

    model_config = {"extra": "forbid", "frozen": True}

    system_package: float = Field(default=300.0, gt=0)
    repository: float = Field(default=300.0, gt=0)
    python_version: float = Field(default=300.0, gt=0)
    pip_package: float = Field(default=600.0, gt=0)
    node_version: float = Field(default=300.0, gt=0)
    npm_package: float = Field(default=300.0, gt=0)
    docker: float = Field(default=900.0, gt=0)
    container: float = Field(default=900.0, gt=0)

    def total(self, keys: Iterable[str]) -> float:
        return sum(getattr(self, key) for key in keys)


class InstallError(Exception):
    """The install failed or ran out of time."""

    def __init__(self, kind: FailureKind, message: str, log: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.log = log


def start_command(unit: str, settings: InstallSettings, items: Sequence[tuple[str, Any]]) -> str:
    """The command that starts the install, unless it is done or running."""
    ordered = sorted(items, key=lambda item: SETUP_KEYS.index(item[0]))
    return "\n".join([
        *_state(unit),
        'status=$(sudo -n cat "$dir/status" 2>/dev/null)',
        '[ "$status" = done ] && exit 0',
        '[ "$status" = running ] && systemctl is-active --quiet "$unit" && exit 0',
        'sudo -n mkdir -p "$dir" || exit 1',
        *_file("setup.sh", _SETUP.read_text().strip()),
        *_file("settings", "\n".join(f"{k} {v}" for k, v in settings.model_dump().items())),
        *_file("items", "\n".join(line(key, value) for key, value in ordered)),
        'echo running | sudo -n tee "$dir/status" >/dev/null || exit 1',
        'sudo -n systemctl reset-failed "$unit" 2>/dev/null',
        'sudo -n systemd-run --quiet --collect --unit "$unit" /bin/bash "$dir/setup.sh" "$dir"',
    ])


def poll_command(unit: str, log_lines: int) -> str:
    """The command that prints the install's status, then the end of its log."""
    return "\n".join([
        *_state(unit),
        'status=$(sudo -n cat "$dir/status" 2>/dev/null)',
        '[ "$status" = running ] && ! systemctl is-active --quiet "$unit" && status=stopped',
        'echo "${status:-missing}"',
        f'sudo -n tail -n {log_lines} "$dir/log" 2>/dev/null',
        "exit 0",
    ])


def _state(unit: str) -> list[str]:
    if not _UNIT.fullmatch(unit):
        raise ValueError(f"{unit!r} is not an install unit name")
    return [f"unit={unit}", f'dir="{_STATE}/$unit"']


def _file(name: str, text: str) -> list[str]:
    end = f"END_{name.upper().replace('.', '_')}"
    return [f"sudo -n tee \"$dir/{name}\" >/dev/null <<'{end}' || exit 1", text, end]


def install(
    run: Run,
    unit: str,
    settings: InstallSettings,
    items: Sequence[tuple[str, Any]],
    *,
    timeout_s: float,
    poll_s: float,
    call_s: float,
    log_lines: int,
    on_poll: Callable[[], None],
) -> None:
    """Start the install, or find it running, and wait for it to finish.

    Raises InstallError if the install fails or outlasts timeout_s.
    """
    deadline = time.monotonic() + timeout_s
    started = False
    log = ""
    while True:
        if not started:
            code, _ = run(start_command(unit, settings, items), call_s)
            started = code == 0
        if started:
            code, out = run(poll_command(unit, log_lines), call_s)
            if code == 0:
                status, _, log = out.partition("\n")
                if status == "done":
                    return
                if status != "running":
                    raise _failure(status, log)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InstallError("timeout", f"install not finished after {timeout_s:.0f}s", log)
        on_poll()
        time.sleep(min(poll_s, remaining))


def _failure(status: str, log: str) -> InstallError:
    missing = status.removeprefix("failed unknown ")
    if missing != status:
        return InstallError("unknown", f"{missing} does not exist", log)
    failed = status.removeprefix("failed error ")
    if failed != status:
        return InstallError("error", f"installing {failed} failed", log)
    return InstallError("error", f"the install ended without finishing: {status}", log)
