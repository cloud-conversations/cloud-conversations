"""Installing a description's software on a host, and checking it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

import cloud_conversations.db.store as store
from cloud_conversations.adapter.fake import FAKE_KEYPAIR
from cloud_conversations.adapter.protocol import ExecResult
from cloud_conversations.agent import observe
from cloud_conversations.config import Settings
from cloud_conversations.environment import install as installer
from cloud_conversations.environment.install import InstallSeconds
from cloud_conversations.environment.items import SETUP_KEYS, parse, target
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import Requirement
from cloud_conversations.schema.runs import RunState
from cloud_conversations.steps.context import finishing_by
from cloud_conversations.validator.probe import build_probe_script, parse_probe_output
from cloud_conversations.validator.rubric import SOFTWARE_KINDS, Check
from tests.conftest import BM_SITE, FAST

_SETUP = Path(installer.__file__).with_name("setup.sh")


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _path(directory: Path) -> dict[str, str]:
    return {**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"}


def test_a_port_check_takes_its_port_as_a_number():
    check = Check.model_validate(
        {"id": "p", "kind": "port_listening", "target": 8888, "expected": True, "comparator": "eq"}
    )
    assert check.target == "8888"


def test_a_check_target_cannot_carry_a_command():
    with pytest.raises(ValidationError, match="set the package_installed check's target"):
        Check(
            id="jq", kind="package_installed", target="jq; rm -rf ~", expected=True,
            comparator="eq",
        )


def test_the_probe_reports_each_software_check(tmp_path):
    _stub(tmp_path, "dpkg-query", '[ "$3" = jq ] && printf "install ok installed"')
    rubric = [
        Check(id="jq", kind="package_installed", target="jq", expected=True, comparator="eq"),
        Check(id="curl", kind="package_installed", target="curl", expected=True, comparator="eq"),
    ]

    probed = subprocess.run(
        ["sh", "-c", build_probe_script("example.org", "venv", rubric)],
        capture_output=True, text=True, check=False, env=_path(tmp_path),
    )

    facts = parse_probe_output(probed.stdout)
    assert (facts["package_installed jq"], facts["package_installed curl"]) == (True, False)


def test_a_pip_package_cannot_be_an_editable_install():
    with pytest.raises(ValueError, match="is not a pip requirement"):
        parse("pip_package", "-e .")


def test_a_pip_package_from_git_is_checked_by_its_name():
    value = parse("pip_package", "tqdm@git+https://github.com/tqdm/tqdm@v4.66.4")
    assert target("pip_package", value) == "tqdm"


def _installer(tmp_path: Path, items: list[str], *, installed: str) -> tuple[str, str]:
    """Run setup.sh with apt stubbed, and return its status and the commands it ran."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    calls = tmp_path / "calls"
    _stub(stubs, "getent", f'echo "alice:x:1000:1000::{tmp_path}:/bin/sh"')
    _stub(stubs, "dpkg-query", f'[ "$3" = {installed} ] && printf "install ok installed"')
    _stub(stubs, "apt-cache", '[ "$2" != nosuch ]')
    _stub(stubs, "apt-get", f'echo "apt-get $@" >>{calls}')
    _stub(stubs, "cloud-init", f'echo "cloud-init $@" >>{calls}')
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings").write_text("user alice\nvenv_dir venv\ninstall_lock_s 600\n")
    (state / "items").write_text("\n".join(items) + "\n")

    subprocess.run(["bash", str(_SETUP), str(state)], check=False, env=_path(stubs))

    made = calls.read_text() if calls.exists() else ""
    return (state / "status").read_text().strip(), made


def test_the_installer_reports_a_package_that_does_not_exist(tmp_path):
    status, _ = _installer(tmp_path, ["system_package nosuch"], installed="none")
    assert status == "failed unknown system_package nosuch"


def test_the_installer_leaves_an_installed_package_alone(tmp_path):
    status, calls = _installer(tmp_path, ["system_package jq"], installed="jq")
    assert (status, calls) == ("done", "cloud-init status --wait\n")


def test_the_installer_waits_for_cloud_init_and_for_apt_s_lock(tmp_path):
    _, calls = _installer(tmp_path, ["system_package jq"], installed="none")
    assert calls.splitlines() == [
        "cloud-init status --wait",
        "apt-get -o DPkg::Lock::Timeout=600 update",
        "apt-get -o DPkg::Lock::Timeout=600 install -y jq",
    ]


def test_a_yum_package_looked_up_while_the_mirrors_are_down_is_an_error(tmp_path):
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for tool in ("cut", "mktemp", "uname", "rm"):
        found = shutil.which(tool)
        assert found is not None, tool
        (stubs / tool).symlink_to(found)
    _stub(stubs, "getent", f'echo "alice:x:1000:1000::{tmp_path}:/bin/sh"')
    _stub(stubs, "rpm", "exit 1")
    _stub(stubs, "dnf", "exit 1")
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings").write_text("user alice\nvenv_dir venv\n")
    (state / "items").write_text("system_package jq\n")

    subprocess.run(
        ["/bin/bash", str(_SETUP), str(state)], check=False, env={"PATH": str(stubs)}
    )

    assert (state / "status").read_text().strip() == "failed error system_package jq"


# A tar that unpacks a uv which always fails.
_FAILING_UV = (
    'd="$4/uv-x86_64-unknown-linux-gnu"; mkdir -p "$d"; '
    "printf '#!/bin/sh\\nexit 1\\n' >\"$d/uv\"; chmod 755 \"$d/uv\""
)


def _python_installer(tmp_path: Path) -> tuple[str, str]:
    """Install Python 3.11 as alice, with downloads stubbed and a uv that fails.

    Returns the status, and the commands run as alice.
    """
    stubs = tmp_path / "bin"
    stubs.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    as_alice = tmp_path / "as-alice"
    _stub(stubs, "getent", f'echo "alice:x:1000:1000::{home}:/bin/sh"')
    _stub(stubs, "curl", 'while [ "$1" != -o ]; do shift; done; touch "$2"')
    _stub(stubs, "sha256sum", "true")
    _stub(stubs, "cloud-init", "true")
    _stub(stubs, "tar", _FAILING_UV)
    _stub(stubs, "install", 'cp "$5" "$6" && chmod 755 "$6"')
    _stub(
        stubs, "runuser",
        f'echo "$@" >>{as_alice}; while [ "$1" != -- ]; do shift; done; shift; "$@"',
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "settings").write_text("user alice\nvenv_dir venv\nuv_url https://example.org/uv\n")
    (state / "items").write_text("python_version 3.11\n")

    subprocess.run(["bash", str(_SETUP), str(state)], check=False, env=_path(stubs))

    return (state / "status").read_text().strip(), as_alice.read_text()


def test_uv_s_folders_are_made_by_the_user_it_runs_as(tmp_path):
    _, as_alice = _python_installer(tmp_path)
    assert f"mkdir -p {tmp_path}/home/.local/bin" in as_alice


def test_a_python_install_that_fails_is_an_error_to_retry(tmp_path):
    status, _ = _python_installer(tmp_path)
    assert status == "failed error python_version 3.11"


_INSTALL_SETTINGS = installer.InstallSettings(
    user="alice",
    venv_dir="venv",
    uv_url="https://example.org/uv",
    node_dist_url="https://example.org/node",
    docker_rpm_repo_url="https://example.org/docker-ce.repo",
    nvidia_toolkit_url="https://example.org/nvidia",
    install_lock_s=600,
)


def test_the_install_script_reads_every_install_setting():
    script = _SETUP.read_text()
    assert [f for f in installer.InstallSettings.model_fields if f"    {f}) " not in script] == []


def test_an_install_that_is_running_is_waited_for_and_started_once():
    sent: list[str] = []
    polls = iter(["running", "running", "done"])

    def run(command: str, timeout_s: float) -> tuple[int, str]:
        sent.append(command)
        return (0, "") if "<<'END_ITEMS'" in command else (0, next(polls))

    installer.install(
        run, "cc-setup-0123456789abcdef", _INSTALL_SETTINGS, [("system_package", "jq")],
        timeout_s=10, poll_s=0, call_s=1, log_lines=5, on_poll=lambda: None,
    )

    assert sum("<<'END_ITEMS'" in c for c in sent) == 1
    assert len(sent) == 4


def test_an_install_that_stopped_is_an_error_to_retry():
    def run(command: str, timeout_s: float) -> tuple[int, str]:
        return (0, "") if "<<'END_ITEMS'" in command else (0, "stopped")

    with pytest.raises(installer.InstallError) as raised:
        installer.install(
            run, "cc-setup-0123456789abcdef", _INSTALL_SETTINGS, [("system_package", "jq")],
            timeout_s=10, poll_s=0, call_s=1, log_lines=5, on_poll=lambda: None,
        )

    assert (raised.value.kind, str(raised.value)) == (
        "error", "the install ended without finishing: stopped"
    )


def test_install_times_cover_every_setup_key():
    assert set(InstallSeconds.model_fields) == set(SETUP_KEYS)


def _with(requirements, *items: Requirement, hours: int = 4):
    return requirements.model_copy(
        update={"duration_hours": hours, "items": [*requirements.items, *items]}
    )


_JQ = Requirement(key="system_package", value="jq", source="stated")
_JQ_CHECK = Check(
    id="jq", kind="package_installed", target="jq", expected=True, comparator="eq",
    from_requirement="system_package",
)


def test_a_lease_shorter_than_the_installs_is_refused(bm_requirements, fake_adapter):
    container = Requirement(
        key="container", value={"image": "nginx:1.27"}, source="stated"
    )
    docker = Requirement(key="docker", value={"gpu": False}, source="stated")

    with pytest.raises(sequential.PlanError, match="longer than the 1-hour lease"):
        sequential.build_plan(
            _with(bm_requirements, docker, container, hours=1), frozenset(), uuid.uuid4(),
            fake_adapter.capabilities().site(BM_SITE), 1, keypair=FAKE_KEYPAIR,
            install_s=InstallSeconds(docker=2400, container=2400),
        )


def _provision(owner, requirements, rubric, adapter):
    did, ver = store.create_description(owner, "conv-software", requirements, rubric)
    return sequential.provision(
        owner, did, ver, None, FAKE_KEYPAIR, adapter, BM_SITE, FAST.defaults
    )


def _installs_end(monkeypatch, adapter, status: str) -> None:
    """Make every install on the fake report status."""
    real = adapter.ssh_exec

    def ssh_exec(site, host, user, command, timeout_s):
        if 'echo "${status:-missing}"' in command:
            return ExecResult(stdout=status, stderr="", exit_code=0)
        return real(site, host, user, command, timeout_s)

    monkeypatch.setattr(adapter, "ssh_exec", ssh_exec)


def test_a_package_that_does_not_exist_is_a_description_failure(
    owner_a, bm_requirements, bm_rubric, fake_adapter, monkeypatch
):
    _installs_end(monkeypatch, fake_adapter, "failed unknown system_package jq")
    requirements = _with(bm_requirements, _JQ)

    run_id = _provision(owner_a, requirements, [*bm_rubric, _JQ_CHECK], fake_adapter)

    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert (run.failure.kind, run.failure.action) == ("description", "install_software")


def test_an_install_cut_off_by_the_job_s_time_limit_can_be_resumed(
    owner_a, bm_requirements, bm_rubric, fake_adapter, monkeypatch
):
    _installs_end(monkeypatch, fake_adapter, "running")
    requirements = _with(bm_requirements, _JQ)

    with finishing_by(time.monotonic() + 2):
        run_id = _provision(owner_a, requirements, [*bm_rubric, _JQ_CHECK], fake_adapter)

    run = store.get_run(owner_a, run_id)
    assert run.failure is not None
    assert run.failure.message == (
        "the build ran out of time with the install still running; resume the run to keep "
        "waiting for it"
    )


def test_the_install_log_read_asks_about_the_run_s_own_install(
    owner_a, bm_requirements, bm_rubric, fake_adapter
):
    requirements = _with(bm_requirements, _JQ)
    run_id = _provision(owner_a, requirements, [*bm_rubric, _JQ_CHECK], fake_adapter)
    run = store.get_run(owner_a, run_id)
    desc = store.get_description(owner_a, run.description_id, run.description_version)

    read = observe.read("install_log", fake_adapter, run, desc, [], Settings())

    assert read == "done"


def test_the_page_groups_every_setup_key_and_software_check():
    page = (Path(__file__).parent.parent / "web" / "src" / "software.ts").read_text()
    groups = page[page.index("export const SOFTWARE_GROUPS"):]
    groups = groups[: groups.index("];")]

    def listed(field: str) -> set[str]:
        lists = re.findall(rf"{field}: \[([^\]]*)\]", groups)
        return set(re.findall(r'"([a-z_]+)"', " ".join(lists)))

    assert (set(SETUP_KEYS) - listed("keys"), SOFTWARE_KINDS - listed("kinds")) == (set(), set())
