"""Sequential run executor."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cloud_conversations.db.store as store
from cloud_conversations import audit
from cloud_conversations.config import Settings, resource_name
from cloud_conversations.environment.install import InstallSeconds
from cloud_conversations.environment.items import SETUP_KEYS
from cloud_conversations.runner.validate import validate_run
from cloud_conversations.schema.description import PLANNER_KEYS, VERIFIABLE_KEYS, Requirements
from cloud_conversations.schema.runs import (
    SETTLED_STATES,
    Failure,
    Plan,
    PlanStep,
    RunState,
)
from cloud_conversations.steps import ACTIONS
from cloud_conversations.steps.context import StepContext, StepError
from cloud_conversations.steps.failure import FailureKind, RetryScope
from cloud_conversations.steps.failure import record as record_failure
from cloud_conversations.steps.retry import record as record_retry

if TYPE_CHECKING:
    from cloud_conversations.adapter.protocol import SiteDescriptor, TestbedAdapter

log = logging.getLogger(__name__)

# Resource kinds, in teardown order.
_TEARDOWN_ORDER = ("instance", "floating_ip", "security_group", "lease")


@dataclass(frozen=True)
class ReleaseOutcome:
    kind: str
    provider_id: str
    success: bool
    error: str | None = None


class PlanError(Exception):
    """Raised when a description cannot be turned into a plan."""


def _fail(
    owner_id: str,
    run_id: uuid.UUID,
    failure: Failure,
) -> None:
    """Record a terminal failure, unless the run is settled."""
    run = store.get_run_unscoped(run_id)
    if run.state in SETTLED_STATES:
        return
    store.update_run(owner_id, run_id, state=RunState.failed, failure=failure)
    store.append_event(
        run_id,
        "runner",
        "run_terminal",
        {
            "state": RunState.failed.value,
            "failure_kind": failure.kind,
            "step_id": failure.step_id,
            "action": failure.action,
        },
        owner_id=owner_id,
    )


def _step_failed(
    owner_id: str,
    run_id: uuid.UUID,
    step_id: str,
    action: str,
    kind: FailureKind,
    message: str,
) -> None:
    """Record a step's failure, and fail the run with it."""
    record_failure(run_id, owner_id, step_id, action, kind, message)
    _fail(owner_id, run_id, Failure(kind=kind, message=message, step_id=step_id, action=action))


def build_plan(
    requirements: Requirements,
    rubric_kinds: frozenset[str],
    run_id: uuid.UUID,
    descriptor: SiteDescriptor,
    plan_seq: int,
    keypair: str,
    install_s: InstallSeconds,
) -> Plan:
    """Build the fixed plan, raising PlanError on an unusable requirement."""
    unusable = [
        item.key
        for item in requirements.items
        if item.key not in PLANNER_KEYS
        and not (item.key in VERIFIABLE_KEYS and item.key in rubric_kinds)
    ]
    if unusable:
        raise PlanError(
            "Requirements the planner cannot act on and no rubric check verifies: "
            f"{sorted(set(unusable))}"
        )

    raw_count = requirements.value_of("count", 1)
    if raw_count is None:
        raw_count = 1
    try:
        count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"count={raw_count!r} is not a number") from exc
    if count != 1:
        raise PlanError(
            f"count={count} is not supported: the plan boots one instance and "
            "runs.connection holds one endpoint"
        )

    node_type = requirements.value_of("node_type")
    flavor = requirements.value_of("flavor")
    image_name = requirements.value_of("image_name")
    os_release = requirements.value_of("os_release")
    if not (image_name or os_release):
        raise PlanError("an image_name or os_release requirement is required to choose an image")

    if node_type and flavor:
        raise PlanError("give one of node_type or flavor, not both")
    key = "node_type" if node_type else "flavor"
    offered = descriptor.reserves_by()
    if not (node_type or flavor):
        raise PlanError(f"{descriptor.site} requires a {' or '.join(offered)} requirement")
    if key not in offered:
        raise PlanError(f"{descriptor.site} takes {' or '.join(offered)}, not {key}")

    actions: list[tuple[str, dict]] = [
        ("ensure_keypair", {"ssh_key_ref": keypair}),
        ("reserve", {
            key: node_type or flavor,
            "duration_hours": requirements.duration_hours,
            "start_at": requirements.start_at.isoformat() if requirements.start_at else None,
        }),
        ("select_image", {"image_name": image_name, "os_release": os_release}),
        ("boot", {"name": resource_name(run_id)}),
        ("assign_ip", {}),
        ("wait_ssh", {}),
    ]
    software = [item.key for item in requirements.items if item.key in SETUP_KEYS]
    if software:
        minutes = install_s.total(software) / 60
        if minutes > requirements.duration_hours * 60:
            raise PlanError(
                f"installing the software can take {minutes:.0f} minutes, longer than the "
                f"{requirements.duration_hours}-hour lease; ask for a longer lease"
            )
        actions.append(("install_software", {}))
    steps = [
        PlanStep(step_id=f"step-{number:04d}", action=action, args=args)
        for number, (action, args) in enumerate(actions, start=1)
    ]

    return Plan(
        plan_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, f"plan:{run_id}:{plan_seq}")),
        site=descriptor.site,
        steps=steps,
    )


class _Retry(Exception):
    """A step failed in a way another resource may fix."""

    scope: RetryScope
    kind: FailureKind
    # The step_retried field naming what was released.
    released: str
    # Log text for the next attempt.
    doing: str

    def __init__(
        self, cause: Exception, step_id: str, action: str, resource: str | None
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.step_id = step_id
        self.action = action
        self.resource = resource


class _RetryAddress(_Retry):
    """SSH never answered on the attached address."""

    scope = "address"
    kind = "step"
    released = "released_address"
    doing = "taking another address"


class _RetryReservation(_Retry):
    """The reserved host cannot boot."""

    scope = "reservation"
    kind = "plan"
    released = "released_lease"
    doing = "reserving again"


def _execute_plan(
    run_id: uuid.UUID,
    owner_id: str,
    adapter: TestbedAdapter,
    plan: Plan,
    requirements: Requirements,
    settings: Settings,
    current_state: RunState,
) -> RunState | None:
    """Drive a plan to completion, returning the state reached or None on failure."""

    def reserved(reservation_attempt: int) -> RunState | None:
        state = current_state if reservation_attempt == 1 else RunState.provisioning
        return _retrying(
            run_id, owner_id, _RetryAddress, settings.address_attempts,
            lambda address, why: _release_address(run_id, adapter, plan.site, address, why),
            lambda address_attempt: _run_steps(
                run_id, owner_id, adapter, plan, requirements, settings,
                state, reservation_attempt, address_attempt,
            ),
        )

    return _retrying(
        run_id, owner_id, _RetryReservation, settings.reservation_attempts,
        lambda lease_id, why: _release_reservation(run_id, adapter, plan.site, lease_id, why),
        reserved,
    )


def _retrying(
    run_id: uuid.UUID,
    owner_id: str,
    retry_on: type[_Retry],
    attempts: int,
    release: Callable[[str | None, str], bool],
    once: Callable[[int], RunState | None],
) -> RunState | None:
    """Run once, releasing the bad resource and trying again while attempts remain."""
    for attempt in range(1, attempts + 1):
        try:
            return once(attempt)
        except retry_on as retry:
            why = f"{retry.action} failed, {retry.doing}: {retry.cause}"
            if attempt == attempts or not release(retry.resource, why):
                _step_failed(
                    owner_id, run_id, retry.step_id, retry.action, retry.kind, str(retry.cause)
                )
                return None
            record_retry(
                run_id, owner_id, retry.step_id, retry.action, retry.scope,
                attempt, attempts, str(retry.cause), **{retry.released: retry.resource},
            )
            log.warning("%s %s, attempt %d of %d", retry.cause, retry.doing, attempt + 1, attempts)
    raise AssertionError("unreachable: the budget is spent on the last attempt")


def _release_address(
    run_id: uuid.UUID,
    adapter: TestbedAdapter,
    site: str,
    address: str | None,
    reason: str,
) -> bool:
    """Give back an address this run allocated. An address from the lease is kept."""
    if address is None:
        return False
    allocated = [
        r for r in store.live_resources(run_id)
        if r.kind == "floating_ip" and r.provider_id == address
    ]
    if not allocated:
        return True
    try:
        with audit.because(reason):
            adapter.delete_floating_ip(site, address)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not release address %s: %s", address, exc)
        return False
    store.release_resource_with_event(run_id, "floating_ip", address, site)
    return True


def _release_reservation(
    run_id: uuid.UUID,
    adapter: TestbedAdapter,
    site: str,
    lease_id: str | None,
    reason: str,
) -> bool:
    """Give the reserved host back."""
    if lease_id is None:
        return False
    try:
        with audit.because(reason):
            adapter.delete_lease(site, lease_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not release lease %s: %s", lease_id, exc)
        return False
    store.release_resource_with_event(run_id, "lease", lease_id, site)
    return True


def _run_steps(
    run_id: uuid.UUID,
    owner_id: str,
    adapter: TestbedAdapter,
    plan: Plan,
    requirements: Requirements,
    settings: Settings,
    current_state: RunState,
    reservation_attempt: int,
    address_attempt: int,
) -> RunState | None:
    """Run every step once, raising a retry if the host or address is bad."""
    outputs: dict = {}

    for plan_step in plan.steps:
        step_def = ACTIONS.get(plan_step.action)
        if step_def is None:
            _fail(
                owner_id,
                run_id,
                Failure(
                    kind="plan",
                    message=f"Unknown action {plan_step.action!r}",
                    step_id=plan_step.step_id,
                    action=plan_step.action,
                ),
            )
            return None

        if step_def.phase != current_state:
            store.update_run(owner_id, run_id, state=step_def.phase)
            current_state = step_def.phase

        ctx = StepContext(
            run_id=run_id,
            owner_id=owner_id,
            site=plan.site,
            adapter=adapter,
            requirements=requirements,
            settings=settings,
            outputs=outputs,
            reservation_attempt=reservation_attempt,
            address_attempt=address_attempt,
        )
        step_args = {**plan_step.args, "step_id": plan_step.step_id}

        try:
            outputs.update(step_def.fn(ctx, step_args))
        except StepError as exc:
            if exc.retry_reservation:
                raise _RetryReservation(
                    exc.cause, plan_step.step_id, plan_step.action,
                    outputs.get("lease_id"),
                ) from exc
            if exc.retry_address:
                raise _RetryAddress(
                    exc.cause, plan_step.step_id, plan_step.action,
                    outputs.get("host"),
                ) from exc
            _fail(
                owner_id,
                run_id,
                Failure(
                    kind=exc.failure_kind,  # type: ignore[arg-type]
                    message=str(exc.cause),
                    step_id=plan_step.step_id,
                    action=plan_step.action,
                ),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            _step_failed(
                owner_id, run_id, plan_step.step_id, plan_step.action, "step",
                f"{type(exc).__name__}: {exc}",
            )
            return None

        store.append_event(
            run_id,
            "runner",
            "step_succeeded",
            {"step_id": plan_step.step_id, "action": plan_step.action},
            owner_id=owner_id,
        )

    return current_state


def _validate_and_settle(
    run_id: uuid.UUID,
    owner_id: str,
    adapter: TestbedAdapter,
    plan: Plan,
    rubric: list,
    settings: Settings,
    current_state: RunState,
) -> None:
    """Run the rubric and write the terminal state.

    `passed` requires runs.results written and every check passed.
    """
    run = store.get_run_unscoped(run_id)
    if run.connection is None:
        _fail(
            owner_id,
            run_id,
            Failure(kind="step", message="plan finished without a connection"),
        )
        return

    if current_state == RunState.provisioning:
        store.update_run(owner_id, run_id, state=RunState.configuring)
        current_state = RunState.configuring
    if current_state != RunState.validating:
        store.update_run(owner_id, run_id, state=RunState.validating)

    try:
        results = validate_run(
            run_id,
            owner_id,
            adapter,
            plan.site,
            rubric,
            run.connection,
            settings,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(
            owner_id,
            run_id,
            Failure(kind="step", message=f"validation failed: {type(exc).__name__}: {exc}"),
        )
        return

    failed = [r.check_id for r in results if not r.passed]
    if failed:
        unreachable = any(
            r.kind == "ssh_reachable" and not r.passed for r in results
        ) or all(r.actual is None for r in results if r.kind != "ssh_reachable")
        kind = "step" if unreachable else "description"
        _fail(
            owner_id,
            run_id,
            Failure(kind=kind, message=f"rubric checks failed: {failed}"),  # type: ignore[arg-type]
        )
        return

    store.update_run(owner_id, run_id, state=RunState.passed, failure=None)
    store.append_event(
        run_id,
        "runner",
        "run_terminal",
        {"state": RunState.passed.value, "checks": len(results)},
        owner_id=owner_id,
    )


def provision(
    owner_id: str,
    description_id: uuid.UUID,
    version: int,
    project_id: str | None,
    keypair: str,
    adapter: TestbedAdapter,
    site: str,
    settings: Settings,
) -> uuid.UUID:
    """Run a description version to completion. Return run_id."""
    desc = store.get_description(owner_id, description_id, version)
    rubric_kinds = frozenset(c.kind for c in desc.rubric)

    run_id = store.create_run(owner_id, description_id, version, project_id)
    store.append_event(run_id, "runner", "run_started", {}, owner_id=owner_id)

    descriptor = adapter.capabilities().site(site)
    if descriptor is None:
        _fail(
            owner_id,
            run_id,
            Failure(kind="description", message=f"Unknown site {site!r}"),
        )
        return run_id

    try:
        plan = build_plan(
            desc.requirements, rubric_kinds, run_id, descriptor, plan_seq=1, keypair=keypair,
            install_s=settings.install_s,
        )
    except PlanError as exc:
        _fail(owner_id, run_id, Failure(kind="description", message=str(exc)))
        return run_id

    store.update_run(owner_id, run_id, plan=plan)
    store.append_event(
        run_id,
        "runner",
        "plan_proposed",
        {"plan_id": plan.plan_id, "plan": plan.model_dump()},
        owner_id=owner_id,
    )

    with audit.on_run(run_id):
        reached = _execute_plan(
            run_id, owner_id, adapter, plan, desc.requirements, settings, RunState.planning
        )
        if reached is not None:
            _validate_and_settle(
                run_id, owner_id, adapter, plan, desc.rubric, settings, reached
            )
    return run_id


def resume(run_id: uuid.UUID, adapter: TestbedAdapter, settings: Settings) -> None:
    """Replay an incomplete or failed run. Completed steps are no-ops."""
    run = store.get_run_unscoped(run_id)
    if run.plan is None:
        raise ValueError(f"Run {run_id} has no plan; cannot resume")
    if run.state in SETTLED_STATES:
        raise ValueError(f"Run {run_id} is {run.state.value}; nothing to resume")

    desc = store.get_description(run.owner_id, run.description_id, run.description_version)
    store.append_event(
        run_id,
        "runner",
        "run_started",
        {"resume": True, "resume_count": run.resume_count + 1},
        owner_id=run.owner_id,
    )
    store.update_run(run.owner_id, run_id, resume_count=run.resume_count + 1)

    first = run.plan.steps[0] if run.plan.steps else None
    first_def = ACTIONS.get(first.action) if first is not None else None
    if first_def is None:
        _fail(
            run.owner_id,
            run_id,
            Failure(
                kind="plan",
                message=(
                    "plan has no steps"
                    if first is None
                    else f"Unknown action {first.action!r}"
                ),
                step_id=first.step_id if first is not None else None,
                action=first.action if first is not None else None,
            ),
        )
        return
    first_phase = first_def.phase
    if run.state != first_phase:
        store.update_run(run.owner_id, run_id, state=first_phase)

    with audit.on_run(run_id):
        reached = _execute_plan(
            run_id, run.owner_id, adapter, run.plan, desc.requirements, settings, first_phase
        )
        if reached is not None:
            _validate_and_settle(
                run_id, run.owner_id, adapter, run.plan, desc.rubric, settings, reached
            )


def teardown(run_id: uuid.UUID, adapter: TestbedAdapter) -> list[ReleaseOutcome]:
    """Release all resources for a run and mark it abandoned.

    A settled run keeps its state. A partly failed teardown leaves it unsettled.
    """
    run = store.get_run_unscoped(run_id)
    with audit.on_run(run_id):
        outcomes = _release_all(run_id, adapter)

    for outcome in outcomes:
        if not outcome.success:
            record_failure(
                run_id, run.owner_id, "teardown", "teardown", "step", outcome.error or "unknown",
                resource_kind=outcome.kind, provider_id=outcome.provider_id,
            )

    if any(not o.success for o in outcomes):
        return outcomes

    abandon(run_id)
    return outcomes


def abandon(run_id: uuid.UUID) -> None:
    """Mark a run that holds nothing abandoned.

    Raises ConflictError if the run holds a resource.
    """
    held = store.live_resources(run_id)
    if held:
        raise store.ConflictError(f"run {run_id} still holds {len(held)} resource(s)")
    run = store.get_run_unscoped(run_id)
    if run.state in SETTLED_STATES:
        return
    store.update_run(run.owner_id, run_id, state=RunState.abandoned)
    store.append_event(
        run_id, "runner", "run_terminal", {"state": RunState.abandoned.value}
    )


def _release_all(run_id: uuid.UUID, adapter: TestbedAdapter) -> list[ReleaseOutcome]:
    """Delete the run's live resources at the provider, recording each release."""
    resources = store.live_resources(run_id)
    outcomes: list[ReleaseOutcome] = []

    for kind in _TEARDOWN_ORDER:
        for r in (x for x in resources if x.kind == kind):
            try:
                if kind == "instance":
                    adapter.delete_instance(r.site, r.provider_id)
                    adapter.wait_instance_gone(r.site, r.provider_id)
                elif kind == "floating_ip":
                    adapter.delete_floating_ip(r.site, r.provider_id)
                elif kind == "security_group":
                    adapter.delete_security_group(r.site, r.provider_id)
                else:
                    adapter.delete_lease(r.site, r.provider_id)
                store.release_resource_with_event(run_id, kind, r.provider_id, r.site)
                outcomes.append(ReleaseOutcome(kind=kind, provider_id=r.provider_id, success=True))
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    ReleaseOutcome(
                        kind=kind,
                        provider_id=r.provider_id,
                        success=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

    for r in resources:
        if r.kind not in _TEARDOWN_ORDER:
            store.release_resource_with_event(run_id, r.kind, r.provider_id, r.site)
            outcomes.append(ReleaseOutcome(kind=r.kind, provider_id=r.provider_id, success=True))

    return outcomes
