"""One exchange, end to end: what each route writes on the thread."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.config import Config, Settings
from cloud_conversations.llm.client import LLMError
from cloud_conversations.worker import chat
from tests.conftest import PUBLISHED_CONFIG
from tests.fakes import (
    KNOWLEDGE,
    LEASES,
    SITES,
    UC,
    FakeModel,
    Published,
    answer_reply,
    publish,
    reference_node,
    search_reply,
)
from tests.loopback import page, serving

pytestmark = pytest.mark.usefixtures("logged_in")


def _config() -> Config:
    return Config(
        defaults=Settings(max_rounds=4),
        adapters=PUBLISHED_CONFIG.adapters,
        knowledge=KNOWLEDGE,
    )


def _route(chosen: str, **extra: object) -> str:
    return json.dumps(
        {"action": "route", "route": chosen, "reason": "because", **extra}
    )


def _decline() -> str:
    return json.dumps({
        "action": "decline",
        "reason": "this testbed has no quantum hardware",
        "suggestions": ["a bare-metal node to simulate it on"],
    })


@pytest.fixture()
def thread(owner_a):
    return store.create_thread(owner_a, "New chat")


def _exchange(monkeypatch, owner, thread, text, replies, *, payloads=None, config=None):
    """Run one exchange against canned model replies and a stubbed reference API."""
    model = FakeModel(replies)
    monkeypatch.setattr(chat, "build_client", lambda settings: model)
    skylake = reference_node("compute_skylake")
    payloads = payloads or (page(skylake, skylake),)

    with serving(*payloads) as (root, _asked):
        adapter = Published(root, SITES)
        monkeypatch.setattr(chat.credentials, "testbed_adapter", lambda *a, **k: (adapter, None))

        job = store.enqueue_job(owner, thread.thread_id, "exchange", {"text": text})
        store.append_message(thread.thread_id, "user", "user", text, owner_id=owner)
        message = chat.exchange(job, config or _config())
    return message, model


def test_a_documentation_question_is_answered_with_its_sources(
    monkeypatch, owner_a, thread, embedder, open_base
):
    publish([LEASES], KNOWLEDGE, embedder)

    message, _ = _exchange(
        monkeypatch, owner_a, thread, "creating a lease",
        [_route("ask"), "Create a lease in Blazar."],
    )

    assert message.kind == "answer"
    assert message.text == "Create a lease in Blazar."
    assert message.payload["sources"] == [LEASES.url]


def test_a_question_with_no_index_says_so_rather_than_failing(
    monkeypatch, owner_a, thread
):
    message, model = _exchange(
        monkeypatch, owner_a, thread, "how do I make a lease?", [_route("ask")]
    )

    assert message.kind == "notice"
    assert "no documentation index" in message.text
    assert len(model.calls) == 1


def test_a_question_with_the_knowledge_base_disabled_is_declined(
    monkeypatch, owner_a, thread, embedder
):
    publish([LEASES], KNOWLEDGE, embedder)
    disabled = _config().model_copy(
        update={"knowledge": KNOWLEDGE.model_copy(update={"enabled": False})}
    )

    message, model = _exchange(
        monkeypatch, owner_a, thread, "creating a lease",
        [_route("ask"), "Create a lease in Blazar."], config=disabled,
    )

    assert message.kind == "notice"
    assert message.text == chat.KNOWLEDGE_DISABLED
    assert len(model.calls) == 1


def _describe_first(monkeypatch, owner_a, thread):
    return _exchange(
        monkeypatch, owner_a, thread, "a skylake node for two hours",
        [_route("describe"), search_reply(), answer_reply()],
    )[0]


def test_a_request_for_hardware_writes_the_first_version(monkeypatch, owner_a, thread):
    message = _describe_first(monkeypatch, owner_a, thread)

    assert message.kind == "description"
    assert message.description_version == 1
    assert "compute_skylake at CHI@UC for 2h" in message.text

    linked = store.get_thread(owner_a, thread.thread_id).description_id
    assert linked == message.description_id
    written = store.latest_description(owner_a, linked)
    assert written.requirements.value_of("node_type") == "compute_skylake"


def _describe_prompt(monkeypatch, owner, thread) -> str:
    _, model = _exchange(
        monkeypatch, owner, thread, "a node", [_route("describe"), answer_reply()]
    )
    return model.calls[1][0]["content"]


def test_the_description_agent_is_told_the_users_default_sites(monkeypatch, owner_a, thread):
    store.update_user(owner_a, default_vm_site="CHI@TACC", default_baremetal_site="CHI@UC")

    prompt = _describe_prompt(monkeypatch, owner_a, thread)

    assert (
        "Unless the request names a site, prefer CHI@TACC for a VM and CHI@UC for bare metal."
        in prompt
    )


def test_a_default_site_no_longer_offered_is_not_preferred(monkeypatch, owner_a, thread):
    store.update_user(owner_a, default_vm_site="CHI@TACC", default_baremetal_site="CHI@GONE")

    prompt = _describe_prompt(monkeypatch, owner_a, thread)

    assert "Unless the request names a site, prefer CHI@TACC for a VM.\n" in prompt


def test_a_lease_running_into_its_sites_retirement_is_warned(monkeypatch, owner_a, thread):
    today = datetime.now(timezone.utc).date()
    chameleon = PUBLISHED_CONFIG.adapters["chameleon"]
    retiring = {**chameleon.sites[UC], "retirement": {
        "retires_on": today + timedelta(days=30), "warn_from": today,
        "notice": "CHI@UC retires next month.", "retired_notice": "CHI@UC has retired.",
    }}
    config = _config().model_copy(update={"adapters": {
        "chameleon": chameleon.model_copy(update={"sites": {**chameleon.sites, UC: retiring}})
    }})

    _exchange(
        monkeypatch, owner_a, thread, "a skylake node for two hours",
        [_route("describe"), search_reply(), answer_reply()], config=config,
    )

    said = [(m.kind, m.text) for m in store.read_messages(owner_a, thread.thread_id)]
    assert said[-1] == ("notice", "CHI@UC retires next month.")
    assert said[-2][0] == "description"


def test_an_answer_to_an_open_question_appends_a_version(monkeypatch, owner_a, thread):
    first = _describe_first(monkeypatch, owner_a, thread)
    store.finish_job(store.unfinished_jobs(owner_a, thread.thread_id)[0].job_id)

    message, _ = _exchange(
        monkeypatch, owner_a, store.get_thread(owner_a, thread.thread_id),
        "Ubuntu 22.04 please",
        [_route("revise"), search_reply(), answer_reply(
            items=[
                {"key": "node_type", "value": "compute_skylake", "source": "stated"},
                {"key": "os_release", "value": "Ubuntu 22.04", "source": "asked"},
            ],
            rubric=[
                {"id": "ssh", "kind": "ssh_reachable", "expected": True,
                 "comparator": "eq"},
                {"id": "os", "kind": "os_release", "expected": "Ubuntu 22.04",
                 "comparator": "prefix", "from_requirement": "os_release"},
            ],
        )],
    )

    assert message.description_id == first.description_id
    assert message.description_version == 2
    written = store.get_description(owner_a, message.description_id, 2)
    assert written.parent_version == 1
    assert written.revision_reason == "Ubuntu 22.04 please"


def test_building_a_version_again_puts_it_back_without_revising_it(
    monkeypatch, owner_a, thread
):
    first = _describe_first(monkeypatch, owner_a, thread)
    store.finish_job(store.unfinished_jobs(owner_a, thread.thread_id)[0].job_id)
    _exchange(
        monkeypatch, owner_a, store.get_thread(owner_a, thread.thread_id),
        "make it 4 hours",
        [_route("revise"), search_reply(duration_hours=4),
         answer_reply(duration_hours=4, duration_source="stated")],
    )
    store.finish_job(store.unfinished_jobs(owner_a, thread.thread_id)[0].job_id)

    message, model = _exchange(
        monkeypatch, owner_a, store.get_thread(owner_a, thread.thread_id),
        "build version 1 again", [_route("build", version=1)],
    )

    assert message.kind == "description"
    assert message.description_version == 1
    assert message.payload["again"] is True
    assert len(model.calls) == 1
    assert len(store.description_versions(owner_a, first.description_id)) == 2


def _tomorrow() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()


def test_a_revision_that_changes_nothing_says_so_and_writes_no_version(
    monkeypatch, owner_a, thread
):
    first = _describe_first(monkeypatch, owner_a, thread)
    store.finish_job(store.unfinished_jobs(owner_a, thread.thread_id)[0].job_id)

    message, _ = _exchange(
        monkeypatch, owner_a, store.get_thread(owner_a, thread.thread_id),
        "can it start in 10 minutes?",
        [_route("revise"), search_reply(), answer_reply(start_at=_tomorrow(), start_source="asked"),
         answer_reply()],
    )

    assert message.kind == "notice"
    assert message.text.startswith("I could not change anything, so version 1 still")
    assert "was found free for 2h from as soon as possible" in message.text
    assert "Say it another way" in message.text
    assert len(store.description_versions(owner_a, first.description_id)) == 1


def test_saying_what_it_could_not_keep_is_a_version_of_its_own(
    monkeypatch, owner_a, thread
):
    first = _describe_first(monkeypatch, owner_a, thread)
    store.finish_job(store.unfinished_jobs(owner_a, thread.thread_id)[0].job_id)

    message, _ = _exchange(
        monkeypatch, owner_a, store.get_thread(owner_a, thread.thread_id),
        "can it start in 10 minutes?",
        [_route("revise"), search_reply(),
         answer_reply(open_questions=["nothing is free in 10 minutes; starting now"])],
    )

    assert message.description_version == 2
    written = store.get_description(owner_a, first.description_id, 2)
    assert written.open_questions == [
        "nothing is free in 10 minutes; starting now"
    ]


def test_a_decline_is_reported_with_its_suggestions_and_writes_no_description(
    monkeypatch, owner_a, thread
):
    message, _ = _exchange(
        monkeypatch, owner_a, thread, "a quantum computer",
        [_route("describe"), _decline()],
    )

    assert message.kind == "notice"
    assert message.text == "this testbed has no quantum hardware"
    assert message.payload["suggestions"] == ["a bare-metal node to simulate it on"]
    assert store.get_thread(owner_a, thread.thread_id).description_id is None


def test_a_loop_that_reaches_nothing_says_so(monkeypatch, owner_a, thread):
    message, _ = _exchange(
        monkeypatch, owner_a, thread, "a skylake node",
        [_route("describe"), "not json", "still not json", "nope", "nope"],
    )

    assert message.kind == "notice"
    assert "could not settle on hardware" in message.text


def test_no_model_endpoint_is_reported_on_the_thread(monkeypatch, owner_a, thread):
    def unreachable(settings):
        raise LLMError("CC_LLM_BASE_URL unset")

    monkeypatch.setattr(chat, "build_client", unreachable)
    job = store.enqueue_job(owner_a, thread.thread_id, "exchange", {"text": "hello"})

    message = chat.exchange(job, _config())

    assert message.kind == "notice"
    assert "cannot reach a model" in message.text
    assert "CC_LLM_BASE_URL unset" in message.text


def test_a_model_that_stops_answering_is_reported_on_the_thread(monkeypatch, owner_a, thread):
    message, _ = _exchange(monkeypatch, owner_a, thread, "a skylake node", [])

    assert (message.kind, message.text) == (
        "notice", f"{chat.NO_ENDPOINT} (the fake model has no replies left)"
    )
    routing = store.get_conversation(
        owner_a, uuid.UUID(message.payload["routing_conversation_id"])
    )
    assert [r["action"] for r in routing.transcript] == ["error"]


def test_a_listing_it_cannot_do_is_refused_with_its_reason(
    monkeypatch, owner_a, thread
):
    message, _ = _holdings_exchange(
        monkeypatch, owner_a, thread, "how much of my allocation is left?",
        [
            _route("inventory"),
            json.dumps({
                "action": "decline",
                "reason": "I cannot read allocations here",
                "suggestions": ["your leases"],
            }),
        ],
    )

    assert message.kind == "notice"
    assert message.text == "I cannot read allocations here"
    assert message.payload["suggestions"] == ["your leases"]


def test_the_router_is_shown_what_was_said_before(monkeypatch, owner_a, thread):
    store.append_message(
        thread.thread_id, "user", "user", "an A100 for a day", owner_id=owner_a
    )
    _, model = _exchange(
        monkeypatch, owner_a, thread, "make it 12 hours", [_route("ask")]
    )

    assert "an A100 for a day" in model.calls[0][1]["content"]


def test_an_exchange_records_what_the_router_said(monkeypatch, owner_a, thread):
    message, _ = _exchange(
        monkeypatch, owner_a, thread, "creating a lease", [_route("ask")]
    )

    routing = uuid.UUID(str(message.payload["routing_conversation_id"]))
    stored = store.get_conversation(owner_a, routing)
    assert stored.prompt == "creating a lease"
    assert stored.transcript[0]["action"] == "route"


def _holdings_exchange(monkeypatch, owner, thread, text, replies):
    """Run one exchange against the fake sites."""
    from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites

    model = FakeModel(replies)
    adapter = FakeAdapter(sites=default_fake_sites(), config=_config())
    adapter.leases["lease-1"] = _fake_lease("lease-1", "FAKE@BM")
    monkeypatch.setattr(chat, "build_client", lambda settings: model)
    monkeypatch.setattr(
        chat.credentials, "build_adapter", lambda site, config, user=None: adapter
    )

    job = store.enqueue_job(owner, thread.thread_id, "exchange", {"text": text})
    store.append_message(thread.thread_id, "user", "user", text, owner_id=owner)
    return chat.exchange(job, _config()), model


def _fake_lease(lease_id, site):
    from cloud_conversations.adapter.fake import FakeLease

    return FakeLease(
        lease_id=lease_id, name="mine", site=site, state="ACTIVE",
        start=datetime(2026, 9, 20, 9, tzinfo=timezone.utc),
        end=datetime(2026, 9, 20, 13, tzinfo=timezone.utc),
        count=1, node_type="compute_skylake", flavor_id=None,
        reservation_id="res-1", reserved_flavor_id=None, reserved_fips=[],
        run_id=None,
    )


def test_listing_what_the_user_holds_writes_the_tables_it_read(
    monkeypatch, owner_a, thread
):
    message, _ = _holdings_exchange(
        monkeypatch, owner_a, thread, "list my leases",
        [
            _route("inventory"),
            json.dumps({
                "action": "holdings", "holdings": ["leases"], "site": "FAKE@BM",
            }),
        ],
    )

    assert message.kind == "holdings"
    assert message.text == "1 lease at FAKE@BM."
    assert "mine  lease-1  ACTIVE  1x" in message.payload["tables"][0]
    assert store.unfinished_jobs(owner_a, thread.thread_id)[0].route == "inventory"


def test_listing_with_no_site_credentials_says_so_and_spends_no_round(
    monkeypatch, owner_a, thread
):
    from cloud_conversations.adapter.fake import FakeAdapter

    model = FakeModel([_route("inventory")])
    monkeypatch.setattr(chat, "build_client", lambda settings: model)
    monkeypatch.setattr(
        chat.credentials,
        "build_adapter",
        lambda site, config, user=None: FakeAdapter(sites={}, config=_config()),
    )

    job = store.enqueue_job(
        owner_a, thread.thread_id, "exchange", {"text": "list my leases"}
    )
    message = chat.exchange(job, _config())

    assert message.kind == "notice"
    assert message.text == "You have no login at any chameleon site, so there is nothing to list."
    assert len(model.calls) == 1


def test_only_the_sites_with_credentials_are_listed(
    monkeypatch, owner_a, thread
):
    from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites

    model = FakeModel([
        _route("inventory"),
        json.dumps({"action": "holdings", "holdings": ["leases"]}),
    ])
    sites = default_fake_sites()
    adapter = FakeAdapter(sites=sites, config=_config())
    monkeypatch.setattr(adapter, "sites_with_login", lambda: ["FAKE@BM"])
    monkeypatch.setattr(chat, "build_client", lambda settings: model)
    monkeypatch.setattr(
        chat.credentials, "build_adapter", lambda site, config, user=None: adapter
    )

    job = store.enqueue_job(
        owner_a, thread.thread_id, "exchange", {"text": "list my leases"}
    )
    message = chat.exchange(job, _config())

    assert message.kind == "holdings"
    assert "FAKE@BM" in model.calls[1][0]["content"]
    assert "FAKE@KVM" not in model.calls[1][0]["content"]
    assert "FAKE@KVM" not in message.payload["tables"][0]


def test_a_local_users_catalog_says_why_it_lists_no_images(monkeypatch):
    from cloud_conversations import local_users
    from cloud_conversations.adapter.chameleon.catalog import CatalogError

    local_users.add("chameleon", "alice", "correct horse battery")
    thread = store.create_thread("chameleon:local:alice", "New chat")
    asked: dict = {}

    def catalog(adapter, config, site, images_unseen=None):
        asked.update(logins=adapter.sites_with_login(), images_unseen=images_unseen)
        raise CatalogError("stop before the reference API")

    monkeypatch.setattr(chat, "get_catalog", catalog)
    monkeypatch.setattr(chat, "build_client", lambda settings: FakeModel([_route("describe")]))
    job = store.enqueue_job(
        "chameleon:local:alice", thread.thread_id, "exchange", {"text": "ubuntu 26"}
    )

    chat.exchange(job, PUBLISHED_CONFIG)

    assert asked == {"logins": [], "images_unseen": "this account cannot reach Chameleon"}
