"""The docs assistant: one question, answered from the knowledge base."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from cloud_conversations import bundle
from cloud_conversations.agent.turns import Round
from cloud_conversations.knowledge.retrieve import (
    PRIMARY_SECTION,
    SUPPLEMENTARY_SECTION,
    Retrieved,
    Retriever,
)
from cloud_conversations.llm.client import Model
from cloud_conversations.schema.knowledge import Knowledge


class Answer(BaseModel):
    """The answer, and the passages it came from."""

    text: str
    sources: list[str]
    retrieved: Retrieved
    turn: Round


def answer(
    client: Model,
    base: Retriever,
    question: str,
    history: list[tuple[str, str]],
    *,
    testbed: str,
) -> Answer:
    """Answer one question from the knowledge base.

    Raises KnowledgeError if the knowledge base cannot be searched, and
    LLMError if the model endpoint cannot be reached.
    """
    found = base.retrieve(question)
    messages = [{"role": "system", "content": _system_prompt(testbed, base.knowledge)}]
    for asked, said in _recent(history, base.knowledge.max_history_exchanges):
        messages.append({"role": "user", "content": asked})
        messages.append({"role": "assistant", "content": said})
    messages.append({
        "role": "user",
        "content": f"Question: {question}\nContext: {found.context}",
    })

    reply = client.complete(messages)
    return Answer(
        text=reply.text,
        sources=found.sources,
        retrieved=found,
        turn=Round.of(1, "answer", reply, [], messages),
    )


def as_turn(given: Answer) -> dict[str, Any]:
    """The answer as a stored transcript entry."""
    return {**given.turn.model_dump(mode="json"), "sources": given.sources}


def _system_prompt(testbed: str, knowledge: Knowledge) -> str:
    """The instructions, then the deployment's own rules."""
    instructions = bundle.default().prompt(
        testbed,
        "answer",
        "instructions",
        primary_section=PRIMARY_SECTION,
        supplementary_section=SUPPLEMENTARY_SECTION,
    )
    policy = knowledge.answer_policy.strip()
    return f"{instructions}\nAlso:\n\n{policy}" if policy else instructions


def _recent(history: list[tuple[str, str]], exchanges: int) -> list[tuple[str, str]]:
    """The last few exchanges, oldest first."""
    return history[-exchanges:] if exchanges > 0 else []
