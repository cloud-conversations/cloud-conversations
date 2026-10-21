"""Local test users: made with ccx, logged in with a password, and kept away from Chameleon."""

from __future__ import annotations

import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

import cloud_conversations.db.store as store
from cloud_conversations import local_users, oidc
from cloud_conversations.adapter.credentials import CredentialsError
from cloud_conversations.api.app import app
from cloud_conversations.worker import runs
from tests.conftest import LOGIN, PUBLISHED_CONFIG, TEST_DB_URL

PASSWORD = "correct horse battery"
PAGE = {"Origin": LOGIN["CC_PUBLIC_URL"]}


def _local_login(tmp_path, monkeypatch, on: bool) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[adapters.chameleon]\n\n[service]\n"
        f'local_login = {str(on).lower()}\nlocal_users_testbed = "chameleon"\n'
    )
    monkeypatch.setenv("CC_CONFIG_FILE", str(path))


@pytest.fixture()
def local_login(tmp_path, monkeypatch):
    _local_login(tmp_path, monkeypatch, on=True)


def _log_in(client, name: str, password: str):
    return client.post("/auth/local", json={"name": name, "password": password}, headers=PAGE)


def test_a_local_user_logs_in_without_the_keycloak_settings(local_login, monkeypatch):
    for env in oidc.env_names("chameleon"):
        monkeypatch.delenv(env)
    local_users.add("chameleon", "alice", PASSWORD)
    client = TestClient(app)

    assert _log_in(client, "alice", PASSWORD).status_code == 204
    assert client.get("/me").json()["username"] == "local:alice"


def test_a_wrong_password_and_an_unknown_user_get_the_same_reply(local_login, client):
    local_users.add("chameleon", "alice", PASSWORD)

    wrong = _log_in(client, "alice", "not the password at all")
    unknown = _log_in(client, "nobody", PASSWORD)

    assert (wrong.status_code, wrong.json()) == (401, {"detail": "wrong username or password"})
    assert (unknown.status_code, unknown.json()) == (wrong.status_code, wrong.json())


def test_a_disabled_local_user_is_refused_with_the_reason(local_login, client):
    local_users.add("chameleon", "alice", PASSWORD)
    store.disable_user(local_users.user_id("chameleon", "alice"), "done testing")

    refused = _log_in(client, "alice", PASSWORD)

    assert refused.status_code == 403
    assert refused.json() == {"detail": "an admin disabled this account: done testing"}


def test_with_local_login_off_there_is_no_password_login(client, tmp_path, monkeypatch):
    _local_login(tmp_path, monkeypatch, on=False)
    local_users.add("chameleon", "alice", PASSWORD)

    assert _log_in(client, "alice", PASSWORD).status_code == 404
    assert client.get("/auth/methods").json() == {"local": False, "logins": []}


def test_an_admin_named_alice_is_not_the_local_user_alice(local_login, monkeypatch):
    monkeypatch.setenv("CC_ADMINS", "chameleon:alice")
    local_users.add("chameleon", "alice", PASSWORD)
    client = TestClient(app)
    _log_in(client, "alice", PASSWORD)

    assert client.get("/me").json()["admin"] is False


def test_the_password_is_neither_stored_nor_logged(local_login, caplog):
    caplog.set_level(logging.DEBUG)
    local_users.add("chameleon", "alice", PASSWORD)
    _log_in(TestClient(app), "alice", PASSWORD)

    with psycopg.connect(TEST_DB_URL) as conn:
        rows = conn.execute("SELECT * FROM auth.users").fetchall()
    assert PASSWORD not in str(rows)
    assert PASSWORD not in caplog.text


def test_a_short_password_is_refused():
    with pytest.raises(ValueError) as raised:
        local_users.add("chameleon", "alice", "too short")
    assert str(raised.value) == "a password must be at least 12 characters"


def test_a_name_that_could_not_be_typed_at_the_page_is_refused():
    with pytest.raises(ValueError) as raised:
        local_users.add("chameleon", "alice smith", PASSWORD)
    assert str(raised.value) == "'alice smith' is not 1 to 64 letters, digits, '_', '.' or '-'"


def test_a_second_local_user_of_the_same_name_is_refused():
    local_users.add("chameleon", "alice", PASSWORD)
    with pytest.raises(store.ConflictError):
        local_users.add("chameleon", "alice", PASSWORD)


def test_a_new_password_replaces_the_old():
    local_users.add("chameleon", "alice", PASSWORD)

    local_users.set_password("chameleon", "alice", "a different password")

    assert local_users.check("chameleon", "alice", PASSWORD) is None
    assert local_users.check("chameleon", "alice", "a different password") == (
        "chameleon:local:alice"
    )


def test_a_password_cannot_be_given_to_a_chameleon_user(owner_a):
    with pytest.raises(store.NotFoundError):
        store.set_local_password(owner_a, "a-hash")


def test_a_local_users_build_stops_before_chameleon(bm_requirements, bm_rubric):
    local_users.add("chameleon", "alice", PASSWORD)
    thread = store.create_thread(
        "chameleon:local:alice", "New chat", ssh_key_ref="cc-keypair"
    )
    did, version = store.create_description(
        "chameleon:local:alice",
        "test",
        bm_requirements.model_copy(update={"site": "CHI@UC"}),
        bm_rubric,
    )
    job = store.enqueue_job(
        "chameleon:local:alice", thread.thread_id, "build",
        {"description_id": str(did), "version": version},
    )

    with pytest.raises(CredentialsError, match="this account cannot reach Chameleon"):
        runs.build_experiment(job, PUBLISHED_CONFIG)
    assert store.runs_for_description("chameleon:local:alice", did) == []
