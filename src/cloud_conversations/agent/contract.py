"""The step the model returns each round."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from cloud_conversations.schema.description import Check, Note, Requirement, Source
from cloud_conversations.schema.search import NodeQuery

MAX_NOTES = 10

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def loads(text: str) -> Any:
    """Parse a model's reply, tolerating a markdown fence around it."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    return json.loads(fenced.group(1) if fenced else stripped)


class ProposedDescription(BaseModel):
    """An answer in the catalog's vocabulary."""

    model_config = {"extra": "forbid"}

    site: str
    start_at: datetime | None = None
    start_source: Source = "default"
    duration_hours: int
    duration_source: Source = "default"
    items: list[Requirement]
    rubric: list[Check]
    unsupported: list[Note] = Field(default=[], max_length=MAX_NOTES)
    open_questions: list[Note] = Field(default=[], max_length=MAX_NOTES)

    def value_of(self, key: str) -> Any:
        return next((item.value for item in self.items if item.key == key), None)


class SearchStep(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["search"]
    query: NodeQuery


class KnowledgeStep(BaseModel):
    """A question for the knowledge base."""

    model_config = {"extra": "forbid"}

    action: Literal["search_knowledge_base"]
    question: Note


class ImageSearchStep(BaseModel):
    """A search of one site's images by name."""

    model_config = {"extra": "forbid"}

    action: Literal["search_images"]
    site: str
    text: Note


class AnswerStep(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["answer"]
    description: ProposedDescription


class DeclineStep(BaseModel):
    """A refusal."""

    model_config = {"extra": "forbid"}

    action: Literal["decline"]
    reason: Note
    suggestions: list[Note] = Field(default=[], max_length=MAX_NOTES)


Step = Annotated[
    SearchStep | KnowledgeStep | ImageSearchStep | AnswerStep | DeclineStep,
    Field(discriminator="action"),
]


# Which part of the system a message belongs to.
Route = Literal["describe", "revise", "build", "inventory", "ask"]


class RouteStep(BaseModel):
    """Where one message goes."""

    model_config = {"extra": "forbid"}

    action: Literal["route"]
    route: Route
    reason: Note
    version: int | None = None


# A read the experiment agent may ask for.
Read = Literal[
    "leases", "instances", "console_log", "availability", "validate"
]


class InspectStep(BaseModel):
    model_config = {"extra": "forbid"}

    action: Literal["inspect"]
    read: Read


class RetryStep(BaseModel):
    """Run the same plan again."""

    model_config = {"extra": "forbid"}

    action: Literal["retry"]
    reason: Note


class ReviseStep(BaseModel):
    """A change to the description."""

    model_config = {"extra": "forbid"}

    action: Literal["revise"]
    change: Note
    reason: Note


class AbandonStep(BaseModel):
    """Release everything and stop."""

    model_config = {"extra": "forbid"}

    action: Literal["abandon"]
    reason: Note


BuildStep = Annotated[
    InspectStep | KnowledgeStep | RetryStep | ReviseStep | AbandonStep,
    Field(discriminator="action"),
]
