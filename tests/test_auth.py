"""Logging in with Chameleon, against a Keycloak stand-in."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

import cloud_conversations.db.store as store
from cloud_conversations import crypt, oidc
from cloud_conversations.api.app import app
from cloud_conversations.api.deps import config, public_url, session_hash
from cloud_conversations.config import ConfigError
from tests.conftest import ALICE, LOGIN, TEST_DB_URL

ISSUER = LOGIN["CC_OIDC_CHAMELEON_ISSUER"]
PAGE = LOGIN["CC_PUBLIC_URL"]
CLIENT_ID = LOGIN["CC_OIDC_CHAMELEON_CLIENT_ID"]
PROJECTS = [{"id": "CHI-000001", "nickname": "mine"}]


@pytest.fixture()
def keycloak(monkeypatch):
    """Every call made to Keycloak, and the userinfo it answers with."""
    config.cache_clear()
    oidc.endpoints.cache_clear()
    calls: list[dict] = []
    userinfo = {
        "sub": "sub-alice",
        "preferred_username": "alice",
        "email": "alice@example.org",
        "projects": PROJECTS,
    }
    published = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
        "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
        "userinfo_endpoint": f"{ISSUER}/protocol/openid-connect/userinfo",
        "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
    }

    def answer(url, timeout_s, *, form=None, bearer=None):
        calls.append({"url": url, "form": form, "bearer": bearer})
        if url.endswith("/.well-known/openid-configuration"):
            return published
        if url == published["token_endpoint"]:
            return {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "refresh_expires_in": 1800,
            }
        if url == published["userinfo_endpoint"]:
            return userinfo
        raise AssertionError(url)

    monkeypatch.setattr(oidc, "call", answer)
    yield {"calls": calls, "userinfo": userinfo, "published": published}
    config.cache_clear()
    oidc.endpoints.cache_clear()


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _refusal(client, back) -> str:
    """The message the login page shows after a refused login. Fails unless back sent it there."""
    assert back.headers["location"] == f"{PAGE}/login"
    refused: str = client.get("/auth/methods").json()["refused"]
    return refused


def _log_in(client):
    """Log alice in through Keycloak. Returns the callback's reply."""
    sent = _start(client)
    return client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})


def _start(client, next_path: str = "/threads/t1") -> dict[str, str]:
    """Begin a login. Returns the query it sends to Keycloak."""
    sent = client.get("/auth/login", params={"testbed": "chameleon", "next": next_path})
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(sent.headers["location"]).query))


def test_a_login_leaves_for_keycloak_with_a_challenge_for_the_verifier_it_keeps(
    client, keycloak
):
    sent = _start(client)

    pending = json.loads(crypt.decrypt(client.cookies["cc_login"]))
    digest = hashlib.sha256(pending["verifier"].encode()).digest()
    assert sent["client_id"] == CLIENT_ID
    assert sent["redirect_uri"] == f"{PAGE}/api/auth/callback"
    assert sent["state"] == pending["state"]
    assert sent["code_challenge_method"] == "S256"
    assert sent["code_challenge"] == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_finishing_a_login_starts_a_session_for_the_keycloak_user(client, keycloak):
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert back.status_code == 307
    assert back.headers["location"] == f"{PAGE}/threads/t1"
    user = store.session_user(session_hash(client.cookies["cc_session"]))
    assert user is not None
    assert (user.user_id, user.username, user.projects) == (
        "chameleon:sub-alice", "alice", PROJECTS
    )


def test_the_users_email_is_kept_from_keycloak(client, keycloak):
    sent = _start(client)

    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert store.user_summaries("chameleon:sub-alice")[0].email == "alice@example.org"


def test_a_login_without_an_email_still_starts_a_session(client, keycloak):
    del keycloak["userinfo"]["email"]
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert back.status_code == 307
    assert store.user_summaries("chameleon:sub-alice")[0].email is None


def test_the_session_cookie_is_kept_from_scripts_and_other_sites(client, keycloak):
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    (cookie,) = [c for c in back.headers.get_list("set-cookie") if c.startswith("cc_session=")]
    flags = {flag.strip().lower() for flag in cookie.split(";")[1:]}
    assert {"httponly", "samesite=lax"} <= flags
    assert "secure" not in flags


def test_the_session_cookie_is_secure_and_host_only_behind_an_https_page(monkeypatch, keycloak):
    monkeypatch.setenv("CC_PUBLIC_URL", "https://page.example.org")
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    (cookie,) = [
        c for c in back.headers.get_list("set-cookie") if c.startswith("__Host-cc_session=")
    ]
    assert "secure" in {flag.strip().lower() for flag in cookie.split(";")[1:]}


def test_an_https_session_is_read_from_its_host_only_cookie(monkeypatch, keycloak):
    monkeypatch.setenv("CC_PUBLIC_URL", "https://page.example.org")
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    sent = _start(client)
    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    me = client.get("/me")

    assert (me.status_code, me.json()["username"]) == (200, "alice")


def test_a_session_ends_when_its_keycloak_login_does(client, keycloak):
    sent = _start(client)

    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    with psycopg.connect(TEST_DB_URL) as conn:
        row = conn.execute("SELECT expires_at FROM auth.sessions").fetchone()
    assert row is not None
    left = row[0] - datetime.now(timezone.utc)
    assert timedelta(seconds=1790) < left <= timedelta(seconds=1800)


def test_the_code_is_exchanged_with_the_secret_and_the_verifier(client, keycloak):
    sent = _start(client)
    verifier = json.loads(crypt.decrypt(client.cookies["cc_login"]))["verifier"]

    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    (exchange,) = [c for c in keycloak["calls"] if c["form"] is not None]
    assert exchange["form"] == {
        "grant_type": "authorization_code",
        "code": "code-1",
        "redirect_uri": f"{PAGE}/api/auth/callback",
        "client_id": CLIENT_ID,
        "client_secret": "client-secret",
        "code_verifier": verifier,
    }


def test_the_refresh_token_is_stored_encrypted(client, keycloak):
    sent = _start(client)

    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    with psycopg.connect(TEST_DB_URL) as conn:
        row = conn.execute(
            "SELECT refresh_token FROM auth.refresh_tokens WHERE user_id = 'chameleon:sub-alice'"
        ).fetchone()
    assert row is not None
    (stored,) = row
    assert stored != "refresh-1"
    assert crypt.decrypt(stored) == "refresh-1"


def test_a_callback_whose_state_does_not_match_starts_no_session(client, keycloak):
    _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": "forged"})

    assert _refusal(client, back) == "this login expired or did not start here; log in again"
    assert "cc_session" not in client.cookies


def test_someone_in_no_project_is_refused(client, keycloak):
    keycloak["userinfo"]["projects"] = []
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert _refusal(client, back) == (
        "your Chameleon account belongs to no project; join one, then log in again"
    )
    assert "cc_session" not in client.cookies


def test_a_disabled_user_is_refused_with_the_reason(client, keycloak):
    store.ensure_user("chameleon:sub-alice")
    store.disable_user("chameleon:sub-alice", "a runaway script")
    sent = _start(client)

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert _refusal(client, back) == "an admin disabled this account: a runaway script"
    assert "cc_session" not in client.cookies


def _stale(owner: str) -> None:
    with psycopg.connect(TEST_DB_URL) as conn:
        conn.execute(
            "UPDATE auth.users SET checked_at = now() - interval '1 hour' WHERE user_id = %s",
            [owner],
        )


def test_a_stale_login_takes_its_projects_from_keycloak(client, keycloak, logged_in, owner_a):
    _stale(owner_a)
    keycloak["userinfo"]["projects"] = [{"id": "CHI-000009", "nickname": "new"}]

    me = client.get("/me", headers=ALICE)

    assert me.json()["projects"] == [{"id": "CHI-000009", "nickname": "new"}]
    assert client.get("/me", headers=ALICE).status_code == 200
    assert sum(c["url"].endswith("/userinfo") for c in keycloak["calls"]) == 1


def test_a_fresh_login_is_not_checked_again(client, keycloak, logged_in):
    assert client.get("/me", headers=ALICE).status_code == 200
    assert keycloak["calls"] == []


def test_a_login_keycloak_refuses_ends_the_session(
    client, keycloak, logged_in, owner_a, monkeypatch
):
    _stale(owner_a)

    def refused(service, login, refresh_token):
        raise oidc.LoginExpired("refused")

    monkeypatch.setattr(oidc, "refresh", refused)

    reply = client.get("/me", headers=ALICE)

    assert reply.status_code == 401
    assert reply.json() == {"detail": "your login has expired; log in to the page again"}
    assert store.session_user(session_hash("alice-session")) is None


def test_an_unreachable_keycloak_keeps_the_session_and_is_not_asked_again_at_once(
    client, keycloak, logged_in, owner_a, monkeypatch
):
    _stale(owner_a)
    tried: list[str] = []

    def unreachable(service, login, refresh_token):
        tried.append(refresh_token)
        raise oidc.OidcError("unreachable")

    monkeypatch.setattr(oidc, "refresh", unreachable)

    assert client.get("/me", headers=ALICE).status_code == 200
    assert client.get("/me", headers=ALICE).status_code == 200
    assert len(tried) == 1


def test_a_recheck_that_finds_no_project_ends_the_session(client, keycloak, logged_in, owner_a):
    _stale(owner_a)
    keycloak["userinfo"]["projects"] = []

    reply = client.get("/me", headers=ALICE)

    assert reply.status_code == 401
    assert reply.json() == {
        "detail": "your Chameleon account belongs to no project; join one, then log in again"
    }
    assert store.session_user(session_hash("alice-session")) is None


def test_a_login_never_returns_to_another_site(client, keycloak):
    sent = _start(client, next_path="//evil.example.org/")

    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert back.headers["location"] == f"{PAGE}/"


def test_logging_out_ends_the_session_and_the_chameleon_login(client, keycloak):
    sent = _start(client)
    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})
    token = client.cookies["cc_session"]

    out = client.post("/auth/logout", headers={"Origin": PAGE})

    assert out.status_code == 303
    assert store.session_user(session_hash(token)) is None
    left_for = urllib.parse.urlsplit(out.headers["location"])
    assert left_for._replace(query="").geturl() == keycloak["published"]["end_session_endpoint"]
    assert dict(urllib.parse.parse_qsl(left_for.query)) == {
        "client_id": CLIENT_ID,
        "post_logout_redirect_uri": f"{PAGE}/",
    }


def test_another_site_cannot_log_a_user_out(client, keycloak):
    sent = _start(client)
    client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})
    token = client.cookies["cc_session"]

    refused = client.post("/auth/logout", headers={"Origin": "https://evil.example.org"})

    assert refused.status_code == 403
    assert store.session_user(session_hash(token)) is not None


def test_a_session_ends_once_its_testbed_has_no_login(client, logged_in, monkeypatch, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[adapters.chameleon]\n")
    monkeypatch.setenv("CC_CONFIG_FILE", str(path))
    config.cache_clear()

    refused = client.get("/me", headers=ALICE)

    assert (refused.status_code, refused.json()) == (
        401, {"detail": "logging in through chameleon is turned off"}
    )
    assert store.session_user(session_hash("alice-session")) is None


def test_a_testbed_without_a_login_cannot_be_logged_in_through(client, keycloak):
    refused = client.get("/auth/login", params={"testbed": "elsewhere"})

    assert (refused.status_code, refused.json()) == (
        404, {"detail": "no login for testbed 'elsewhere'"}
    )


def test_the_login_page_is_offered_each_testbed_with_a_login(client):
    assert client.get("/auth/methods").json() == {
        "local": False, "logins": [{"testbed": "chameleon", "label": "Chameleon"}], "refused": None
    }


def test_each_testbed_reads_its_own_login_settings(monkeypatch):
    monkeypatch.setenv("CC_OIDC_OTHER_ISSUER", "https://auth.example.org/realms/other")
    monkeypatch.setenv("CC_OIDC_OTHER_CLIENT_ID", "other-client")
    monkeypatch.setenv("CC_OIDC_OTHER_CLIENT_SECRET", "other-secret")

    assert oidc.client("other") == oidc.Client(
        issuer="https://auth.example.org/realms/other",
        client_id="other-client",
        secret="other-secret",
    )


def test_a_local_user_logs_out_without_visiting_keycloak(client, keycloak):
    store.add_local_user("chameleon:local:alice", "hash")
    store.start_session(
        "chameleon:local:alice",
        session_hash("local-session"),
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    client.cookies["cc_session"] = "local-session"

    out = client.post("/auth/logout", headers={"Origin": PAGE})

    assert out.headers["location"] == f"{PAGE}/"
    assert store.session_user(session_hash("local-session")) is None


def test_an_issuer_that_names_another_issuer_is_not_trusted(client, keycloak):
    keycloak["published"]["issuer"] = "https://auth.example.org/realms/other"

    refused = client.get("/auth/login", params={"testbed": "chameleon"})

    assert refused.status_code == 502
    assert refused.json() == {
        "detail": f"{ISSUER} publishes issuer 'https://auth.example.org/realms/other'"
    }


def test_calls_to_keycloak_name_cloud_conversations_as_their_user_agent(monkeypatch):
    import io

    sent: list = []

    def urlopen(request, timeout):
        sent.append(request)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(oidc.urllib.request, "urlopen", urlopen)

    oidc.call(f"{ISSUER}/.well-known/openid-configuration", 1.0)

    assert sent[0].get_header("User-agent") == "cloud-conversations"


def test_every_missing_login_setting_is_named(monkeypatch):
    monkeypatch.delenv("CC_OIDC_CHAMELEON_ISSUER")
    monkeypatch.delenv("CC_OIDC_CHAMELEON_CLIENT_SECRET")

    with pytest.raises(oidc.OidcError) as raised:
        oidc.client("chameleon")
    assert str(raised.value) == (
        "CC_OIDC_CHAMELEON_ISSUER is unset; CC_OIDC_CHAMELEON_CLIENT_SECRET is unset"
    )


def test_a_public_url_that_is_not_a_web_address_is_named(monkeypatch):
    monkeypatch.setenv("CC_PUBLIC_URL", "page.example.org")

    with pytest.raises(ConfigError) as raised:
        public_url()
    assert str(raised.value) == "CC_PUBLIC_URL is not an http or https address"


def test_a_worker_will_not_start_without_the_login_settings(monkeypatch):
    from cloud_conversations.worker.app import _require_login_settings

    monkeypatch.delenv("CC_OIDC_CHAMELEON_CLIENT_SECRET")

    with pytest.raises(SystemExit) as raised:
        _require_login_settings()
    assert str(raised.value) == (
        "ERROR: the worker needs the login settings: CC_OIDC_CHAMELEON_CLIENT_SECRET is unset"
    )


def test_keycloak_refusing_a_login_is_shown_on_the_login_page(client, keycloak):
    sent = _start(client)

    back = client.get("/auth/callback", params={"error": "access_denied", "state": sent["state"]})

    assert _refusal(client, back) == "the login was refused: access_denied"


def test_an_error_on_a_link_from_elsewhere_is_not_shown(client, keycloak):
    _start(client)

    back = client.get("/auth/callback", params={"error": "call 555-0100 to unlock your account"})

    assert _refusal(client, back) == "this login expired or did not start here; log in again"


def test_a_keycloak_that_cannot_be_reached_is_shown_on_the_login_page(
    client, keycloak, monkeypatch
):
    sent = _start(client)

    def unreachable(url, timeout_s, *, form=None, bearer=None):
        raise oidc.OidcError("auth.example.org did not answer")

    monkeypatch.setattr(oidc, "call", unreachable)
    back = client.get("/auth/callback", params={"code": "code-1", "state": sent["state"]})

    assert _refusal(client, back) == "auth.example.org did not answer"


def test_the_login_page_shows_a_refusal_once(client, keycloak):
    sent = _start(client)
    back = client.get("/auth/callback", params={"error": "access_denied", "state": sent["state"]})
    _refusal(client, back)

    assert client.get("/auth/methods").json()["refused"] is None


def test_the_login_page_shows_no_refusal_the_api_did_not_write(client):
    client.cookies.set("cc_refused", "call 555-0100 to unlock your account")

    assert client.get("/auth/methods").json()["refused"] is None


def test_with_approval_on_a_new_user_waits_and_gets_no_session(
    client, keycloak, approval_required
):
    back = _log_in(client)

    assert _refusal(client, back) == "an admin must approve this account before you can use it"
    assert "cc_session" not in client.cookies
    waiting = store.user_summaries("chameleon:sub-alice")[0]
    assert (waiting.username, waiting.approval) == ("alice", "waiting")


def test_a_waiting_user_who_logs_in_again_keeps_their_first_request_time(
    client, keycloak, approval_required
):
    _log_in(client)
    first = store.user_summaries("chameleon:sub-alice")[0].approval_requested_at

    _log_in(client)

    assert store.user_summaries("chameleon:sub-alice")[0].approval_requested_at == first


def test_with_approval_on_an_approved_user_logs_in(client, keycloak, approval_required):
    store.request_approval("chameleon:sub-alice", "alice", PROJECTS, email=None)
    store.approve_user("chameleon:sub-alice")

    _log_in(client)

    assert store.session_user(session_hash(client.cookies["cc_session"])) is not None


def test_with_approval_on_an_admin_logs_in_and_is_approved(
    client, keycloak, approval_required, alice_is_an_admin
):
    store.request_approval("chameleon:sub-alice", "alice", PROJECTS, email=None)

    _log_in(client)

    assert store.session_user(session_hash(client.cookies["cc_session"])) is not None
    assert store.approval("chameleon:sub-alice") == "approved"


def test_a_disabled_admin_is_refused(client, keycloak, alice_is_an_admin):
    store.ensure_user("chameleon:sub-alice")
    store.disable_user("chameleon:sub-alice", "a runaway script")

    back = _log_in(client)

    assert _refusal(client, back) == "an admin disabled this account: a runaway script"


def test_with_approval_on_someone_in_no_project_is_not_kept_waiting(
    client, keycloak, approval_required
):
    keycloak["userinfo"]["projects"] = []

    _log_in(client)

    assert store.approval("chameleon:sub-alice") is None


def test_with_approval_off_a_denied_user_is_still_refused(client, keycloak):
    store.request_approval("chameleon:sub-alice", "alice", PROJECTS, email=None)
    store.deny_user("chameleon:sub-alice")

    back = _log_in(client)

    assert _refusal(client, back) == "an admin denied access to this account"
    assert "cc_session" not in client.cookies


def test_with_approval_off_a_waiting_user_logs_in_and_is_approved(client, keycloak):
    store.request_approval("chameleon:sub-alice", "alice", PROJECTS, email=None)

    _log_in(client)

    assert store.session_user(session_hash(client.cookies["cc_session"])) is not None
    approved = store.user_summaries("chameleon:sub-alice")[0]
    assert approved.approval == "approved"
    assert approved.approval_decided_at is not None
