"""Collect host facts in one SSH round trip."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cloud_conversations.validator.rubric import SOFTWARE_KINDS, Check

# Prints key=value lines describing the host.
_SCRIPT = Path(__file__).with_name("probe.sh")
# Prints a line for each software check it reads.
_SOFTWARE = Path(__file__).with_name("software.sh")
_END = "END_CHECKS"


def build_probe_script(dns_host: str, venv_dir: str, rubric: list[Check]) -> str:
    """The probe, resolving dns_host, with the rubric's software checks."""
    script = _SCRIPT.read_text().strip().replace("{dns_host}", dns_host)
    software = [c for c in rubric if c.kind in SOFTWARE_KINDS]
    if not software:
        return script
    loop = _SOFTWARE.read_text().strip().replace("{venv_dir}", venv_dir)
    targets = [f"{c.kind} {c.target or '-'}" for c in software]
    return "\n".join([script, f"{loop} 3<<'{_END}'", *targets, _END])


_INT_FACTS = frozenset({"cpu_count", "mem_gb", "disk_gb", "gpu_count"})
_BOOL_FACTS = frozenset({
    "dns_resolves",
    "package_installed",
    "docker_running",
    "docker_gpu",
    "container_running",
    "port_listening",
})


def parse_probe_output(stdout: str) -> dict[str, Any]:
    """Turn `key=value` lines into typed scalars."""
    facts: dict[str, Any] = {}
    for line in stdout.splitlines():
        key, _, raw = line.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key or not _:
            continue
        kind = key.partition(" ")[0]
        if kind in _INT_FACTS:
            try:
                facts[key] = int(raw)
            except ValueError:
                continue
        elif kind in _BOOL_FACTS:
            facts[key] = raw.lower() == "true"
        elif raw or kind not in SOFTWARE_KINDS:
            facts[key] = raw
    return facts
