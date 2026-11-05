"""The step the model returns each round."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from cloud_conversations.schema.description import Note, Requirement, Source
from cloud_conversations.schema.reply import Reply
from cloud_conversations.schema.search import NodeQuery
from cloud_conversations.validator.rubric import Check

MAX_NOTES = 10

_FENCE = re.compile(r"^```(?:json)?\s*\n(.*)\n```$", re.DOTALL)


def loads(text: str) -> Any:
    """Parse a model's reply, tolerating a markdown fence around it."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    return json.loads(fenced.group(1) if fenced else stripped)


class ProposedDescription(Reply):
    """An answer in the catalog's vocabulary."""

    # How the description meets the request, in a few sentences.
    plan: list[Note] = Field(default=[], max_length=MAX_NOTES)
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


class SearchStep(Reply):
    action: Literal["search"]
    query: NodeQuery


class KnowledgeStep(Reply):
    """A question for the knowledge base."""

    action: Literal["search_knowledge_base"]
    question: Note


class ImageSearchStep(Reply):
    """A search of one site's images by name."""

    action: Literal["search_images"]
    site: str
    text: Note


class ReadRepositoryStep(Reply):
    """A read of a repository's setup files."""

    action: Literal["read_repository"]
    url: str
    ref: str | None = None


class AnswerStep(Reply):
    action: Literal["answer"]
    description: ProposedDescription


class DeclineStep(Reply):
    """A refusal."""

    action: Literal["decline"]
    reason: Note
    suggestions: list[Note] = Field(default=[], max_length=MAX_NOTES)


DeclineCase = Literal[
    "no_such_hardware", "retired_site", "no_hardware", "images_not_visible", "set_aside_rules"
]


class DescriptionDecline(DeclineStep):
    """A refusal to describe an experiment."""

    case: DeclineCase


Step = Annotated[
    SearchStep
    | KnowledgeStep
    | ImageSearchStep
    | ReadRepositoryStep
    | AnswerStep
    | DescriptionDecline,
    Field(discriminator="action"),
]


# Which part of the system a message belongs to.
Route = Literal["describe", "revise", "build", "inventory", "ask"]


class RouteStep(Reply):
    """Where one message goes."""

    action: Literal["route"]
    route: Route
    reason: Note
    version: int | None = None


# A read the experiment agent may ask for.
Read = Literal[
    "leases", "instances", "console_log", "install_log", "availability", "validate"
]


class InspectStep(Reply):
    action: Literal["inspect"]
    read: Read


class RetryStep(Reply):
    """Run the same plan again."""

    action: Literal["retry"]
    reason: Note


class ReviseStep(Reply):
    """A change to the description."""

    action: Literal["revise"]
    change: Note
    reason: Note


class AbandonStep(Reply):
    """Release everything and stop."""

    action: Literal["abandon"]
    reason: Note


BuildStep = Annotated[
    InspectStep | KnowledgeStep | RetryStep | ReviseStep | AbandonStep,
    Field(discriminator="action"),
]
