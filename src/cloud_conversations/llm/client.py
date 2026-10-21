"""One chat completion against an OpenAI-compatible endpoint."""

from __future__ import annotations

import os
from typing import Any, Protocol

from openai import OpenAI
from pydantic import BaseModel

from cloud_conversations.config import MAX_TEMPERATURE, MIN_TEMPERATURE, Settings

API_KEY_ENV = "CC_LLM_API_KEY"
BASE_URL_ENV = "CC_LLM_BASE_URL"
MODEL_ENV = "CC_LLM_MODEL"
TEMPERATURE_ENV = "CC_LLM_TEMPERATURE"


class LLMError(Exception):
    """The model endpoint is not configured or did not answer."""


class Reply(BaseModel):
    text: str
    usage: dict[str, int]
    finish_reason: str
    model: str
    # The model the endpoint says answered.
    served_model: str
    temperature: float


class Model(Protocol):
    def complete(self, messages: list[dict[str, str]]) -> Reply: ...


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float,
        max_retries: int,
        temperature: float,
    ) -> None:
        self._client = OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )
        self._model = model
        self._temperature = temperature

    def complete(self, messages: list[dict[str, str]]) -> Reply:
        try:
            completion = self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                temperature=self._temperature,
            )
        except Exception as exc:
            raise LLMError(f"{self._model}: {exc}") from exc

        choice = completion.choices[0] if completion.choices else None
        if choice is None or choice.message.content is None:
            raise LLMError(f"{self._model} returned no content")
        return Reply(
            text=choice.message.content,
            usage=_usage(completion.usage),
            finish_reason=choice.finish_reason or "",
            model=self._model,
            served_model=completion.model or "",
            temperature=self._temperature,
        )


def temperature_for(settings: Settings, given: float | None) -> float:
    """The sampling temperature to use.

    Raises LLMError if the value is out of range.
    """
    if given is None:
        raw = os.environ.get(TEMPERATURE_ENV)
        if raw is None:
            return settings.llm_temperature
        try:
            given = float(raw)
        except ValueError:
            raise LLMError(f"{TEMPERATURE_ENV}={raw!r} is not a number") from None
        source = TEMPERATURE_ENV
    else:
        source = "--temperature"
    if not MIN_TEMPERATURE <= given <= MAX_TEMPERATURE:
        raise LLMError(
            f"{source} {given} is outside {MIN_TEMPERATURE:g} to {MAX_TEMPERATURE:g}"
        )
    return given


def build_client(
    settings: Settings, model: str | None = None, temperature: float | None = None
) -> LLMClient:
    """Build a client for the configured endpoint.

    Raises LLMError when the endpoint, the model or the key is unset.
    """
    base_url = os.environ.get(BASE_URL_ENV) or settings.llm_base_url
    model = model or os.environ.get(MODEL_ENV) or settings.llm_model
    api_key = os.environ.get(API_KEY_ENV)
    unset = [
        name
        for name, value in ((BASE_URL_ENV, base_url), (MODEL_ENV, model),
                            (API_KEY_ENV, api_key))
        if not value
    ]
    if unset or base_url is None or model is None or api_key is None:
        raise LLMError(f"no model endpoint: {', '.join(unset)} unset")

    return LLMClient(
        base_url, api_key, model, settings.llm_timeout_s, settings.llm_max_retries,
        temperature_for(settings, temperature),
    )


def _usage(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "prompt_tokens": usage.prompt_tokens or 0,
        "completion_tokens": usage.completion_tokens or 0,
        "total_tokens": usage.total_tokens or 0,
    }
