"""What goes wrong in the knowledge base."""

from __future__ import annotations

MISSING_EXTRA = "the knowledge extra is not installed; run: uv sync --extra knowledge"


class KnowledgeError(Exception):
    """The knowledge base is unbuilt, unreadable, or its models cannot be loaded."""


class FetchError(KnowledgeError):
    """A page could not be read. `status` is the HTTP status, if there was one."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
