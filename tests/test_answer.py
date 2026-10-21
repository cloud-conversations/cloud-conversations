"""The docs assistant: what it is sent, and what it gives back."""

from __future__ import annotations

from cloud_conversations.knowledge.answer import answer, as_turn
from cloud_conversations.knowledge.retrieve import PRIMARY_SECTION, SUPPLEMENTARY_SECTION
from cloud_conversations.schema.knowledge import Knowledge
from tests.fakes import KNOWLEDGE, LEASES, FakeModel, publish


def test_the_answer_is_what_the_model_said_about_the_passages_found(
    embedder, open_base
):
    publish([LEASES], KNOWLEDGE, embedder)
    client = FakeModel(["Create a lease in Blazar, then launch onto it."])

    given = answer(client, open_base(KNOWLEDGE), "creating a lease", [], testbed="chameleon")

    assert given.text == "Create a lease in Blazar, then launch onto it."
    assert given.sources == [LEASES.url]


def test_the_question_and_the_context_are_the_last_thing_the_model_is_sent(
    embedder, open_base
):
    publish([LEASES], KNOWLEDGE, embedder)
    client = FakeModel(["An answer."])
    base = open_base(KNOWLEDGE)

    given = answer(client, base, "creating a lease in Blazar", [], testbed="chameleon")

    assert client.calls[0][-1] == {
        "role": "user",
        "content": (
            f"Question: creating a lease in Blazar\nContext: {given.retrieved.context}"
        ),
    }
    assert client.calls[0][0]["role"] == "system"


def test_the_stored_turn_records_what_the_model_was_sent(embedder, open_base):
    publish([LEASES], KNOWLEDGE, embedder)
    client = FakeModel(["An answer."])

    given = answer(client, open_base(KNOWLEDGE), "creating a lease", [], testbed="chameleon")

    assert as_turn(given)["sent"] == client.calls[0]


def test_history_is_trimmed_to_the_turns_configured(embedder, open_base):
    knowledge = Knowledge(
        query_prefix="", sources=KNOWLEDGE.sources, max_history_exchanges=2
    )
    publish([LEASES], knowledge, embedder)
    client = FakeModel(["An answer."])
    history = [(f"question {n}", f"answer {n}") for n in range(4)]

    answer(client, open_base(knowledge), "and now", history, testbed="chameleon")

    assert [m["content"] for m in client.calls[0][1:-1]] == [
        "question 2", "answer 2", "question 3", "answer 3",
    ]


def test_no_history_is_sent_where_none_is_configured(embedder, open_base):
    knowledge = Knowledge(
        query_prefix="", sources=KNOWLEDGE.sources, max_history_exchanges=0
    )
    publish([LEASES], knowledge, embedder)
    client = FakeModel(["An answer."])

    answer(client, open_base(knowledge), "and now", [("earlier", "before")], testbed="chameleon")

    assert len(client.calls[0]) == 2


def test_the_deployment_policy_is_appended_to_the_instructions(embedder, open_base):
    knowledge = Knowledge(
        query_prefix="",
        sources=KNOWLEDGE.sources,
        answer_policy="  Name the site a command must be run against.  ",
    )
    publish([LEASES], knowledge, embedder)
    client = FakeModel(["An answer."])

    answer(client, open_base(knowledge), "creating a lease", [], testbed="chameleon")

    system = client.calls[0][0]["content"]
    assert system.endswith(
        "Also:\n\nName the site a command must be run against."
    )


def test_no_policy_leaves_the_instructions_alone(embedder, open_base):
    publish([LEASES], KNOWLEDGE, embedder)
    client = FakeModel(["An answer."])

    answer(client, open_base(KNOWLEDGE), "creating a lease", [], testbed="chameleon")

    assert "Also:" not in client.calls[0][0]["content"]


def test_the_instructions_name_the_sections_the_context_is_built_from(
    embedder, open_base
):
    publish([LEASES], KNOWLEDGE, embedder)
    client = FakeModel(["An answer."])

    given = answer(client, open_base(KNOWLEDGE), "creating a lease", [], testbed="chameleon")

    system = client.calls[0][0]["content"]
    assert f"=== {PRIMARY_SECTION} (" in given.retrieved.context
    assert PRIMARY_SECTION in system
    assert SUPPLEMENTARY_SECTION in system
