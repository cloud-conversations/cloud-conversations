"""Every call that changes the testbed is recorded, under the request that allowed it."""

from __future__ import annotations

import inspect
import uuid

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations import audit
from cloud_conversations.adapter.errors import CapacityError
from cloud_conversations.adapter.fake import default_fake_sites
from cloud_conversations.adapter.protocol import TestbedAdapter
from tests.conftest import BM_SITE, TEST_DB_URL
from tests.fakes import FailureConfig, FaultyAdapter

# Every adapter call that only reads.
_READS = {
    "capabilities", "sites_with_login", "published_node_types", "published_flavors",
    "search_hardware", "settings", "list_instances", "get_instance", "get_console_log",
    "find_image", "list_images", "list_own_images", "find_flavor", "find_keypair",
    "list_keypairs", "wait_instance_active", "wait_instance_gone", "find_availability",
    "published_availability", "list_leases", "wait_lease_active", "reserved_host",
}


def _calls(owner: str) -> list[tuple]:
    with psycopg.connect(TEST_DB_URL) as conn:
        return conn.execute(
            "SELECT operation, kind, provider_id, outcome, error, request_id "
            "FROM audit.testbed_calls WHERE owner_id = %s ORDER BY seq",
            [owner],
        ).fetchall()


def test_every_adapter_call_is_either_recorded_or_a_read():
    calls = {
        name for name, member in inspect.getmembers(TestbedAdapter, inspect.isfunction)
        if not name.startswith("_")
    }
    assert calls - _READS == set(audit.CHANGES)


def test_a_change_is_refused_when_no_one_is_named_as_acting(owner_a):
    adapter = FaultyAdapter(sites=default_fake_sites())

    with pytest.raises(RuntimeError, match="no one is named as acting"):
        audit.audited(adapter).delete_keypair(BM_SITE, "cc-keypair")

    assert adapter.find_keypair(BM_SITE, "cc-keypair") is not None
    assert _calls(owner_a) == []


def test_a_change_is_recorded_under_the_request_and_run_it_was_made_for(owner_a):
    adapter = audit.audited(FaultyAdapter(sites=default_fake_sites()))
    request_id, run_id = uuid.uuid4(), uuid.uuid4()

    with audit.acting(owner_a, request_id), audit.on_run(run_id):
        adapter.delete_keypair(BM_SITE, "cc-keypair")

    assert _calls(owner_a) == [
        ("delete_keypair", "keypair", "cc-keypair", "started", None, request_id),
        ("delete_keypair", "keypair", "cc-keypair", "ok", None, request_id),
    ]
    with psycopg.connect(TEST_DB_URL) as conn:
        (call,) = conn.execute(
            "SELECT DISTINCT call_id, run_id, site FROM audit.testbed_calls"
        ).fetchall()
    assert call[1:] == (run_id, BM_SITE)


def test_a_call_is_recorded_before_it_reaches_the_testbed(owner_a):
    inner = FaultyAdapter(sites=default_fake_sites())
    seen: list[list[tuple]] = []

    def delete_keypair(site: str, name: str) -> bool:
        seen.append(_calls(owner_a))
        return True

    inner.delete_keypair = delete_keypair  # type: ignore[method-assign]
    request_id = uuid.uuid4()

    with audit.acting(owner_a, request_id):
        audit.audited(inner).delete_keypair(BM_SITE, "cc-keypair")

    assert seen == [
        [("delete_keypair", "keypair", "cc-keypair", "started", None, request_id)]
    ]


def test_a_call_that_changed_the_testbed_returns_if_its_outcome_cannot_be_written(
    owner_a, monkeypatch
):
    adapter = FaultyAdapter(sites=default_fake_sites())
    record = store.record_testbed_call

    def failing_after_the_start(*args: object) -> None:
        if args[6] != "started":
            raise psycopg.OperationalError("the database went away")
        record(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "record_testbed_call", failing_after_the_start)

    with audit.acting(owner_a, uuid.uuid4()):
        assert audit.audited(adapter).delete_keypair(BM_SITE, "cc-keypair") is True

    assert adapter.find_keypair(BM_SITE, "cc-keypair") is None
    assert [c[3] for c in _calls(owner_a)] == ["started"]


def test_a_failed_change_is_recorded_with_its_error_and_raised(owner_a):
    adapter = audit.audited(
        FaultyAdapter(sites=default_fake_sites(), failures=FailureConfig(capacity_exhausted=True))
    )
    request_id = uuid.uuid4()

    with audit.acting(owner_a, request_id), pytest.raises(CapacityError) as raised:
        adapter.create_lease(
            BM_SITE, duration_hours=1, node_type="compute_skylake", run_id=uuid.uuid4(),
            idempotency_key="k",
        )

    assert _calls(owner_a) == [
        ("create_lease", "lease", None, "started", None, request_id),
        ("create_lease", "lease", None, "failed", f"CapacityError: {raised.value}",
         request_id),
    ]


def test_the_timeline_reads_a_request_in_order(owner_a, bm_requirements, bm_rubric):
    request_id = uuid.uuid4()
    store.record_decision(
        request_id, owner_a, "proposed", "agent", "release",
        shown="The experiment agent wants to release lease L at FAKE@BM.",
    )
    store.record_decision(request_id, owner_a, "confirmed", "user", "release", via="web")
    adapter = audit.audited(FaultyAdapter(sites=default_fake_sites()))
    with audit.acting(owner_a, request_id):
        adapter.delete_keypair(BM_SITE, "cc-keypair")

    with psycopg.connect(TEST_DB_URL) as conn:
        lines = conn.execute(
            "SELECT entry, actor, what, said FROM audit.timeline WHERE request_id = %s",
            [request_id],
        ).fetchall()
    assert lines == [
        ("decision", "agent", "proposed release",
         "The experiment agent wants to release lease L at FAKE@BM."),
        ("decision", "user", "confirmed release", None),
        ("testbed call", None, "delete_keypair keypair cc-keypair at FAKE@BM: started", None),
        ("testbed call", None, "delete_keypair keypair cc-keypair at FAKE@BM: ok", None),
    ]
