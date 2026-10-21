"""One user never sees or changes another's data, in the database, the store, or the API."""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi.routing import APIRoute

import cloud_conversations.db.store as store
from cloud_conversations import releases
from cloud_conversations.api.app import app
from cloud_conversations.api.deps import session_hash
from tests.conftest import ALICE, BOB, LOGIN, TEST_DB_URL

pytestmark = pytest.mark.usefixtures("logged_in")

CAROLS_ID = "chameleon:local:carol"
ALICE_ELSEWHERE = {"Cookie": "cc_session=alice-elsewhere-session", "Origin": LOGIN["CC_PUBLIC_URL"]}

# Text found only in Alice's data.
_ALICES_TITLE = "alice's own chat"
_ALICES_LEASE = "lease-only-alice-holds"


@pytest.fixture()
def carol() -> None:
    """A local test user."""
    store.add_local_user(CAROLS_ID, "not-a-real-hash")


@pytest.fixture()
def alice_elsewhere() -> None:
    """An account named alice at another testbed, logged in."""
    store.record_login(
        "other:alice",
        "alice",
        [{"id": "OTHER-000001", "nickname": ""}],
        "a refresh token",
        session_hash("alice-elsewhere-session"),
        datetime.now(timezone.utc) + timedelta(days=1),
        email=None,
    )


@pytest.fixture()
def two_testbeds(tmp_path, monkeypatch, fresh_config) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[adapters.chameleon]\nlabel = "Chameleon"\n'
        "[adapters.chameleon.login]\n"
        '[adapters.chameleon.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.chameleon.sites."CHI@UC"]\nkinds = ["bm"]\n'
        '[adapters.other]\nlabel = "Other"\n'
        "[adapters.other.login]\n"
        '[adapters.other.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.other.sites."OTHER@ONE"]\nkinds = ["bm"]\n'
    )
    monkeypatch.setenv("CC_CONFIG_FILE", str(path))


@pytest.fixture()
def alices(owner_a, bm_requirements, bm_rubric) -> dict[str, str]:
    """A chat, a description, a run with a lease and its release, and a key, all Alice's."""
    thread = store.create_thread(owner_a, _ALICES_TITLE)
    did, version = store.create_description(owner_a, "test", bm_requirements, bm_rubric)
    store.append_message(
        thread.thread_id, "assistant", "description", "a description",
        owner_id=owner_a, description_id=did, description_version=version,
    )
    run_id = store.create_run(owner_a, did, version, None)
    store.record_resource(run_id, _ALICES_LEASE, "lease", "FAKE@BM", None)
    request_id = releases.offer(
        owner_a, store.get_run(owner_a, run_id), "nothing will help", {"action": "abandon"},
        None,
    )
    key = store.import_user_key(owner_a, "laptop", "cc-laptop-a1b2c3", "ssh-ed25519 AAAA alice")
    trashed = store.create_thread(owner_a, _ALICES_TITLE)
    store.delete_thread(owner_a, trashed.thread_id)
    return {
        "thread_id": str(thread.thread_id),
        "trashed_thread_id": str(trashed.thread_id),
        "description_id": str(did),
        "version": str(version),
        "run_id": str(run_id),
        "request_id": str(request_id),
        "name": key.name,
        "keypair_name": key.keypair_name,
    }


# Every route that takes an ID, and the body it is called with.
_ROUTES = [
    ("GET", "/threads/{thread_id}", None),
    ("PATCH", "/threads/{thread_id}", {"ssh_key_ref": "anything"}),
    ("DELETE", "/threads/{thread_id}", None),
    ("POST", "/threads/{thread_id}/messages", {"text": "hello"}),
    ("POST", "/threads/{thread_id}/builds", {"description_id": "{description_id}", "version": 1}),
    ("POST", "/threads/{thread_id}/feedback", {"run_id": "{run_id}", "rating": "up"}),
    ("GET", "/runs/{run_id}", None),
    ("GET", "/runs/{run_id}/events", None),
    ("POST", "/runs/{run_id}/teardown",
     {"resources": [{"kind": "lease", "provider_id": _ALICES_LEASE, "site": "FAKE@BM"}]}),
    ("POST", "/releases/{request_id}/confirm", None),
    ("POST", "/releases/{request_id}/decline", None),
    ("DELETE", "/keys/{name}", None),
    ("POST", "/trash/{thread_id}/restore", None),
]


def _snapshot() -> dict[str, list[str]]:
    """Every row of every table a user's data lives in."""
    tables = [
        "chat.threads", "chat.messages", "chat.jobs", "chat.conversations", "chat.feedback",
        "experiments.experiment_descriptions", "experiments.runs",
        "experiments.run_resources", "experiments.run_events", "auth.user_keys",
        "audit.decisions", "audit.testbed_calls",
    ]
    with psycopg.connect(TEST_DB_URL) as conn:
        return {
            t: sorted(str(row) for row in conn.execute(f"SELECT * FROM {t}").fetchall())
            for t in tables
        }


_ROUTE_IDS = [f"{method} {path}" for method, path, _ in _ROUTES]


def _call(client, method, path, body, alices, headers):
    if path.startswith("/trash/"):
        alices = {**alices, "thread_id": alices["trashed_thread_id"]}
    if body is not None:
        body = {k: v.format(**alices) if isinstance(v, str) else v for k, v in body.items()}
    return client.request(method, path.format(**alices), json=body, headers=headers)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
def test_alice_reaches_her_own_data_through_each_route(
    client, dispatched, alices, method, path, body
):
    assert _call(client, method, path, body, alices, ALICE).status_code not in (401, 403, 404)


@pytest.mark.parametrize(("method", "path", "body"), _ROUTES, ids=_ROUTE_IDS)
def test_bob_reaches_none_of_alices_data_through_a_route(
    client, dispatched, alices, method, path, body
):
    before = _snapshot()

    reply = _call(client, method, path, body, alices, BOB)

    assert reply.status_code == 404
    assert _snapshot() == before
    assert dispatched == []
    for marker in (_ALICES_TITLE, _ALICES_LEASE, alices["keypair_name"], "chameleon:alice"):
        assert marker not in reply.text


def test_an_account_sees_only_its_own_testbeds_sites(client, two_testbeds, alice_elsewhere):
    profile = client.get("/profile", headers=ALICE_ELSEWHERE).json()

    assert (profile["testbed"], profile["sites"]) == ("Other", ["OTHER@ONE"])
    assert client.get("/profile", headers=ALICE).json()["sites"] == ["CHI@UC"]


def test_an_account_cannot_reach_a_site_of_another_testbed(client, two_testbeds, alice_elsewhere):
    refused = client.get("/profile/keypairs", params={"site": "CHI@UC"}, headers=ALICE_ELSEWHERE)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "CHI@UC is not a site of Other"}


def test_every_route_that_takes_an_id_is_in_the_separation_table():
    taking_an_id = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute) and "{" in route.path and not route.path.startswith("/admin")
        for method in route.methods or ()
    }
    assert taking_an_id == {(method, path) for method, path, _ in _ROUTES}


# Store functions the runner calls with IDs it already owns.
_RUNNER_ONLY = {
    "get_run_unscoped", "touch_heartbeat", "record_resource", "create_resource_with_event",
    "resource_by_idempotency_key", "release_resource_with_event",
    "start_job", "route_job", "finish_job",
}
_WORKER_MAY = {"start_job", "route_job", "finish_job"}


def _store_calls(package: str) -> set[str]:
    paths = list((Path(__file__).parents[1] / "src/cloud_conversations" / package).glob("*.py"))
    assert paths
    called: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "store"
            ):
                called.add(node.attr)
    return called


def test_the_api_calls_no_store_function_that_skips_the_owner():
    assert _store_calls("api") & _RUNNER_ONLY == set()


def test_the_workers_call_only_the_job_updates_that_skip_the_owner():
    assert _store_calls("worker") & _RUNNER_ONLY <= _WORKER_MAY


def test_a_row_for_someone_who_is_not_a_user_is_refused(bm_requirements, bm_rubric):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.create_description("nobody", "test", bm_requirements, bm_rubric)


def test_a_run_of_another_owners_description_is_refused(alices, owner_b):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.create_run(owner_b, uuid.UUID(alices["description_id"]), 1, None)


def test_a_conversation_about_another_owners_description_is_refused(alices, owner_b):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        store.create_conversation(
            owner_b, "mine now", [], uuid.UUID(alices["description_id"]), 1
        )


def test_another_owners_run_cannot_be_rated_from_ones_own_chat(alices, owner_b):
    bobs = store.create_thread(owner_b, "bob's chat")

    with pytest.raises(store.NotFoundError):
        store.save_feedback(owner_b, bobs.thread_id, None, uuid.UUID(alices["run_id"]), "up", None)


def test_a_job_on_another_owners_chat_is_refused(alices, owner_b):
    with psycopg.connect(TEST_DB_URL) as conn, pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO chat.jobs (job_id, thread_id, owner_id, kind, state) "
            "VALUES (%s, %s, %s, 'exchange', 'queued')",
            [uuid.uuid4(), alices["thread_id"], owner_b],
        )


def test_deleting_a_local_user_takes_everything_of_theirs_and_nothing_else(
    carol, alices, bm_requirements, bm_rubric
):
    store.create_thread(CAROLS_ID, "carol's chat")
    did, version = store.create_description(CAROLS_ID, "test", bm_requirements, bm_rubric)
    store.create_run(CAROLS_ID, did, version, None)
    before = _snapshot()

    store.delete_local_user(CAROLS_ID)

    after = _snapshot()
    assert all(CAROLS_ID not in row for rows in after.values() for row in rows)
    assert [r for r in after["chat.threads"] if "chameleon:alice" in r] == [
        r for r in before["chat.threads"] if "chameleon:alice" in r
    ]
    assert after["experiments.run_resources"] == before["experiments.run_resources"]
    with pytest.raises(store.NotFoundError):
        store.get_user(CAROLS_ID)


def test_a_local_user_holding_a_live_resource_is_not_deleted(carol, bm_requirements, bm_rubric):
    did, version = store.create_description(CAROLS_ID, "test", bm_requirements, bm_rubric)
    run_id = store.create_run(CAROLS_ID, did, version, None)
    store.record_resource(run_id, "lease-of-carol", "lease", "FAKE@BM", None)

    with pytest.raises(store.ConflictError):
        store.delete_local_user(CAROLS_ID)
    assert store.get_user(CAROLS_ID).username == "local:carol"


def test_only_a_local_user_can_be_deleted_as_one(owner_a):
    with pytest.raises(store.NotFoundError):
        store.delete_local_user(owner_a)
    assert store.get_user(owner_a).user_id == owner_a
