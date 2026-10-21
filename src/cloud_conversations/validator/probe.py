"""Collect host facts in one SSH round trip."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.protocol import ExecResult
from cloud_conversations.config import DEFAULT_DNS_PROBE_HOST

# Prints key=value lines describing the host.
_SCRIPT = Path(__file__).with_name("probe.sh")


def build_probe_script(dns_host: str) -> str:
    """The probe, resolving dns_host for the dns_resolves fact."""
    return _SCRIPT.read_text().strip().replace("{dns_host}", dns_host)


_INT_FACTS = frozenset({"cpu_count", "mem_gb", "disk_gb", "gpu_count"})
_BOOL_FACTS = frozenset({"dns_resolves"})


def parse_probe_output(stdout: str) -> dict[str, Any]:
    """Turn `key=value` lines into typed scalars."""
    facts: dict[str, Any] = {}
    for line in stdout.splitlines():
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key or not _:
            continue
        if key in _INT_FACTS:
            try:
                facts[key] = int(raw)
            except ValueError:
                continue
        elif key in _BOOL_FACTS:
            facts[key] = raw.lower() == "true"
        else:
            facts[key] = raw
    return facts


def probe_host(
    adapter: Any,
    site: str,
    host: str,
    user: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Return a facts dict for one host, always including ssh_reachable.

    An adapter error returns {"ssh_reachable": False}.
    """
    descriptor = adapter.capabilities().site(site)
    dns_host = descriptor.dns_probe_host if descriptor else DEFAULT_DNS_PROBE_HOST
    try:
        result: ExecResult = adapter.ssh_exec(
            site, host, user, build_probe_script(dns_host), timeout_s=timeout_s
        )
    except AdapterError:
        return {"ssh_reachable": False}
    if result.exit_code != 0:
        return {"ssh_reachable": False}

    facts = parse_probe_output(result.stdout)
    facts["ssh_reachable"] = True
    return facts
