"""The facts the probe script prints."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cloud_conversations.validator.probe import build_probe_script, parse_probe_output


def _stub(directory: Path, name: str, output: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\n")
    path.chmod(0o755)


def test_memory_and_disk_are_read_in_the_catalogs_units(tmp_path):
    _stub(tmp_path, "lsmem", "Total online memory:       8589934592")
    _stub(tmp_path, "lsblk", "480103981056 disk\n1000000000 part\n240057409536 disk")

    probed = subprocess.run(
        ["sh", "-c", build_probe_script("example.org", "venv", [])],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}"},
    )

    facts = parse_probe_output(probed.stdout)
    assert (facts["mem_gb"], facts["disk_gb"]) == (8, 480)
