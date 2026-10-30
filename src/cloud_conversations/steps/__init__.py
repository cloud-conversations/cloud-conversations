"""Action registry: each action maps to a step function and the state it runs in."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cloud_conversations.schema.runs import RunState
from cloud_conversations.steps.context import StepContext
from cloud_conversations.steps.provision import (
    assign_ip,
    boot,
    ensure_keypair,
    install_software,
    reserve,
    select_image,
    wait_ssh,
)

StepFn = Callable[[StepContext, dict], dict[str, Any]]


@dataclass(frozen=True)
class StepDef:
    fn: StepFn
    phase: RunState


ACTIONS: dict[str, StepDef] = {
    "reserve": StepDef(reserve, RunState.provisioning),
    "select_image": StepDef(select_image, RunState.provisioning),
    "ensure_keypair": StepDef(ensure_keypair, RunState.provisioning),
    "boot": StepDef(boot, RunState.provisioning),
    "assign_ip": StepDef(assign_ip, RunState.provisioning),
    "wait_ssh": StepDef(wait_ssh, RunState.configuring),
    "install_software": StepDef(install_software, RunState.configuring),
}
