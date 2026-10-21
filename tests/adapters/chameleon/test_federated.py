"""Reaching Chameleon as the logged-in user."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from keystoneauth1 import session
from keystoneauth1.identity import v3

import cloud_conversations.db.store as store
from cloud_conversations import crypt, oidc
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.chameleon import federated
from cloud_conversations.adapter.credentials import CredentialsError, UserScope
from cloud_conversations.api.deps import session_hash
from tests.conftest import PUBLISHED_CONFIG, TEST_DB_URL

pytestmark = pytest.mark.usefixtures("logged_in")


def _session_left(username: str) -> timedelta:
    with psycopg.connect(TEST_DB_URL) as conn:
        row = conn.execute(
            "SELECT expires_at FROM auth.sessions WHERE token_hash = %s",
            [session_hash(f"{username}-session")],
        ).fetchone()
    assert row is not None
    return row[0] - datetime.now(timezone.utc)


def _refreshed_until(monkeypatch, seconds: int) -> None:
    monkeypatch.setattr(
        oidc,
        "refresh",
        lambda service, login, token: {"access_token": "a", "refresh_expires_in": seconds},
    )


def _stored_refresh_token(owner: str) -> str:
    with psycopg.connect(TEST_DB_URL) as conn:
        row = conn.execute(
            "SELECT refresh_token FROM auth.refresh_tokens WHERE user_id = %s", [owner]
        ).fetchone()
    assert row is not None
    return crypt.decrypt(row[0])


def test_an_access_token_comes_from_the_stored_refresh_token_which_it_rotates(
    monkeypatch, owner_a
):
    sent: list[str] = []

    def refresh(service, login, refresh_token):
        sent.append(refresh_token)
        return {"access_token": "access-2", "refresh_token": "refresh-2"}

    monkeypatch.setattr(oidc, "refresh", refresh)

    assert credentials.access_token(owner_a, PUBLISHED_CONFIG.service) == "access-2"
    assert sent == ["a refresh token"]
    assert _stored_refresh_token(owner_a) == "refresh-2"


def test_the_refresh_token_stays_locked_while_it_is_being_refreshed(monkeypatch, owner_a):
    seen: list[str] = []

    def refresh(service, login, refresh_token):
        with psycopg.connect(TEST_DB_URL) as other, pytest.raises(psycopg.errors.LockNotAvailable):
            other.execute(
                "SELECT 1 FROM auth.refresh_tokens WHERE user_id = %s FOR UPDATE NOWAIT",
                [owner_a],
            )
        seen.append("locked")
        return {"access_token": "access-2", "refresh_token": "refresh-2"}

    monkeypatch.setattr(oidc, "refresh", refresh)

    credentials.access_token(owner_a, PUBLISHED_CONFIG.service)

    assert seen == ["locked"]


def test_a_login_keycloak_no_longer_accepts_says_to_log_in_again(monkeypatch, owner_a):
    def refresh(service, login, refresh_token):
        raise oidc.LoginExpired("invalid_grant")

    monkeypatch.setattr(oidc, "refresh", refresh)

    with pytest.raises(CredentialsError) as raised:
        credentials.access_token(owner_a, PUBLISHED_CONFIG.service)
    assert str(raised.value) == "your login has expired; log in to the page again"
    assert _stored_refresh_token(owner_a) == "a refresh token"
    assert store.session_user(session_hash("alice-session")) is None
    assert store.session_user(session_hash("bob-session")) is not None


def test_a_refresh_moves_the_users_sessions_to_end_with_the_new_token(monkeypatch, owner_a):
    _refreshed_until(monkeypatch, 600)

    credentials.access_token(owner_a, PUBLISHED_CONFIG.service)

    assert timedelta(seconds=590) < _session_left("alice") <= timedelta(seconds=600)
    assert _session_left("bob") > timedelta(hours=23)


def test_a_refresh_never_moves_a_session_past_session_days(monkeypatch, owner_a):
    _refreshed_until(monkeypatch, 10**9)

    credentials.access_token(owner_a, PUBLISHED_CONFIG.service)

    longest = timedelta(days=PUBLISHED_CONFIG.service.session_days)
    assert longest - timedelta(minutes=1) < _session_left("alice") <= longest


def test_someone_who_never_logged_in_has_no_scope():
    with pytest.raises(CredentialsError) as raised:
        credentials.user_scope("chameleon:never-logged-in", None)
    assert str(raised.value) == "log in to the page first"


def test_a_chat_with_no_project_builds_in_the_users_first(owner_a):
    assert credentials.user_scope(owner_a, None).project == "CHI-000001"
    assert credentials.user_scope(owner_a, "CHI-000002") == UserScope(
        owner_a, "chameleon", "CHI-000002"
    )


def test_the_user_logs_in_at_every_site_federated_into_their_project(owner_a):
    logins = federated.user_logins(UserScope(owner_a, "chameleon", "CHI-000002"), PUBLISHED_CONFIG)

    plugin = logins["KVM@TACC"].auth("https://kvm.tacc.invalid:5000/v3")

    assert isinstance(plugin, v3.OidcAccessToken)
    assert sorted(logins) == sorted(PUBLISHED_CONFIG.sites())
    assert plugin.auth_url == "https://kvm.tacc.invalid:5000/v3"
    assert (plugin.identity_provider, plugin.protocol) == ("chameleon", "openid")
    assert (plugin.project_name, plugin.project_domain_name) == ("CHI-000002", "chameleon")


def test_each_keystone_login_takes_a_fresh_access_token(monkeypatch, owner_a):
    tokens = iter(["access-1", "access-2"])
    monkeypatch.setattr(federated, "access_token", lambda owner, service: next(tokens))
    monkeypatch.setattr(
        v3.OidcAccessToken, "get_unscoped_auth_ref", lambda self, session: self.access_token
    )
    plugin = federated.user_logins(UserScope(owner_a, "chameleon", "CHI-000001"), PUBLISHED_CONFIG)[
        "CHI@UC"
    ].auth("https://chi.uc.invalid:5000/v3")

    assert isinstance(plugin, v3.OidcAccessToken)
    assert plugin.get_unscoped_auth_ref(session.Session()) == "access-1"
    assert plugin.get_unscoped_auth_ref(session.Session()) == "access-2"


def test_each_owner_is_refreshed_with_their_own_token(monkeypatch, owner_a, owner_b):
    from cloud_conversations.adapter.credentials import access_token

    with psycopg.connect(TEST_DB_URL) as conn:
        conn.execute(
            "UPDATE auth.refresh_tokens SET refresh_token = %s WHERE user_id = %s",
            [crypt.encrypt("bob's refresh token"), owner_b],
        )
    sent: list[str] = []

    def refresh(service, login, token):
        sent.append(token)
        return {"access_token": "a"}

    monkeypatch.setattr(oidc, "refresh", refresh)

    access_token(owner_a, PUBLISHED_CONFIG.service)
    access_token(owner_b, PUBLISHED_CONFIG.service)

    assert sent == ["a refresh token", "bob's refresh token"]
