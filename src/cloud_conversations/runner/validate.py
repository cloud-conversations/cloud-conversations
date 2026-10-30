"""Measure a run's host against its rubric."""

from __future__ import annotations

import uuid
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.protocol import ExecResult
from cloud_conversations.config import DEFAULT_DNS_PROBE_HOST, Settings
from cloud_conversations.schema.runs import Connection
from cloud_conversations.validator.evaluate import evaluate_rubric
from cloud_conversations.validator.probe import build_probe_script, parse_probe_output
from cloud_conversations.validator.rubric import Check, CheckResult


def probe_host(
    adapter: Any,
    site: str,
    host: str,
    user: str,
    rubric: list[Check],
    settings: Settings,
) -> dict[str, Any]:
    """Return a facts dict for one host, always including ssh_reachable.

    An adapter error returns {"ssh_reachable": False}.
    """
    descriptor = adapter.capabilities().site(site)
    dns_host = descriptor.dns_probe_host if descriptor else DEFAULT_DNS_PROBE_HOST
    try:
        script = build_probe_script(dns_host, settings.venv_dir, rubric)
        result: ExecResult = adapter.ssh_exec(
            site, host, user, script, timeout_s=settings.probe_s
        )
    except AdapterError:
        return {"ssh_reachable": False}
    if result.exit_code != 0:
        return {"ssh_reachable": False}

    facts = parse_probe_output(result.stdout)
    facts["ssh_reachable"] = True
    return facts


def validate_run(
    run_id: uuid.UUID,
    owner_id: str,
    adapter: Any,
    site: str,
    rubric: list[Check],
    connection: Connection,
    settings: Settings,
) -> list[CheckResult]:
    """Probe the host and record the rubric's results."""
    facts = probe_host(adapter, site, connection.host, connection.user, rubric, settings)
    results = evaluate_rubric(rubric, facts)

    for r in results:
        store.append_event(
            run_id,
            "validator",
            "check_observed",
            {
                "check_id": r.check_id,
                "kind": r.kind,
                "target": r.target,
                "expected": r.expected,
                "actual": r.actual,
                "passed": r.passed,
            },
        )

    failed = [r.check_id for r in results if not r.passed]
    store.update_run(owner_id, run_id, results=results)
    store.append_event(
        run_id,
        "validator",
        "validation_completed",
        {"checks": len(results), "failed": failed},
    )
    return results
