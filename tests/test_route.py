"""Routing one message to an agent."""

from __future__ import annotations

import json

from cloud_conversations import bundle
from cloud_conversations.agent.route import FALLBACK, route
from tests.fakes import FakeModel


def _reply(chosen: str, reason: str = "because", **extra: object) -> str:
    return json.dumps(
        {"action": "route", "route": chosen, "reason": reason, **extra}
    )


def test_revise_is_taken_where_the_thread_holds_a_description():
    chosen, rounds = route(FakeModel([_reply("revise")]), "make it 12 hours", [], [1],
        testbed="chameleon")

    assert chosen.route == "revise"
    assert rounds[0].reasons == []


def test_revise_is_refused_where_there_is_nothing_to_revise():
    model = FakeModel([_reply("revise"), _reply("ask")])

    chosen, rounds = route(model, "make it 12 hours", [], [], testbed="chameleon")

    assert chosen.route == "ask", "the corrected reply is what stands"
    assert rounds[0].reasons == [
        "there is no description on this thread to revise; route describe or ask"
    ]
    assert len(model.calls) == 2


def test_the_lineage_routes_are_offered_only_with_a_description():
    without = FakeModel([_reply("ask")])
    route(without, "hello", [], [], testbed="chameleon")
    with_one = FakeModel([_reply("ask")])
    route(with_one, "hello", [], [1], testbed="chameleon")

    lineage = bundle.default().prompt("chameleon", "route", "versions")
    assert lineage not in without.calls[0][0]["content"]
    assert lineage in with_one.calls[0][0]["content"]


def test_a_request_to_build_a_version_again_names_that_version():
    model = FakeModel([_reply("build", version=3)])

    chosen, rounds = route(model, "build version 3 again", [], [1, 2, 3, 4], testbed="chameleon")

    assert (chosen.route, chosen.version) == ("build", 3)
    assert rounds[0].reasons == []


def test_a_version_the_thread_does_not_hold_is_refused():
    model = FakeModel([_reply("build", version=9), _reply("build", version=2)])

    chosen, rounds = route(model, "build version 9 again", [], [1, 2], testbed="chameleon")

    assert (chosen.route, chosen.version) == ("build", 2)
    assert rounds[0].reasons == [
        "this thread holds v1, v2; name one of those, or none"
    ]


def test_a_reply_that_will_not_parse_is_corrected_once():
    model = FakeModel(["I think this is a new request.", _reply("describe")])

    chosen, rounds = route(model, "an A100 for a day", [], [], testbed="chameleon")

    assert chosen.route == "describe"
    assert [t.action for t in rounds] == ["unparsed", "route"]


def test_each_round_records_what_was_sent_since_the_last_reply():
    model = FakeModel(["I think this is a new request.", _reply("describe")])

    _, rounds = route(model, "an A100 for a day", [], [], testbed="chameleon")

    assert rounds[0].sent == model.calls[0]
    assert rounds[1].sent == model.calls[1][-1:]


def test_two_unusable_replies_fall_back_to_the_documentation():
    model = FakeModel(["nonsense", "more nonsense"])

    chosen, rounds = route(model, "anything", [], [], testbed="chameleon")

    assert chosen.route == FALLBACK == "ask"
    assert [t.action for t in rounds] == ["unparsed", "unparsed"]


def test_an_unreachable_model_falls_back_to_the_documentation():
    chosen, rounds = route(FakeModel([]), "anything", [], [], testbed="chameleon")

    assert chosen.route == FALLBACK
    assert [t.action for t in rounds] == ["error"]
    assert rounds[0].reasons == ["the fake model has no replies left"]


def test_the_conversation_reaches_the_model_so_a_short_reply_can_be_read():
    model = FakeModel([_reply("revise")])
    history = [("user", "an A100 for a day"), ("assistant", "gpu_a100 at CHI@TACC")]

    route(model, "make it 12 hours", history, [1], testbed="chameleon")

    asked = model.calls[0][1]["content"]
    assert "an A100 for a day" in asked
    assert "make it 12 hours" in asked
