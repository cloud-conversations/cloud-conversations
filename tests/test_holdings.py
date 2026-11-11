"""Reading, joining, and filtering one listing."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import cloud_conversations.db.store as store
from cloud_conversations import holdings
from cloud_conversations.adapter.errors import TransientError
from cloud_conversations.adapter.fake import FakeAdapter, FakeLease, default_fake_sites
from cloud_conversations.adapter.protocol import Resource
from cloud_conversations.agent.inventory import Condition, HoldingsStep, Sort
from cloud_conversations.config import Config
from cloud_conversations.schema.service import Service
from tests.conftest import PUBLISHED_CONFIG

NOW = datetime(2026, 9, 21, 12, tzinfo=ZoneInfo("America/Chicago"))
BM = "FAKE@BM"
KVM = "FAKE@KVM"


def _config(**service: float) -> Config:
    return PUBLISHED_CONFIG.model_copy(update={"service": Service.model_validate(service)})


def _lease(lease_id: str, site: str = BM) -> FakeLease:
    return FakeLease(
        lease_id=lease_id, name=f"name-of-{lease_id}", site=site, state="ACTIVE",
        start=datetime(2026, 9, 20, 9, tzinfo=timezone.utc),
        end=datetime(2026, 9, 20, 13, tzinfo=timezone.utc),
        count=1, node_type="compute_skylake", flavor_id=None,
        reservation_id="res-1", reserved_flavor_id=None, reserved_fips=[], run_id=None,
    )


def _adapter(*leases: FakeLease) -> FakeAdapter:
    adapter = FakeAdapter(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases = {x.lease_id: x for x in leases}
    return adapter


def _run(owner: str, requirements, rubric, title: str) -> uuid.UUID:
    """Start a run from a description posted in a chat with this title."""
    did, version = store.create_description(owner, "test", requirements, rubric)
    thread = store.create_thread(owner, title)
    store.append_message(
        thread.thread_id, "assistant", "description", "a description",
        owner_id=owner, description_id=did, description_version=version,
    )
    return store.create_run(owner, did, version, None)


def _read(
    adapter, *, kinds=("leases",), sites=(), config=None, thread_id=None, now=NOW, **asked
):
    step = HoldingsStep(action="holdings", kinds=list(kinds), sites=list(sites), **asked)
    return holdings.read(
        adapter, config or _config(), step, owner_id="chameleon:alice", project_id=None,
        thread_id=thread_id, testbed="chameleon", now=now,
    )


@pytest.fixture(autouse=True)
def _users(owner_a, owner_b):
    store.ensure_user(owner_a)
    store.ensure_user(owner_b)


def test_a_lease_from_the_owners_run_comes_first_with_its_chat(
    owner_a, bm_requirements, bm_rubric
):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "GPU benchmark")
    store.record_resource(run_id, "lease-mine", "lease", BM, None)
    later = _lease("lease-other")
    later.start = datetime(2026, 10, 1, tzinfo=timezone.utc)

    held = _read(_adapter(_lease("lease-mine"), later), sites=[BM])

    assert [(r.resource_id, r.made_by, r.title) for r in held.rows] == [
        ("lease-mine", "experiment", "GPU benchmark"),
        ("lease-other", "you", None),
    ]
    assert held.summary == "2 leases at FAKE@BM."


def test_someone_elses_run_shows_with_no_chat(
    owner_a, owner_b, bm_requirements, bm_rubric
):
    run_id = _run(owner_b, bm_requirements, bm_rubric, "bob's secret chat")
    store.record_resource(run_id, "lease-bob", "lease", BM, None)

    class _Theirs(FakeAdapter):
        def list_resources(self, site: str, kind: str) -> list[Resource]:
            listed = super().list_resources(site, kind)
            return [r.model_copy(update={"mine": False}) for r in listed]

    adapter = _Theirs(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases = {"lease-bob": _lease("lease-bob")}

    [row] = _read(adapter, sites=[BM]).rows

    assert (row.made_by, row.title, row.run_id) == ("other", None, None)


def test_a_resource_the_owners_run_released_is_listed_as_released(
    owner_a, bm_requirements, bm_rubric
):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "MPI scaling")
    store.record_resource(run_id, "lease-gone", "lease", BM, None)
    store.release_resource_with_event(run_id, "lease", "lease-gone", BM)

    [row] = _read(_adapter(), sites=[BM]).rows

    assert (row.resource_id, row.status, row.title) == ("lease-gone", "released", "MPI scaling")
    assert row.released_at is not None


def test_an_id_an_earlier_run_released_shows_the_run_that_holds_it(
    owner_a, bm_requirements, bm_rubric, monkeypatch
):
    earlier = _run(owner_a, bm_requirements, bm_rubric, "MPI scaling")
    store.record_resource(earlier, "lease-1", "lease", BM, None)
    store.release_resource_with_event(earlier, "lease", "lease-1", BM)
    store.record_resource(_run(owner_a, bm_requirements, bm_rubric, "GPU benchmark"),
                          "lease-1", "lease", BM, None)
    owned = store.owner_resources
    monkeypatch.setattr(store, "owner_resources", lambda *a: sorted(
        owned(*a), key=lambda o: o.released_at is not None
    ))

    [row] = _read(_adapter(_lease("lease-1")), sites=[BM]).rows

    assert (row.made_by, row.title) == ("experiment", "GPU benchmark")


def test_a_release_older_than_the_window_is_not_listed(owner_a, bm_requirements, bm_rubric):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "MPI scaling")
    store.record_resource(run_id, "lease-old", "lease", BM, None)
    store.release_resource_with_event(run_id, "lease", "lease-old", BM)
    time.sleep(0.01)

    held = _read(
        _adapter(), sites=[BM], config=_config(holdings_released_days=0),
        now=datetime.now(timezone.utc),
    )

    assert held.rows == []


def test_a_deleted_chat_is_not_named(owner_a, bm_requirements, bm_rubric):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "GPU benchmark")
    store.record_resource(run_id, "lease-1", "lease", BM, None)
    store.release_resource_with_event(run_id, "lease", "lease-1", BM)
    [chat] = store.list_threads(owner_a)
    store.delete_thread(owner_a, chat.thread_id)

    [row] = _read(_adapter(), sites=[BM]).rows

    assert (row.run_id, row.title, row.thread_id) == (run_id, None, None)


def test_a_resource_deleted_outside_the_tool_is_gone(owner_a, bm_requirements, bm_rubric):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "VM smoke test")
    store.record_resource(run_id, "lease-deleted", "lease", BM, None)

    [row] = _read(_adapter(), sites=[BM]).rows

    assert (row.resource_id, row.status) == ("lease-deleted", "gone")


def test_a_short_site_name_reads_every_site_it_is_part_of():
    held = _read(_adapter(_lease("lease-1", BM), _lease("lease-2", KVM)), sites=["fake"])

    assert {r.site for r in held.rows} == {BM, KVM}
    assert held.summary == "2 leases at 2 sites."


def test_an_unknown_site_is_named_and_the_rest_is_still_read():
    held = _read(_adapter(_lease("lease-1")), sites=[BM, "CHI@MARS"])

    assert [r.resource_id for r in held.rows] == ["lease-1"]
    assert held.left_out == ["There's no site called CHI@MARS."]


def test_a_kind_with_nothing_is_left_out_of_the_summary():
    held = _read(_adapter(_lease("lease-1")), kinds=["leases", "instances"], sites=[BM])

    assert held.summary == "1 lease at FAKE@BM."


def test_the_testbeds_own_name_reads_every_site():
    held = _read(_adapter(_lease("lease-1", BM), _lease("lease-2", KVM)), sites=["Chameleon"])

    assert (held.summary, held.left_out) == ("2 leases at 2 sites.", [])


def test_naming_only_sites_that_do_not_exist_reads_nothing():
    held = _read(_adapter(_lease("lease-1")), sites=["CHI@MARS"])

    assert (held.rows, held.summary) == ([], "Nothing to list.")


def test_a_retired_site_shows_its_notice(monkeypatch):
    retired = {"OLD@SITE": "OLD@SITE retired."}
    monkeypatch.setattr(Config, "retired_at", lambda self, testbed: retired)

    held = _read(_adapter(), sites=["OLD@SITE"])

    assert held.left_out == ["OLD@SITE retired."]


def test_a_site_that_fails_is_named_once_with_its_error():
    class _Down(FakeAdapter):
        def list_resources(self, site: str, kind: str) -> list[Resource]:
            if site == BM:
                raise TransientError("blazar is down")
            return super().list_resources(site, kind)

    adapter = _Down(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases = {"lease-1": _lease("lease-1", KVM)}

    held = _read(adapter, kinds=["leases", "instances"])

    assert held.unread == {"FAKE@BM": "blazar is down"}


def test_a_site_that_was_not_read_is_left_out_of_the_summary():
    class _Down(FakeAdapter):
        def list_resources(self, site: str, kind: str) -> list[Resource]:
            if site == BM:
                raise TransientError("blazar is down")
            return super().list_resources(site, kind)

    adapter = _Down(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases = {"lease-1": _lease("lease-1", KVM)}

    assert _read(adapter).summary == "1 lease at FAKE@KVM."


def test_a_site_that_does_not_answer_in_time_is_not_read():
    class _Slow(FakeAdapter):
        def list_resources(self, site: str, kind: str) -> list[Resource]:
            time.sleep(0.5)
            return []

    held = _read(_Slow(sites=default_fake_sites(), config=PUBLISHED_CONFIG), sites=[BM],
                 config=_config(holdings_read_s=0.05))

    assert held.unread == {"FAKE@BM": "No answer in 0.05 s."}


def test_rows_past_the_limit_are_counted_not_kept():
    held = _read(
        _adapter(_lease("lease-1"), _lease("lease-2"), _lease("lease-3")),
        sites=[BM], config=_config(holdings_max_rows=2),
    )

    assert len(held.rows) == 2
    assert held.left_out == ["Showing 2 of 3. Ask for fewer to see the rest."]


def _ending(lease_id: str, day: int) -> FakeLease:
    lease = _lease(lease_id)
    lease.end = datetime(2026, 9, day, 9, tzinfo=timezone.utc)
    return lease


def test_a_condition_keeps_the_rows_it_matches():
    held = _read(
        _adapter(_ending("lease-soon", 22), _ending("lease-later", 30)), sites=[BM],
        where=[Condition(field="ends", op="before", value="2026-09-23T00:00")],
    )

    assert [r.resource_id for r in held.rows] == ["lease-soon"]
    assert held.summary == "1 lease at FAKE@BM where ends before Sep 23 00:00."


def test_a_status_reads_before_the_kind():
    def summary(status: str) -> str:
        where = [Condition(field="status", op="is", value=status)]
        return _read(_adapter(_lease("lease-1")), sites=[BM], where=where).summary

    assert summary("ACTIVE") == "1 active lease at FAKE@BM."
    assert summary("ERROR") == "No error leases at FAKE@BM."


def test_a_time_condition_lists_releases_older_than_the_window(
    owner_a, bm_requirements, bm_rubric
):
    run_id = _run(owner_a, bm_requirements, bm_rubric, "MPI scaling")
    store.record_resource(run_id, "lease-old", "lease", BM, None)
    store.release_resource_with_event(run_id, "lease", "lease-old", BM)
    time.sleep(0.01)

    held = _read(
        _adapter(), sites=[BM], config=_config(holdings_released_days=0),
        now=datetime.now(timezone.utc),
        where=[Condition(field="created", op="after", value="2000-01-01T00:00")],
    )

    assert [r.resource_id for r in held.rows] == ["lease-old"]


def test_made_by_this_chat_keeps_only_what_the_chat_built(
    owner_a, bm_requirements, bm_rubric
):
    here = _run(owner_a, bm_requirements, bm_rubric, "this chat")
    store.record_resource(here, "lease-here", "lease", BM, None)
    store.record_resource(_run(owner_a, bm_requirements, bm_rubric, "another chat"),
                          "lease-there", "lease", BM, None)
    [chat_here] = [t for t in store.list_threads(owner_a) if t.title == "this chat"]

    held = _read(
        _adapter(_lease("lease-here"), _lease("lease-there")), sites=[BM],
        thread_id=chat_here.thread_id,
        where=[Condition(field="made_by", op="in", value=["this_chat"])],
    )

    assert [r.resource_id for r in held.rows] == ["lease-here"]


def test_made_by_others_keeps_only_what_someone_else_made():
    class _OneTheirs(FakeAdapter):
        def list_resources(self, site: str, kind: str) -> list[Resource]:
            listed = super().list_resources(site, kind)
            return [r.model_copy(update={"mine": r.resource_id == "lease-mine"}) for r in listed]

    adapter = _OneTheirs(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases = {x.lease_id: x for x in (_lease("lease-mine"), _lease("lease-theirs"))}

    held = _read(adapter, sites=[BM], where=[Condition(field="made_by", op="in", value=["others"])])

    assert [r.resource_id for r in held.rows] == ["lease-theirs"]


def test_rows_come_in_the_order_asked_and_as_many_as_asked():
    held = _read(
        _adapter(_ending("lease-a", 25), _ending("lease-b", 22), _ending("lease-c", 28)),
        sites=[BM], sort=Sort(field="ends", order="asc"), limit=2,
    )

    assert [r.resource_id for r in held.rows] == ["lease-b", "lease-a"]
