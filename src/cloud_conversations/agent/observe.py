"""Render a build for the experiment agent, and run its reads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.config import Settings
from cloud_conversations.db.store import Description, Event, Resource, Run
from cloud_conversations.schema.runs import CheckResult
from cloud_conversations.validator.evaluate import evaluate_rubric
from cloud_conversations.validator.probe import probe_host

if TYPE_CHECKING:
    from cloud_conversations.adapter.protocol import TestbedAdapter

# The run's own history.
_STEP_KINDS = frozenset({
    "step_started",
    "step_succeeded",
    "step_failed",
    "step_retried",
    "resource_created",
    "resource_released",
})


def render(
    desc: Description,
    run: Run,
    events: list[Event],
    resources: list[Resource],
    rounds_left: int,
    leases_left: int,
    max_events: int,
) -> str:
    """The state of one build.

    `events` spans every run of the build. `run` is the last one.
    """
    return "\n".join([
        *_failure(run),
        *_approved(desc),
        *_measured(run),
        *_run_log(run, events, resources, max_events),
        *_history(events),
        "# Budget",
        "",
        f"rounds left: {rounds_left}",
        f"leases left: {leases_left}",
    ])


def read(
    name: str,
    adapter: TestbedAdapter,
    run: Run,
    desc: Description,
    resources: list[Resource],
    settings: Settings,
) -> str:
    """Run one read. A failure comes back as text."""
    site = _site_of(run, desc)
    try:
        if name == "leases":
            return _leases(adapter, site)
        if name == "instances":
            return _instances(adapter, site)
        if name == "console_log":
            return _console_log(adapter, site, resources, settings)
        if name == "availability":
            return _availability(adapter, site, desc)
        return _validate(adapter, site, run, desc, settings)
    except AdapterError as exc:
        return f"that read failed: {exc}"


def _site_of(run: Run, desc: Description) -> str:
    return run.site or desc.requirements.site or ""


def _failure(run: Run) -> list[str]:
    lines = ["# The failure", ""]
    if run.failure is None:
        return [*lines, f"the run is {run.state.value} and records no failure", ""]
    where = (
        f"{run.failure.step_id} {run.failure.action}"
        if run.failure.step_id
        else "no step"
    )
    return [
        *lines,
        f"kind: {run.failure.kind}",
        f"step: {where}",
        f"message: {run.failure.message}",
        "",
    ]


def _approved(desc: Description) -> list[str]:
    req = desc.requirements
    start = req.start_at.isoformat() if req.start_at else "as soon as provisioned"
    lines = [
        "# What was approved",
        "",
        f"site: {req.site}",
        f"start_at: {start}",
        f"duration_hours: {req.duration_hours}",
        "requirements:",
    ]
    lines += [f"  {i.key:<12} {i.value!r:<24} {i.source}" for i in req.items]
    lines.append("rubric:")
    lines += [
        f"  {c.id:<6} {c.kind:<14} {c.comparator:<6} {c.expected!r}" for c in desc.rubric
    ]
    return [*lines, ""]


def _measured(run: Run) -> list[str]:
    """The rubric's results."""
    if not run.results:
        return []
    return ["# What the rubric measured", "", *(f"  {_check_line(r)}" for r in run.results), ""]


def _run_log(
    run: Run, events: list[Event], resources: list[Resource], max_events: int
) -> list[str]:
    steps = [e for e in events if e.run_id == run.run_id and e.kind in _STEP_KINDS]
    lines = ["# This run", "", f"state: {run.state.value}", "events:"]
    lines += [f"  {_event_line(e)}" for e in steps[-max_events:]]
    lines.append("live resources:")
    lines += [f"  {r.kind} {r.provider_id} at {r.site}" for r in resources] or ["  none"]
    return [*lines, ""]


def _event_line(event: Event) -> str:
    fields = " ".join(
        f"{k}={v!r}" for k, v in event.payload.items() if k != "v" and k != "args"
    )
    return f"{event.kind:<20} {fields}"


def _history(events: list[Event]) -> list[str]:
    """The rounds and readings so far."""
    so_far = [e for e in events if e.kind in ("model_turn", "tool_called")]
    if not so_far:
        return []
    lines = ["# So far in this build", ""]
    for event in so_far:
        if event.kind == "model_turn":
            lines.append(f"round {event.payload['round']}: {event.payload['action']}")
            lines += [f"  refused: {r}" for r in event.payload.get("reasons") or []]
        else:
            lines.append(f"  {event.payload['read']}:")
            lines += [f"    {line}" for line in event.payload["result"].splitlines()]
    return [*lines, ""]


def _leases(adapter: TestbedAdapter, site: str) -> str:
    leases = adapter.list_leases(site)
    if not leases:
        return f"{site} holds no leases"
    return "\n".join(
        f"{x.lease_id} {x.state} {x.count}x from {x.start.isoformat()} to {x.end.isoformat()}"
        for x in leases
    )


def _instances(adapter: TestbedAdapter, site: str) -> str:
    instances = adapter.list_instances(site)
    if not instances:
        return f"{site} holds no instances"
    return "\n".join(
        f"{x.instance_id} {x.state}" + (f" fault={x.fault}" if x.fault else "")
        for x in instances
    )


def _console_log(
    adapter: TestbedAdapter, site: str, resources: list[Resource], settings: Settings
) -> str:
    instance = next((r for r in resources if r.kind == "instance"), None)
    if instance is None:
        return "this run holds no instance, so there is no console to read"
    log = adapter.get_console_log(site, instance.provider_id, settings.console_lines)
    return log[-settings.console_chars :] or "the console is empty"


def _availability(adapter: TestbedAdapter, site: str, desc: Description) -> str:
    req = desc.requirements
    node_type = req.value_of("node_type")
    flavor_name = req.value_of("flavor")
    flavor_id = None
    if flavor_name:
        flavor = adapter.find_flavor(site, str(flavor_name))
        if flavor is None:
            return f"no flavor named {flavor_name!r} at {site}"
        flavor_id = flavor.flavor_id

    windows = adapter.published_availability(
        site, node_type, flavor_id, req.duration_hours, start=req.start_at
    )
    unit = node_type or flavor_name
    if not windows:
        return (
            f"the calendar is readable, and no {unit} is free at {site} for "
            f"{req.duration_hours}h in the window the description asks for"
        )
    return "\n".join(
        f"{w.count}x {unit} free at {site} from {w.start.isoformat()} to {w.end.isoformat()}"
        for w in windows
    )


def _validate(
    adapter: TestbedAdapter,
    site: str,
    run: Run,
    desc: Description,
    settings: Settings,
) -> str:
    """Measure the host against the rubric."""
    if run.connection is None:
        return "this run reached no host, so there is nothing to measure"
    facts = probe_host(
        adapter, site, run.connection.host, run.connection.user, settings.probe_s
    )
    return "\n".join(_check_line(r) for r in evaluate_rubric(desc.rubric, facts))


def _check_line(result: CheckResult) -> str:
    mark = "PASS" if result.passed else "FAIL"
    return (
        f"{mark} {result.check_id:<6} {result.kind:<14} "
        f"expected {result.expected!r}, got {result.actual!r}"
    )
