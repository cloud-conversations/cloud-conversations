"""Turning a message into a listing request."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
from cloud_conversations.agent.contract import DeclineStep
from cloud_conversations.agent.inventory import HoldingsStep, parse_time, read_request
from tests.conftest import PUBLISHED_CONFIG
from tests.fakes import FakeModel

NOW = datetime(2026, 11, 6, 14, 2, tzinfo=ZoneInfo("America/Chicago"))
_KINDS = FakeAdapter(sites=default_fake_sites(), config=PUBLISHED_CONFIG).resource_kinds()


def _step(kinds: list[str], sites: list[str] | None = None, **more: object) -> str:
    return json.dumps({"action": "holdings", "kinds": kinds, "sites": sites or [], **more})


def _corrected(*conditions: dict) -> list[str]:
    """The reasons sent back for the first reply."""
    model = FakeModel([_step(["leases"], where=list(conditions)), _step(["leases"])])
    _, rounds = read_request(model, _KINDS, "my leases", testbed="chameleon", now=NOW)
    return rounds[0].reasons


def test_the_sites_a_message_names_are_kept_as_written():
    model = FakeModel([_step(["leases"], ["TACC"])])

    step, rounds = read_request(
        model, _KINDS, "my leases at TACC", testbed="chameleon", now=NOW
    )

    assert step == HoldingsStep(action="holdings", kinds=["leases"], sites=["TACC"])
    assert len(rounds) == 1


def test_the_prompt_lists_the_adapters_kinds():
    model = FakeModel([_step(["leases"])])

    read_request(model, _KINDS, "my leases", testbed="chameleon", now=NOW)

    assert (
        "\n  leases: ends\n  instances: addresses\n  images: no fields of its own\n"
        in model.calls[0][0]["content"]
    )


def test_an_unknown_kind_is_corrected_once_with_the_kinds_there_are():
    model = FakeModel([_step(["allocations"]), _step(["leases"])])

    step, rounds = read_request(model, _KINDS, "my allocation", testbed="chameleon", now=NOW)

    assert isinstance(step, HoldingsStep)
    assert rounds[0].reasons == [
        "there is no kind 'allocations'; the kinds are leases, instances, images"
    ]


def test_two_replies_that_will_not_do_end_with_nothing():
    model = FakeModel([_step(["allocations"]), _step(["allocations"])])

    step, rounds = read_request(model, _KINDS, "my allocation", testbed="chameleon", now=NOW)

    assert (step, len(rounds)) == (None, 2)


def test_something_it_cannot_list_is_refused_rather_than_guessed_at():
    model = FakeModel([json.dumps({
        "action": "decline", "reason": "I cannot read allocations here",
        "suggestions": ["your leases"],
    })])

    step, _ = read_request(
        model, _KINDS, "how much of my allocation is left?", testbed="chameleon", now=NOW
    )

    assert isinstance(step, DeclineStep)
    assert (step.reason, step.suggestions) == ("I cannot read allocations here", ["your leases"])


def test_the_prompt_says_what_time_it_is_for_the_user():
    model = FakeModel([_step(["leases"])])

    read_request(model, _KINDS, "my leases", testbed="chameleon", now=NOW)

    prompt = model.calls[0][0]["content"]
    assert "The user's time now is Friday 2026-11-06T14:02 CST." in prompt


def test_a_condition_on_a_field_the_kind_lacks_is_corrected():
    assert _corrected({"field": "size_gb", "op": "gt", "value": 10}) == [
        "'size_gb' is not a field of these kinds; the fields are "
        "name, status, created, lease_ends, made_by, ends"
    ]


def test_an_op_that_does_not_fit_the_field_is_corrected():
    assert _corrected({"field": "ends", "op": "contains", "value": "Monday"}) == [
        "'contains' does not apply to 'ends'; use after, before"
    ]


def test_a_time_it_cannot_read_is_corrected():
    assert _corrected({"field": "ends", "op": "before", "value": "next Monday"}) == [
        "'next Monday' is not a time; write an ISO time, now, today, "
        "or an offset like -7d or +24h"
    ]


def test_a_number_on_a_text_field_is_corrected():
    assert _corrected({"field": "name", "op": "contains", "value": 2}) == [
        "'name' contains takes text"
    ]


def test_an_offset_too_far_out_is_corrected():
    assert _corrected({"field": "ends", "op": "before", "value": "+999999999d"}) == [
        "'+999999999d' is not a time; write an ISO time, now, today, "
        "or an offset like -7d or +24h"
    ]


def test_made_by_takes_only_its_own_values():
    assert _corrected({"field": "made_by", "op": "in", "value": ["bob"]}) == [
        "made_by takes you, your_experiments, this_chat, others"
    ]


def test_a_time_is_read_in_the_users_zone():
    assert parse_time("2026-11-09T09:00", NOW) == datetime(
        2026, 11, 9, 9, tzinfo=ZoneInfo("America/Chicago")
    )
    week_ago = datetime(2026, 10, 30, 14, 2, tzinfo=ZoneInfo("America/Chicago"))
    assert parse_time("-7d", NOW) == week_ago
