"""Per-user limits."""

from __future__ import annotations

import uuid
from datetime import timezone

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations.api.deps import config
from tests.conftest import ALICE, TEST_DB_URL

pytestmark = pytest.mark.usefixtures("logged_in")


def _limit(owner: str, **overrides: int | None) -> None:
    store.update_user(owner, **overrides)


def _build_job(owner: str, description_id: uuid.UUID | None = None) -> None:
    thread = store.create_thread(owner, "a build")
    store.enqueue_job(
        owner, thread.thread_id, "build", {"description_id": str(description_id)}
    )


def _run_holding_hardware(owner: str, requirements, rubric, state: str) -> uuid.UUID:
    did, version = store.create_description(owner, "test", requirements, rubric)
    run_id = store.create_run(owner, did, version, None)
    store.record_resource(run_id, f"lease-{run_id}", "lease", "FAKE@BM", None)
    with psycopg.connect(TEST_DB_URL) as conn:
        conn.execute("UPDATE experiments.runs SET state = %s WHERE run_id = %s", [state, run_id])
    return run_id


def _approve(client, owner: str, requirements, rubric):
    thread_id = client.post("/threads", headers=ALICE).json()["thread_id"]
    store.update_thread(owner, uuid.UUID(thread_id), ssh_key_ref="cc-keypair")
    did, version = store.create_description(
        owner, "test", requirements.model_copy(update={"site": "FAKE@BM"}), rubric
    )
    return client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(did), "version": version},
        headers=ALICE,
    )


def test_a_message_past_the_daily_limit_is_refused_until_the_oldest_ages_out(
    client, dispatched, owner_a
):
    _limit(owner_a, max_messages_per_day=2)
    for _ in range(2):
        thread_id = client.post("/threads", headers=ALICE).json()["thread_id"]
        assert client.post(
            f"/threads/{thread_id}/messages", json={"text": "hello"}, headers=ALICE
        ).status_code == 202
    with psycopg.connect(TEST_DB_URL) as conn:
        row = conn.execute(
            "SELECT min(created_at) + interval '1 day' FROM chat.messages"
        ).fetchone()
    assert row is not None
    after = row[0].astimezone(timezone.utc)

    thread_id = client.post("/threads", headers=ALICE).json()["thread_id"]

    refused = client.post(f"/threads/{thread_id}/messages", json={"text": "hi"}, headers=ALICE)

    assert refused.status_code == 429
    assert refused.json() == {
        "detail": "you have sent 2 messages in the last 24 hours; "
                  f"send another after {after:%Y-%m-%d %H:%M} UTC"
    }
    assert len(dispatched) == 2


def test_a_build_past_the_builds_limit_is_refused(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    _limit(owner_a, max_builds_per_user=2)
    _build_job(owner_a)
    _build_job(owner_a)

    refused = _approve(client, owner_a, bm_requirements, bm_rubric)

    assert refused.status_code == 409
    assert refused.json() == {
        "detail": "you have 2 builds running, the most allowed; wait for one to finish"
    }
    assert dispatched == []


def test_runs_holding_hardware_and_unfinished_builds_are_active_experiments(
    owner_a, bm_requirements, bm_rubric
):
    _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "passed")
    _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "provisioning")
    building = _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "provisioning")
    released = _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "failed")
    store.release_resource_with_event(released, "lease", f"lease-{released}", "FAKE@BM")
    _build_job(owner_a, store.get_run(owner_a, building).description_id)

    with store.transaction() as conn:
        used = store.usage(owner_a, conn)

    assert (used.active_experiments, used.builds_running) == (3, 1)


def test_a_build_past_the_active_experiments_limit_is_refused(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    _limit(owner_a, max_active_experiments_per_user=1)
    _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "passed")

    refused = _approve(client, owner_a, bm_requirements, bm_rubric)

    assert refused.status_code == 409
    assert refused.json() == {
        "detail": "you have 1 active experiments, the most allowed; release one from your profile"
    }
    assert dispatched == []


def test_the_profile_shows_each_limit_and_the_experiments_holding_hardware(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    _limit(owner_a, max_builds_per_user=5)
    run_id = _run_holding_hardware(owner_a, bm_requirements, bm_rubric, "passed")
    thread = store.create_thread(owner_a, "an A100 for a day")
    store.append_message(
        thread.thread_id, "assistant", "description", "a description", owner_id=owner_a,
        description_id=store.get_run(owner_a, run_id).description_id, description_version=1,
    )

    profile = client.get("/profile", headers=ALICE).json()

    quotas = profile["quotas"]
    service = config().service
    messages, experiments = service.max_messages_per_day, service.max_active_experiments_per_user
    assert [(q["name"], q["used"], q["limit"], q["default"], q["override"]) for q in quotas] == [
        ("max_messages_per_day", 0, messages, messages, None),
        ("max_builds_per_user", 0, 5, service.max_builds_per_user, 5),
        ("max_active_experiments_per_user", 1, experiments, experiments, None),
    ]
    (active,) = profile["active_experiments"]
    assert (active["run_id"], active["state"], active["site"], active["title"]) == (
        str(run_id), "passed", "FAKE@BM", "an A100 for a day"
    )


def _send(client) -> int:
    thread_id = client.post("/threads", headers=ALICE).json()["thread_id"]
    sent = client.post(f"/threads/{thread_id}/messages", json={"text": "hello"}, headers=ALICE)
    return sent.status_code


def test_a_limit_for_everyone_holds_a_user_with_no_override(client, dispatched):
    store.set_everyone_limits(max_messages_per_day=1)

    assert (_send(client), _send(client)) == (202, 429)


def test_a_user_with_their_own_limit_gets_it_not_the_limit_for_everyone(
    client, dispatched, owner_a
):
    store.set_everyone_limits(max_messages_per_day=1)
    _limit(owner_a, max_messages_per_day=2)

    assert (_send(client), _send(client)) == (202, 202)
