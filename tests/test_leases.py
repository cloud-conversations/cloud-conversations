"""The lease watch: warnings before a lease ends, and leases that ended."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.adapter.credentials import CredentialsError
from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
from cloud_conversations.worker import leases
from tests.conftest import BM_SITE, FAST


@pytest.fixture()
def adapter(monkeypatch) -> FakeAdapter:
    fake = FakeAdapter(sites=default_fake_sites(), config=FAST)
    monkeypatch.setattr(leases.credentials, "build_adapter", lambda *args, **kwargs: fake)
    return fake


@pytest.fixture()
def chat(owner_a, bm_requirements, bm_rubric) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat, and the run of its description."""
    did, version = store.create_description(owner_a, "test", bm_requirements, bm_rubric)
    thread = store.create_thread(owner_a, "a node")
    store.append_message(
        thread.thread_id, "assistant", "description", "a description",
        owner_id=owner_a, description_id=did, description_version=version,
    )
    return thread.thread_id, store.create_run(owner_a, did, version, None)


def _lease(adapter: FakeAdapter, run_id: uuid.UUID, ends_in: timedelta) -> str:
    lease = adapter.create_lease(
        BM_SITE, duration_hours=1.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key=f"lease-of-{run_id}",
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE experiments.run_resources SET expires_at = now() + %s WHERE provider_id = %s",
            [ends_in, lease.lease_id],
        )
    return lease.lease_id


def _notices(owner: str, thread_id: uuid.UUID) -> list[str]:
    return [m.text for m in store.read_messages(owner, thread_id) if m.kind == "notice"]


def test_a_chat_is_warned_once_before_its_lease_ends(owner_a, chat, adapter):
    thread_id, run_id = chat
    lease_id = _lease(adapter, run_id, timedelta(minutes=30))
    (lease,) = store.live_resources(run_id)
    assert lease.provider_id == lease_id and lease.expires_at is not None

    leases.watch(FAST)
    leases.watch(FAST)

    assert _notices(owner_a, thread_id) == [
        f"The lease at {BM_SITE} ends at {lease.expires_at:%Y-%m-%d %H:%M} UTC. "
        "Save your work before then."
    ]


def test_a_lease_that_ends_later_is_not_warned_about_yet(owner_a, chat, adapter):
    thread_id, run_id = chat
    _lease(adapter, run_id, timedelta(seconds=FAST.service.lease_warning_s + 600))

    leases.watch(FAST)

    assert _notices(owner_a, thread_id) == []


def test_a_lease_that_ended_is_released_with_its_instance(owner_a, chat, adapter):
    thread_id, run_id = chat
    lease_id = _lease(adapter, run_id, timedelta(minutes=-5))
    adapter.delete_lease(BM_SITE, lease_id)
    store.record_resource(run_id, "instance-1", "instance", BM_SITE, None)
    store.record_resource(run_id, "192.0.2.7", "floating_ip", BM_SITE, None)

    leases.watch(FAST)

    assert [r.provider_id for r in store.live_resources(run_id)] == ["192.0.2.7"]
    assert _notices(owner_a, thread_id) == [f"The lease at {BM_SITE} ended."]


def test_a_lease_extended_at_the_testbed_keeps_its_new_end(owner_a, chat, adapter):
    thread_id, run_id = chat
    lease_id = _lease(adapter, run_id, timedelta(minutes=-5))
    later = datetime.now(timezone.utc) + timedelta(days=1)
    adapter.leases[lease_id].end = later

    leases.watch(FAST)

    (lease,) = store.live_resources(run_id)
    assert lease.expires_at == later
    assert _notices(owner_a, thread_id) == []


def test_a_lease_whose_owner_cannot_be_logged_in_as_is_left_as_it_is(
    monkeypatch, caplog, owner_a, chat, adapter
):
    thread_id, run_id = chat
    lease_id = _lease(adapter, run_id, timedelta(minutes=-5))
    adapter.delete_lease(BM_SITE, lease_id)

    def logged_out(*args, **kwargs):
        raise CredentialsError("your login has expired; log in to the page again")

    monkeypatch.setattr(leases.credentials, "build_adapter", logged_out)
    monkeypatch.setattr(leases.log, "disabled", False)
    caplog.set_level(logging.WARNING, logger=leases.log.name)

    leases.watch(FAST)

    assert [r.provider_id for r in store.live_resources(run_id)] == [lease_id]
    assert [(r.levelname, r.exc_info) for r in caplog.records] == [("WARNING", None)]


def test_an_ended_lease_is_checked_at_most_once_each_lease_watch_s(
    monkeypatch, owner_a, chat, adapter
):
    thread_id, run_id = chat
    lease_id = _lease(adapter, run_id, timedelta(minutes=-5))
    adapter.leases[lease_id].end = datetime.now(timezone.utc) + timedelta(days=1)
    checks: list[str] = []
    listed = adapter.list_leases

    def counted(site):
        checks.append(site)
        return listed(site)

    monkeypatch.setattr(adapter, "list_leases", counted)
    leases.watch(FAST)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE experiments.run_resources SET expires_at = now() - interval '1 minute'"
        )

    leases.watch(FAST)

    assert checks == [BM_SITE]
