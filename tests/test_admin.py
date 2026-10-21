"""What an admin does to an account, and the announcement."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.routing import APIRoute

import cloud_conversations.db.store as store
from cloud_conversations.api.app import app
from cloud_conversations.api.deps import config, session_hash
from tests.conftest import ALICE, BOB

pytestmark = pytest.mark.usefixtures("logged_in", "alice_is_an_admin")


_ADMIN_ROUTES = sorted(
    (method, route.path)
    for route in app.routes
    if isinstance(route, APIRoute) and route.path.startswith("/admin")
    for method in route.methods or ()
)


@pytest.mark.parametrize(
    ("method", "path"), _ADMIN_ROUTES, ids=[f"{m} {p}" for m, p in _ADMIN_ROUTES]
)
def test_every_admin_route_refuses_someone_who_is_not_an_admin(client, method, path, owner_a):
    reply = client.request(
        method, path.format(user_id=owner_a, feedback_id=uuid.uuid4()), json={}, headers=BOB
    )

    assert reply.status_code == 403


def test_disabling_a_user_ends_their_sessions_and_leaves_their_hardware(
    client, owner_b, bm_requirements, bm_rubric
):
    did, version = store.create_description(owner_b, "test", bm_requirements, bm_rubric)
    run_id = store.create_run(owner_b, did, version, None)
    store.record_resource(run_id, "lease-of-bob", "lease", "FAKE@BM", None)

    shown = client.patch(
        f"/admin/users/{owner_b}", json={"disabled": True, "reason": "a runaway script"},
        headers=ALICE,
    ).json()

    assert (shown["disabled_reason"], shown["open_sessions"]) == ("a runaway script", 0)
    assert client.get("/me", headers=BOB).status_code == 401
    assert [r.provider_id for r in store.live_resources(run_id)] == ["lease-of-bob"]


def test_an_enabled_user_may_log_in_again(client, owner_b):
    client.patch(f"/admin/users/{owner_b}", json={"disabled": True, "reason": "r"}, headers=ALICE)
    client.patch(f"/admin/users/{owner_b}", json={"disabled": False}, headers=ALICE)

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    store.start_session(owner_b, session_hash("bob-again"), tomorrow)
    again = {**BOB, "Cookie": "cc_session=bob-again"}

    assert client.get("/me", headers=again).status_code == 200


def test_a_session_started_as_the_account_is_disabled_is_not_honoured(owner_b):
    store.disable_user(owner_b, "a runaway script")

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    store.start_session(owner_b, session_hash("late-session"), tomorrow)

    assert store.session_user(session_hash("late-session")) is None


def test_disabling_needs_a_reason(client, owner_b):
    refused = client.patch(f"/admin/users/{owner_b}", json={"disabled": True}, headers=ALICE)

    assert refused.json() == {"detail": "give a reason for disabling the account"}
    assert store.disabled_reason(owner_b) is None


def test_an_admin_cannot_disable_themselves(client, owner_a):
    refused = client.patch(
        f"/admin/users/{owner_a}", json={"disabled": True, "reason": "oops"}, headers=ALICE
    )

    assert refused.json() == {"detail": "you cannot disable your own account"}


def test_ending_a_users_sessions_logs_them_out_everywhere(client, owner_b):
    assert client.delete(f"/admin/users/{owner_b}/sessions", headers=ALICE).status_code == 204

    assert store.session_user(session_hash("bob-session")) is None
    assert client.get(f"/admin/users/{owner_b}", headers=ALICE).json()["disabled_at"] is None


def test_an_override_replaces_a_default_until_it_is_cleared(client, owner_b):
    def messages_limit(reply) -> tuple:
        (quota,) = [q for q in reply.json()["quotas"] if q["name"] == "max_messages_per_day"]
        return quota["limit"], quota["override"]

    raised = client.patch(
        f"/admin/users/{owner_b}", json={"max_messages_per_day": 500}, headers=ALICE
    )
    cleared = client.patch(
        f"/admin/users/{owner_b}", json={"max_messages_per_day": None}, headers=ALICE
    )

    assert messages_limit(raised) == (500, 500)
    assert messages_limit(cleared) == (config().service.max_messages_per_day, None)


def test_the_users_table_marks_who_is_disabled_and_who_has_an_override(client, owner_b):
    client.patch(f"/admin/users/{owner_b}", json={"disabled": True, "reason": "r"}, headers=ALICE)
    client.patch(f"/admin/users/{owner_b}", json={"max_builds_per_user": 3}, headers=ALICE)

    users = client.get("/admin/users", headers=ALICE).json()

    assert [(u["user_id"], u["disabled_reason"], u["overridden"]) for u in users] == [
        ("chameleon:alice", None, False), ("chameleon:bob", "r", True)
    ]


def test_a_posted_announcement_replaces_the_last_and_every_user_sees_it(client):
    client.put("/admin/announcement", json={"text": "maintenance at noon"}, headers=ALICE)
    client.put("/admin/announcement", json={"text": "maintenance moved to 2pm"}, headers=ALICE)

    seen = client.get("/announcement", headers=BOB).json()

    assert seen["text"] == "maintenance moved to 2pm"


def test_a_cleared_announcement_is_shown_to_no_one(client):
    client.put("/admin/announcement", json={"text": "maintenance at noon"}, headers=ALICE)

    client.delete("/admin/announcement", headers=ALICE)

    assert client.get("/announcement", headers=BOB).json() is None


def test_the_feedback_list_shows_who_said_what_about_which_message(client, owner_b):
    thread = store.create_thread(owner_b, "bob's chat")
    said = store.append_message(
        thread.thread_id, "assistant", "answer", "Use a lease.", owner_id=owner_b
    )
    store.save_feedback(owner_b, thread.thread_id, said.message_id, None, "down", "wrong site")

    listed = client.get("/admin/feedback", headers=ALICE).json()["feedback"]

    assert [
        (f["username"], f["rating"], f["comment"], f["message_text"]) for f in listed
    ] == [("bob", "down", "wrong site", "Use a lease.")]


def test_the_feedback_list_comes_a_page_at_a_time(client, owner_b):
    size = config().service.feedback_page_sizes[0]
    thread = store.create_thread(owner_b, "bob's chat")
    for i in range(size + 1):
        store.save_feedback(owner_b, thread.thread_id, None, None, None, f"comment {i}")

    first = client.get("/admin/feedback", headers=ALICE).json()
    second = client.get("/admin/feedback", params={"page": 2}, headers=ALICE).json()

    assert (len(first["feedback"]), first["more"]) == (size, True)
    assert ([f["comment"] for f in second["feedback"]], second["more"]) == (["comment 0"], False)


def test_only_a_page_size_on_offer_can_be_chosen(client):
    offered = config().service.feedback_page_sizes[-1]

    chosen = client.get("/admin/feedback", params={"size": offered}, headers=ALICE)
    refused = client.get("/admin/feedback", params={"size": offered + 1}, headers=ALICE)

    assert chosen.json()["size"] == offered
    assert refused.status_code == 422


def test_a_feedback_detail_carries_the_conversations_behind_its_message(client, owner_b):
    thread = store.create_thread(owner_b, "bob's chat")
    routing = store.create_conversation(owner_b, "hi", [{"round": 1, "action": "route"}])
    answered = store.create_conversation(owner_b, "hi", [{"round": 1, "action": "answer"}])
    said = store.append_message(
        thread.thread_id, "assistant", "answer", "Use a lease.",
        {"routing_conversation_id": str(routing)}, owner_id=owner_b, conversation_id=answered,
    )
    saved = store.save_feedback(owner_b, thread.thread_id, said.message_id, None, "up", None)

    shown = client.get(f"/admin/feedback/{saved.feedback_id}", headers=ALICE).json()

    assert (shown["transcript"], shown["routing_transcript"]) == (
        [{"round": 1, "action": "answer"}], [{"round": 1, "action": "route"}]
    )


def test_a_feedback_detail_carries_its_runs_build_agent_turns(
    client, owner_b, bm_requirements, bm_rubric
):
    did, version = store.create_description(owner_b, "test", bm_requirements, bm_rubric)
    thread = store.create_thread(owner_b, "bob's chat")
    store.append_message(
        thread.thread_id, "assistant", "description", "a description",
        owner_id=owner_b, description_id=did, description_version=version,
    )
    run_id = store.create_run(owner_b, did, version, None)
    sent = [{"role": "user", "content": "the run failed"}]
    store.append_event(run_id, "agent", "model_turn", {"action": "retry"}, model_input=sent)
    saved = store.save_feedback(owner_b, thread.thread_id, None, run_id, "down", None)

    shown = client.get(f"/admin/feedback/{saved.feedback_id}", headers=ALICE).json()

    assert shown["build_turns"] == [{"action": "retry", "v": 1, "sent": sent}]
