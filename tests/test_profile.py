"""The profile a user manages, and the users page an admin sees."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations.adapter.errors import TransientError
from cloud_conversations.api import app as api
from cloud_conversations.api.deps import config, session_hash
from cloud_conversations.audit import audited
from tests.conftest import ALICE, BOB, TEST_DB_URL

pytestmark = pytest.mark.usefixtures("logged_in")

TWO_PROJECTS = [{"id": "CHI-000001", "nickname": ""}, {"id": "CHI-000002", "nickname": "two"}]


@pytest.fixture()
def alice_in_two_projects(owner_a) -> None:
    store.record_login(
        owner_a,
        owner_a,
        TWO_PROJECTS,
        "a refresh token",
        session_hash("a-second-session"),
        datetime.now(timezone.utc) + timedelta(days=1),
        email=None,
    )


def test_the_profile_holds_the_users_projects_and_imported_keys(client, owner_a):
    import paramiko

    public_key = f"ssh-rsa {paramiko.RSAKey.generate(2048).get_base64()} alice"
    store.import_user_key(owner_a, "laptop", "cc-laptop-a1b2c3", public_key)
    store.import_user_key("chameleon:bob", "laptop", "cc-laptop-b0b0b0", public_key)

    profile = client.get("/profile", headers=ALICE).json()

    assert (profile["testbed"], profile["username"]) == ("Chameleon", "alice")
    assert profile["projects"] == [{"id": "CHI-000001", "nickname": ""}]
    assert profile["default_project_id"] is None
    assert [k["keypair_name"] for k in profile["keys"]] == ["cc-laptop-a1b2c3"]
    assert profile["sites"] == config().sites_at("chameleon")


def test_new_chats_build_in_the_default_project(client, alice_in_two_projects):
    set_to = client.patch("/profile", json={"default_project_id": "CHI-000002"}, headers=ALICE)

    assert set_to.json()["default_project_id"] == "CHI-000002"
    assert client.post("/threads", headers=ALICE).json()["project_id"] == "CHI-000002"


def test_the_default_project_is_one_the_user_is_in(client, owner_a):
    refused = client.patch("/profile", json={"default_project_id": "CHI-999999"}, headers=ALICE)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "you are not in Chameleon project CHI-999999"}
    assert store.get_user(owner_a).default_project_id is None


class _Sites:
    """An adapter that has a keypair at CHI@UC, none at CHI@TACC, and fails at KVM@TACC."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str]] = []

    def list_keypairs(self, site):
        from cloud_conversations.adapter.protocol import Keypair

        return [Keypair(name="cc-laptop-a1b2c3", public_key="ssh-ed25519 AAAA a")]

    def delete_keypair(self, site, name):
        if site == "KVM@TACC":
            raise TransientError("KVM@TACC is unreachable")
        self.deleted.append((site, name))
        return site == "CHI@UC"


@pytest.fixture()
def sites(monkeypatch) -> _Sites:
    stub = _Sites()
    monkeypatch.setattr(api.credentials, "build_adapter", lambda site, config, user=None: stub)
    return stub


def test_the_keypairs_at_a_site_are_listed_by_name(client, sites):
    listed = client.get("/profile/keypairs", params={"site": "CHI@UC"}, headers=ALICE)

    assert listed.json() == [{"name": "cc-laptop-a1b2c3"}]


def test_forgetting_a_key_leaves_its_keypairs_at_the_sites(client, sites, owner_a):
    store.import_user_key(owner_a, "laptop", "cc-laptop-a1b2c3", "ssh-ed25519 AAAA a")
    store.import_user_key("chameleon:bob", "laptop", "cc-laptop-b0b0b0", "ssh-ed25519 AAAA b")

    forgot = client.delete("/keys/laptop", headers=ALICE)

    assert forgot.json() == {"name": "laptop", "keypair_name": "cc-laptop-a1b2c3", "sites": []}
    assert sites.deleted == []
    assert store.user_keys(owner_a) == []
    assert [k.keypair_name for k in store.user_keys("chameleon:bob")] == ["cc-laptop-b0b0b0"]


def test_deleting_a_key_everywhere_reports_each_site(client, sites, owner_a):
    store.import_user_key(owner_a, "laptop", "cc-laptop-a1b2c3", "ssh-ed25519 AAAA a")

    deleted = client.delete("/keys/laptop", params={"at_sites": "true"}, headers=ALICE)

    assert deleted.json()["sites"] == [
        {"site": "CHI@TACC", "outcome": "absent", "error": None},
        {"site": "CHI@UC", "outcome": "deleted", "error": None},
        {"site": "KVM@TACC", "outcome": "failed", "error": "KVM@TACC is unreachable"},
    ]
    assert store.user_keys(owner_a) == []


def test_deleting_a_key_everywhere_is_in_the_audit_trail(client, monkeypatch, owner_a):
    def adapter(site, config, user=None):
        return audited(_Sites())  # type: ignore[arg-type]

    monkeypatch.setattr(api.credentials, "build_adapter", adapter)
    store.import_user_key(owner_a, "laptop", "cc-laptop-a1b2c3", "ssh-ed25519 AAAA a")

    client.delete("/keys/laptop", params={"at_sites": "true"}, headers=ALICE)

    with psycopg.connect(TEST_DB_URL) as conn:
        decided = conn.execute(
            "SELECT step, actor, via, action, request_id FROM audit.decisions "
            "WHERE owner_id = %s ORDER BY seq",
            [owner_a],
        ).fetchall()
        calls = conn.execute(
            "SELECT site, operation, provider_id, outcome, error, request_id "
            "FROM audit.testbed_calls WHERE owner_id = %s AND outcome <> 'started' ORDER BY seq",
            [owner_a],
        ).fetchall()
    assert [d[:4] for d in decided] == [
        ("proposed", "user", "web", "delete_keypair"),
        ("confirmed", "user", "web", "delete_keypair"),
    ]
    request_id = decided[0][4]
    assert calls == [
        ("CHI@TACC", "delete_keypair", "cc-laptop-a1b2c3", "ok", None, request_id),
        ("CHI@UC", "delete_keypair", "cc-laptop-a1b2c3", "ok", None, request_id),
        ("KVM@TACC", "delete_keypair", "cc-laptop-a1b2c3", "failed",
         "TransientError: KVM@TACC is unreachable", request_id),
    ]


def test_an_admin_sees_each_users_logins_and_chat_counts(client, alice_is_an_admin, owner_a):
    store.create_thread(owner_a, "a chat")
    store.ensure_user("chameleon:carol")

    users = client.get("/admin/users", headers=ALICE).json()

    assert [(u["testbed"], u["username"]) for u in users] == [
        ("Chameleon", "alice"), ("Chameleon", "bob"), ("Chameleon", "carol")
    ]
    assert set(users[0]) == {
        "user_id", "testbed", "username", "email", "projects", "local", "first_login_at",
        "last_login_at", "checked_at", "disabled_at", "disabled_reason", "open_sessions",
        "chats", "latest_chat_at", "overridden",
    }
    assert (users[0]["open_sessions"], users[0]["chats"]) == (1, 1)
    assert (users[1]["chats"], users[1]["latest_chat_at"]) == (0, None)
    assert users[0]["first_login_at"] is not None
    assert (users[2]["first_login_at"], users[2]["last_login_at"]) == (None, None)


def test_a_later_login_moves_the_last_login_but_not_the_first(owner_a):
    def logins() -> tuple:
        (alice,) = [u for u in store.user_summaries() if u.username == "alice"]
        return alice.first_login_at, alice.last_login_at

    first, _ = logins()
    store.start_session(owner_a, session_hash("a-later-session"), datetime.now(timezone.utc))

    assert logins()[0] == first
    assert logins()[1] > first


def test_a_new_chat_starts_with_the_default_keypair(client):
    client.patch("/profile", json={"default_ssh_key_ref": "cc-laptop-a1b2c3"}, headers=ALICE)

    created = client.post("/threads", headers=ALICE).json()

    assert created["ssh_key_ref"] == "cc-laptop-a1b2c3"
    assert client.post("/threads", headers=BOB).json()["ssh_key_ref"] is None


def test_the_default_keypair_can_be_cleared(client, owner_a):
    client.patch("/profile", json={"default_ssh_key_ref": "cc-laptop-a1b2c3"}, headers=ALICE)

    cleared = client.patch("/profile", json={"default_ssh_key_ref": None}, headers=ALICE)

    assert cleared.json()["default_ssh_key_ref"] is None
    assert store.get_user(owner_a).default_ssh_key_ref is None


def test_setting_the_default_keypair_leaves_the_default_project(client, alice_in_two_projects):
    client.patch("/profile", json={"default_project_id": "CHI-000002"}, headers=ALICE)

    client.patch("/profile", json={"default_ssh_key_ref": "cc-laptop-a1b2c3"}, headers=ALICE)

    assert client.get("/profile", headers=ALICE).json()["default_project_id"] == "CHI-000002"


def test_the_default_project_cannot_be_cleared(client):
    refused = client.patch("/profile", json={"default_project_id": None}, headers=ALICE)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "name a project"}


def test_each_default_site_picker_lists_the_sites_of_its_kind(client):
    profile = client.get("/profile", headers=ALICE).json()

    kinds = {
        site: {k.reservation_kind for k in config().kinds_at(site)}
        for site in config().sites_at("chameleon")
    }
    assert profile["vm_sites"] == [s for s, k in kinds.items() if "flavor_instance" in k]
    assert profile["baremetal_sites"] == [s for s, k in kinds.items() if "physical_host" in k]


def test_a_default_vm_site_must_offer_vms(client, owner_a):
    refused = client.patch("/profile", json={"default_vm_site": "CHI@UC"}, headers=ALICE)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "CHI@UC offers no VMs"}
    assert store.get_user(owner_a).default_vm_site is None


def test_a_default_bare_metal_site_is_set_and_cleared(client, owner_a):
    set_to = client.patch("/profile", json={"default_baremetal_site": "CHI@UC"}, headers=ALICE)
    cleared = client.patch("/profile", json={"default_baremetal_site": None}, headers=ALICE)

    assert set_to.json()["default_baremetal_site"] == "CHI@UC"
    assert cleared.json()["default_baremetal_site"] is None


def test_a_default_project_the_user_left_is_not_used(client, alice_in_two_projects, owner_a):
    client.patch("/profile", json={"default_project_id": "CHI-000002"}, headers=ALICE)
    store.update_login(owner_a, "alice", [{"id": "CHI-000001", "nickname": ""}])

    assert client.post("/threads", headers=ALICE).json()["project_id"] == "CHI-000001"


def test_a_new_chat_starts_with_the_default_keypairs_at_each_site(client):
    at_sites = {site: f"key-at-{site}" for site in config().sites_at("chameleon")}
    client.patch("/profile", json={"default_site_keypairs": at_sites}, headers=ALICE)

    created = client.post("/threads", headers=ALICE).json()

    assert (created["ssh_key_ref"], created["site_keypairs"]) == (None, at_sites)
