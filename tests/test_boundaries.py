"""Folders meant to be lifted out import only the Cloud Conversations folders listed here."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src" / "cloud_conversations"


def _ours(folder: Path) -> set[str]:
    """The top-level Cloud Conversations packages a folder imports."""
    modules: set[str] = set()
    for path in folder.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module == "cloud_conversations":
                modules.update(f"cloud_conversations.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules.add(node.module)
    return {m.split(".")[1] for m in modules if m.startswith("cloud_conversations.")}


@pytest.mark.parametrize(
    ("folder", "allowed"),
    [("validator", {"validator"}), ("environment", {"environment", "validator"})],
)
def test_a_folder_imports_only_what_it_may(folder, allowed):
    assert _ours(_SRC / folder) - allowed == set()
