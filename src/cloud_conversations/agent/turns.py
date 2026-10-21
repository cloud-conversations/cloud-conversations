"""The rounds of a conversation with the model, and the loop that runs them."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, Self, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from cloud_conversations import bundle
from cloud_conversations.agent.contract import loads
from cloud_conversations.llm.client import LLMError, Model, Reply

S = TypeVar("S")
T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
R = TypeVar("R", bound="Round")


class Round(BaseModel):
    round: int
    action: str
    text: str
    usage: dict[str, int]
    finish_reason: str
    reasons: list[str]
    # The messages sent since the previous reply.
    sent: list[dict[str, str]]
    model: str
    served_model: str
    temperature: float | None
    bundle_hash: str

    @classmethod
    def of(
        cls,
        number: int,
        action: str,
        reply: Reply,
        reasons: list[str],
        sent: list[dict[str, str]],
        **fields: Any,
    ) -> Self:
        return cls(
            round=number,
            action=action,
            text=reply.text,
            usage=reply.usage,
            finish_reason=reply.finish_reason,
            reasons=reasons,
            sent=sent,
            model=reply.model,
            served_model=reply.served_model,
            temperature=reply.temperature,
            bundle_hash=bundle.default().digest(),
            **fields,
        )

    @classmethod
    def failed(cls, number: int, exc: LLMError, sent: list[dict[str, str]]) -> Self:
        return cls(
            round=number, action="error", text="", usage={}, finish_reason="",
            reasons=[str(exc)], sent=sent, model="", served_model="", temperature=None,
            bundle_hash=bundle.default().digest(),
        )


@dataclass(frozen=True)
class Turn(Generic[T_co]):
    """An agent's verdict on one parsed reply."""

    action: str
    reasons: list[str]
    # Set if the conversation ends here.
    answer: T_co | None = None
    # What the model is told next. None tells it the reasons.
    told: str | None = None
    # Further fields of the round.
    fields: dict[str, Any] = field(default_factory=dict)


def retry(reasons: list[str]) -> str:
    joined = "\n".join(f"- {reason}" for reason in reasons)
    return f"That will not do:\n{joined}\n\nReply with one corrected JSON object."


def converse(
    client: Model,
    messages: list[dict[str, str]],
    steps: TypeAdapter[S],
    rounds_allowed: int,
    act: Callable[[S], Turn[T]],
    round_type: type[R],
) -> tuple[T | None, list[R]]:
    """Ask the model until act returns an answer."""
    rounds: list[R] = []
    replied = 0
    for number in range(1, rounds_allowed + 1):
        sent = messages[replied:]
        try:
            reply = client.complete(messages)
        except LLMError as exc:
            rounds.append(round_type.failed(number, exc, sent))
            return None, rounds
        messages.append({"role": "assistant", "content": reply.text})
        replied = len(messages)

        try:
            step = steps.validate_python(loads(reply.text))
        except (ValueError, ValidationError) as exc:
            rounds.append(round_type.of(number, "unparsed", reply, [str(exc)], sent))
            messages.append({"role": "user", "content": retry([str(exc)])})
            continue

        turn = act(step)
        rounds.append(
            round_type.of(number, turn.action, reply, turn.reasons, sent, **turn.fields)
        )
        if turn.answer is not None:
            return turn.answer, rounds
        told = turn.told if turn.told is not None else retry(turn.reasons)
        messages.append({"role": "user", "content": told})

    return None, rounds
