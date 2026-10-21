"""Building a model client."""

from __future__ import annotations

import pytest

from cloud_conversations.config import Settings
from cloud_conversations.llm.client import (
    API_KEY_ENV,
    BASE_URL_ENV,
    MODEL_ENV,
    TEMPERATURE_ENV,
    LLMClient,
    LLMError,
    build_client,
    temperature_for,
)


@pytest.fixture(autouse=True)
def _no_llm_env(monkeypatch):
    for name in (BASE_URL_ENV, MODEL_ENV, API_KEY_ENV):
        monkeypatch.delenv(name, raising=False)


def test_an_unconfigured_endpoint_names_every_missing_variable():
    with pytest.raises(LLMError) as exc:
        build_client(Settings())
    assert BASE_URL_ENV in str(exc.value)
    assert MODEL_ENV in str(exc.value)
    assert API_KEY_ENV in str(exc.value)


def test_the_environment_supplies_what_the_settings_do_not(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "https://from-env.invalid/v1")
    monkeypatch.setenv(MODEL_ENV, "from-env")
    monkeypatch.setenv(API_KEY_ENV, "sk-test")

    client = build_client(Settings())

    assert str(client._client.base_url).startswith("https://from-env.invalid")  # noqa: SLF001
    assert client._model == "from-env"  # noqa: SLF001


def test_the_environment_beats_the_configured_model(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "from-env")
    monkeypatch.setenv(API_KEY_ENV, "sk-test")
    settings = Settings(llm_base_url="https://llm.invalid/v1", llm_model="from-file")

    assert build_client(settings)._model == "from-env"  # noqa: SLF001


def test_a_named_model_beats_the_environment(monkeypatch):
    monkeypatch.setenv(MODEL_ENV, "from-env")
    monkeypatch.setenv(API_KEY_ENV, "sk-test")
    settings = Settings(llm_base_url="https://llm.invalid/v1", llm_model="from-file")

    assert build_client(settings, "from-flag")._model == "from-flag"  # noqa: SLF001


class _Message:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None, finish_reason: str | None = "stop") -> None:
        self.message = _Message(content)
        self.finish_reason = finish_reason


class _Usage:
    prompt_tokens = 1200
    completion_tokens = 80
    total_tokens = 1280


class _Completion:
    model = "llama-70b-0614"

    def __init__(self, choices: list[_Choice], usage: object | None = None) -> None:
        self.choices = choices
        self.usage = usage


class _StubOpenAI:
    def __init__(self, result, **kwargs) -> None:
        self.kwargs = kwargs
        self.created: list[dict] = []
        self._result = result
        outer = self

        class _Completions:
            def create(self, **call):
                outer.created.append(call)
                if isinstance(outer._result, Exception):
                    raise outer._result
                return outer._result

        self.chat = type("_Chat", (), {"completions": _Completions()})()


@pytest.fixture()
def stub_openai(monkeypatch):
    def _install(result):
        built: list[_StubOpenAI] = []

        def factory(**kwargs):
            built.append(_StubOpenAI(result, **kwargs))
            return built[-1]

        monkeypatch.setattr("cloud_conversations.llm.client.OpenAI", factory)
        return built

    return _install


def _client() -> LLMClient:
    return LLMClient("https://llm.invalid/v1", "sk-test", "llama-70b", 30.0, 2, 0.0)


def test_a_completion_becomes_a_reply(stub_openai):
    built = stub_openai(_Completion([_Choice('{"action": "search"}')], _Usage()))
    reply = _client().complete([{"role": "user", "content": "a skylake node"}])

    assert reply.text == '{"action": "search"}'
    assert reply.finish_reason == "stop"
    assert reply.served_model == "llama-70b-0614"
    assert reply.usage == {
        "prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280
    }

    call = built[0].created[0]
    assert call["model"] == "llama-70b"
    assert call["messages"] == [{"role": "user", "content": "a skylake node"}]
    assert call["temperature"] == 0.0
    assert built[0].kwargs["timeout"] == 30.0
    assert built[0].kwargs["max_retries"] == 2


def test_an_endpoint_that_returns_nothing_usable_is_an_error(stub_openai):
    stub_openai(_Completion([]))
    with pytest.raises(LLMError, match="no content"):
        _client().complete([{"role": "user", "content": "hi"}])

    stub_openai(_Completion([_Choice(None)]))
    with pytest.raises(LLMError, match="no content"):
        _client().complete([{"role": "user", "content": "hi"}])


def test_a_transport_failure_becomes_an_llm_error(stub_openai):
    stub_openai(RuntimeError("connection reset"))
    with pytest.raises(LLMError, match="connection reset"):
        _client().complete([{"role": "user", "content": "hi"}])


def test_a_reply_without_usage_still_parses(stub_openai):
    stub_openai(_Completion([_Choice("{}", finish_reason=None)]))
    reply = _client().complete([{"role": "user", "content": "hi"}])

    assert reply.usage == {}
    assert reply.finish_reason == ""


def test_the_configured_temperature_reaches_the_endpoint(monkeypatch, stub_openai):
    monkeypatch.setenv(API_KEY_ENV, "sk-test")
    built = stub_openai(_Completion([_Choice("{}")]))
    settings = Settings(
        llm_base_url="https://llm.invalid/v1", llm_model="m", llm_temperature=0.7
    )

    build_client(settings).complete([{"role": "user", "content": "hi"}])
    assert built[0].created[0]["temperature"] == 0.7


def test_the_environment_beats_the_configured_temperature(monkeypatch):
    monkeypatch.setenv(TEMPERATURE_ENV, "0.7")
    assert temperature_for(Settings(llm_temperature=0.0), None) == 0.7


def test_a_named_temperature_beats_the_environment(monkeypatch):
    monkeypatch.setenv(TEMPERATURE_ENV, "0.7")
    assert temperature_for(Settings(llm_temperature=0.0), 0.2) == 0.2


def test_zero_is_a_temperature_not_an_absent_one(monkeypatch):
    monkeypatch.setenv(TEMPERATURE_ENV, "0.7")
    assert temperature_for(Settings(llm_temperature=1.5), 0.0) == 0.0


def test_the_file_is_used_when_nothing_else_says(monkeypatch):
    monkeypatch.delenv(TEMPERATURE_ENV, raising=False)
    assert temperature_for(Settings(llm_temperature=1.5), None) == 1.5


@pytest.mark.parametrize("raw", ["warm", ""])
def test_a_temperature_that_is_not_a_number_is_refused(monkeypatch, raw):
    monkeypatch.setenv(TEMPERATURE_ENV, raw)
    with pytest.raises(LLMError, match=TEMPERATURE_ENV):
        temperature_for(Settings(), None)


@pytest.mark.parametrize("given", [-0.1, 2.1])
def test_a_temperature_outside_the_range_is_refused(given):
    with pytest.raises(LLMError, match="outside 0 to 2"):
        temperature_for(Settings(), given)
