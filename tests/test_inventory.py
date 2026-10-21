"""A user's resources, listed from the sites themselves."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from cloud_conversations.adapter.errors import TransientError
from cloud_conversations.adapter.fake import (
    FakeAdapter,
    FakeInstance,
    FakeLease,
    default_fake_sites,
)
from cloud_conversations.adapter.protocol import Capabilities
from cloud_conversations.agent.contract import DeclineStep
from cloud_conversations.agent.inventory import Holdings, read_holdings
from tests.conftest import PUBLISHED_CONFIG
from tests.fakes import FakeModel

BM = "FAKE@BM"
KVM = "FAKE@KVM"


def _step(*holdings: str, site: str | None = None) -> str:
    step: dict[str, object] = {"action": "holdings", "holdings": list(holdings)}
    if site is not None:
        step["site"] = site
    return json.dumps(step)


def _adapter() -> FakeAdapter:
    return FakeAdapter(sites=default_fake_sites(), config=PUBLISHED_CONFIG)


def _caps(*sites: str) -> Capabilities:
    return Capabilities(
        sites=[d for d in _adapter().capabilities().sites if d.site in sites]
    )


def _lease(lease_id: str, site: str, *, count: int = 1) -> FakeLease:
    return FakeLease(
        lease_id=lease_id, name="mine", site=site, state="ACTIVE",
        start=datetime(2026, 9, 20, 9, tzinfo=timezone.utc),
        end=datetime(2026, 9, 20, 13, tzinfo=timezone.utc),
        count=count, node_type="compute_skylake", flavor_id=None,
        reservation_id="res-1", reserved_flavor_id=None, reserved_fips=[],
        run_id=None,
    )


def _instance(instance_id: str, name: str, site: str) -> FakeInstance:
    return FakeInstance(
        instance_id=instance_id, name=name, site=site, image_id="img-ubuntu2404",
        keypair_name="fake-keypair", flavor_id="m1.large", state="ACTIVE",
        floating_ip=None, node_type=None, run_id=None,
    )


def test_a_lease_is_listed_with_its_window_and_counted():
    adapter = _adapter()
    adapter.leases["lease-1"] = _lease("lease-1", BM, count=2)

    read, rounds = read_holdings(
        FakeModel([_step("leases", site=BM)]), adapter, _caps(BM), "my leases", testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert read.summary == "1 lease at FAKE@BM."
    assert "mine  lease-1  ACTIVE  2x" in read.tables[0]
    assert "2026-09-20T09:00:00+00:00 to 2026-09-20T13:00:00+00:00" in read.tables[0]
    assert [r.action for r in rounds] == ["holdings"]


def test_an_instance_is_listed_by_name():
    adapter = _adapter()
    adapter.instances["vm-1"] = _instance("vm-1", "my-vm", KVM)

    read, _ = read_holdings(
        FakeModel([_step("instances", site=KVM)]), adapter, _caps(KVM), "my vms",
            testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert "my-vm  vm-1  ACTIVE" in read.tables[0]
    assert read.summary == "1 instance at FAKE@KVM."


def test_only_the_images_the_project_owns_are_listed():
    read, _ = read_holdings(
        FakeModel([_step("images", site=BM)]), _adapter(), _caps(BM), "my images",
            testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert "my-experiment-snapshot" in read.tables[0]
    assert "CC-Ubuntu24.04" not in read.tables[0]


def test_two_holdings_are_two_tables_and_one_counted_line():
    adapter = _adapter()
    adapter.leases["lease-1"] = _lease("lease-1", BM)

    read, _ = read_holdings(
        FakeModel([_step("leases", "instances", site=BM)]),
        adapter, _caps(BM), "my leases and instances", testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert read.summary == "1 lease, no instances at FAKE@BM."
    assert [t.splitlines()[0] for t in read.tables] == ["# leases", "# instances"]


def test_leaving_the_site_out_covers_every_site():
    adapter = _adapter()
    adapter.leases["lease-1"] = _lease("lease-1", KVM)

    read, _ = read_holdings(
        FakeModel([_step("leases")]), adapter, _caps(BM, KVM), "all my leases", testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert "## FAKE@BM\n\nnothing" in read.tables[0]
    assert "## FAKE@KVM\n\nmine  lease-1" in read.tables[0]
    assert read.summary == "1 lease at 2 sites."


def test_a_site_that_cannot_be_read_says_so_and_is_not_counted():
    class _Broken(FakeAdapter):
        def list_leases(self, site: str):
            if site == BM:
                raise TransientError("blazar is down")
            return super().list_leases(site)

    adapter = _Broken(sites=default_fake_sites(), config=PUBLISHED_CONFIG)
    adapter.leases["lease-1"] = _lease("lease-1", KVM)

    read, _ = read_holdings(
        FakeModel([_step("leases")]), adapter, _caps(BM, KVM), "my leases", testbed="chameleon"
    )

    assert isinstance(read, Holdings)
    assert "could not be read: blazar is down" in read.tables[0]
    assert read.summary == "1 lease at 2 sites.", "the dead site counts nothing"


def test_a_site_there_are_no_credentials_for_is_corrected_once():
    model = FakeModel([_step("leases", site="CHI@TACC"), _step("leases", site=BM)])

    read, rounds = read_holdings(model, _adapter(), _caps(BM), "my leases", testbed="chameleon")

    assert isinstance(read, Holdings)
    assert [r.action for r in rounds] == ["holdings", "holdings"]
    assert "no credentials for 'CHI@TACC'" in rounds[0].reasons[0]


def test_two_replies_that_will_not_do_end_with_nothing():
    model = FakeModel([_step("leases", site="CHI@UC"), _step("leases", site="CHI@UC")])

    read, rounds = read_holdings(model, _adapter(), _caps(BM), "my leases", testbed="chameleon")

    assert read is None
    assert len(rounds) == 2, "one correction, then it gives up"


def _decline(reason: str, *suggestions: str) -> str:
    return json.dumps({
        "action": "decline", "reason": reason, "suggestions": list(suggestions),
    })


def test_something_it_cannot_list_is_refused_rather_than_guessed_at():
    model = FakeModel([_decline("I cannot read allocations here", "your leases")])

    read, rounds = read_holdings(
        model, _adapter(), _caps(BM), "how much of my allocation is left?", testbed="chameleon"
    )

    assert isinstance(read, DeclineStep)
    assert read.reason == "I cannot read allocations here"
    assert read.suggestions == ["your leases"]
    assert [r.action for r in rounds] == ["decline"]
