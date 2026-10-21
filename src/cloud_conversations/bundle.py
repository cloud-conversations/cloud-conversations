"""Prompts and testbed text, read from the bundle directory."""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from cloud_conversations.config import load_config

# The stages a testbed can give guidance for.
GUIDANCE_STAGES = ("hardware", "image")


class BundleError(Exception):
    """A file is missing from the bundle."""


@dataclass(frozen=True)
class Bundle:
    root: Path
    # Testbed name to its label in prompts.
    labels: dict[str, str] = field(default_factory=dict)

    def prompt(self, testbed: str, agent: str, name: str, **values: str) -> str:
        """agents/<agent>/<name>.md, with {testbed}, {about} and each of values filled in."""
        text = self._read(Path("agents", agent, f"{name}.md"))
        filled = {"testbed": self.labels.get(testbed, testbed), "about": self.about(testbed)}
        for key, value in {**filled, **values}.items():
            text = text.replace("{" + key + "}", value)
        return text

    def about(self, testbed: str) -> str:
        return self._read(Path("testbeds", testbed, "about.md")).strip()

    def guidance(self, testbed: str) -> dict[str, str]:
        """The testbed's guidance text, by stage."""
        found = {}
        for stage in GUIDANCE_STAGES:
            path = self.root / "testbeds" / testbed / "guidance" / f"{stage}.md"
            if path.is_file():
                found[stage] = path.read_text().strip()
        return found

    def digest(self) -> str:
        """SHA-256 of every prompt and guidance file."""
        hashed = hashlib.sha256()
        for path in sorted(self.root.rglob("*.md")):
            text = path.read_bytes()
            hashed.update(f"{path.relative_to(self.root)}\0{len(text)}\0".encode())
            hashed.update(text)
        return hashed.hexdigest()

    def _read(self, relative: Path) -> str:
        try:
            return (self.root / relative).read_text()
        except FileNotFoundError as exc:
            raise BundleError(f"the bundle at {self.root} has no {relative}") from exc


@functools.lru_cache(maxsize=1)
def default() -> Bundle:
    """The bundle the config file names, read once per process."""
    config = load_config()
    return Bundle(
        Path(config.service.bundle_dir),
        {name: config.label(name) for name in config.adapters},
    )
