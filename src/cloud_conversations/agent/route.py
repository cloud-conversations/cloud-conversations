"""Route each message to an agent."""

from __future__ import annotations

from pydantic import TypeAdapter

from cloud_conversations import bundle
from cloud_conversations.agent import turns
from cloud_conversations.agent.contract import Route, RouteStep
from cloud_conversations.llm.client import Model

_STEP: TypeAdapter[RouteStep] = TypeAdapter(RouteStep)

# The route if the model gives no usable reply.
FALLBACK: Route = "ask"
_FELL_BACK = RouteStep(action="route", route=FALLBACK, reason="no usable reply")


class Round(turns.Round):
    route: Route | None = None


def route(
    client: Model,
    message: str,
    history: list[tuple[str, str]],
    versions: list[int],
    *,
    testbed: str,
) -> tuple[RouteStep, list[Round]]:
    """Decide where one message goes. Returns the route and the transcript."""
    messages = [
        {"role": "system", "content": _system_prompt(testbed, bool(versions))},
        {"role": "user", "content": _opening(message, history)},
    ]

    def act(step: RouteStep) -> turns.Turn[RouteStep]:
        reasons = _validate_route(step, versions)
        return turns.Turn(
            "route", reasons, answer=None if reasons else step, fields={"route": step.route}
        )

    chosen, rounds = turns.converse(client, messages, _STEP, 2, act, Round)
    return chosen or _FELL_BACK, rounds


def _validate_route(step: RouteStep, versions: list[int]) -> list[str]:
    """Reasons a route cannot be taken on this thread."""
    if step.route == "revise" and not versions:
        return [
            "there is no description on this thread to revise; route describe or ask"
        ]
    if step.route == "build":
        if not versions:
            return [
                "there is no description on this thread to build; route describe or ask"
            ]
        if step.version is not None and step.version not in versions:
            named = ", ".join(f"v{v}" for v in versions)
            return [f"this thread holds {named}; name one of those, or none"]
    return []


def _system_prompt(testbed: str, has_description: bool) -> str:
    prompts = bundle.default()
    parts = [
        prompts.prompt(testbed, "route", "instructions"),
        prompts.prompt(testbed, "route", "versions") if has_description else "",
        prompts.prompt(testbed, "route", "rules"),
        "" if has_description else prompts.prompt(testbed, "route", "no-description"),
    ]
    return "\n".join(part for part in parts if part)


def _opening(message: str, history: list[tuple[str, str]]) -> str:
    if not history:
        return f"The message:\n\n{message}"
    said = "\n\n".join(f"{who}: {what}" for who, what in history)
    return f"The conversation so far:\n\n{said}\n\nThe message:\n\n{message}"
