"""The build loop, driven by canned model replies against the fake testbed."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations import releases
from cloud_conversations.adapter.errors import TransientError
from cloud_conversations.adapter.fake import FakeAdapter, FakeSite, HostFacts
from cloud_conversations.adapter.protocol import Capabilities, Image, NodeType, SiteDescriptor
from cloud_conversations.agent import observe
from cloud_conversations.agent.build import build
from cloud_conversations.agent.contract import KnowledgeStep, RetryStep
from cloud_conversations.agent.recover import recover
from cloud_conversations.agent.validate import validate_build_step
from cloud_conversations.audit import audited
from cloud_conversations.config import Adapter, Config, Settings
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import Check, ImageChoice, Requirement, Requirements
from cloud_conversations.schema.runs import RunState
from cloud_conversations.worker import runs
from tests.conftest import BARE_METAL, FAKE_SETTINGS, KINDS, TEST_DB_URL
from tests.fakes import FailureConfig, FakeModel, FaultyAdapter, Shelf, knowledge_search
from tests.loopback import page, serving

UC = "CHI@UC"
KEYPAIR = "cc-keypair"

_SKYLAKE = NodeType(
    name="compute_skylake", cpu_arch="x86_64", cpu_count=48, mem_gb=192, disk_gb=480
)
_UBUNTU = Image(image_id="img-2404", name="CC-Ubuntu24.04", os_release="Ubuntu 24.04")

_RUBRIC = [
    Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
          from_requirement="os_release"),
    Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
]

# A rubric the built host fails.
_WRONG_RUBRIC = [
    Check(id="os", kind="os_release", expected="Ubuntu 22.04", comparator="prefix",
          from_requirement="os_release"),
]


def _settings(**over) -> Settings:
    base = {
        **FAKE_SETTINGS,
        "poll_interval_s": 0.01, "delete_poll_interval_s": 0.01, "read_retry_s": 0.01,
        "ssh_poll_interval_s": 0.01, "boot_retry_s": 0.01,
        "boot_attempts": 1, "reservation_attempts": 1, "max_rounds": 3,
    }
    return Settings(**{**base, **over})


def _config(settings: Settings, root: str | None = None) -> Config:
    return Config(
        defaults=settings,
        adapters={"chameleon": Adapter(
            site_index_url=f"{root}/sites" if root else None,
            kinds=KINDS,
            sites={UC: {"kinds": ["baremetal"]}},
        )},
    )


def _adapter(
    config: Config, facts: HostFacts | None = None, **failures
) -> FaultyAdapter:
    site = FakeSite(
        descriptor=SiteDescriptor(
            site=UC,
            auth_url="https://chi.uc.invalid:5000/v3",
            kinds=BARE_METAL,
            node_types=[_SKYLAKE],
        ),
        images=[_UBUNTU],
        keypairs={KEYPAIR: "ssh-rsa AAAA...fake"},
        stock={"compute_skylake": 4},
    )
    return FaultyAdapter(
        sites={UC: site},
        failures=FailureConfig(**failures),
        facts_overrides={"cc-": facts} if facts else None,
        config=config,
    )


def _requirements(image_name: str = "CC-Ubuntu24.04") -> Requirements:
    return Requirements(
        site=UC,
        duration_hours=2,
        items=[
            Requirement(key="node_type", value="compute_skylake", source="stated"),
            Requirement(key="image_name", value=image_name, source="stated"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="stated"),
        ],
    )


def _start(
    owner: str,
    adapter: FakeAdapter,
    settings: Settings,
    image_name: str = "CC-Ubuntu24.04",
    site: str = UC,
    rubric: list[Check] = _RUBRIC,
    project_id: str | None = None,
) -> uuid.UUID:
    """Write a description and take the first, deterministic attempt at it."""
    desc_id, version = store.create_description(
        owner_id=owner,
        conversation_id="test",
        requirements=_requirements(image_name),
        rubric=rubric,
    )
    return sequential.provision(
        owner_id=owner, description_id=desc_id, version=version, project_id=project_id,
        keypair=KEYPAIR, adapter=adapter, site=site, settings=settings,
    )


def _kinds(owner: str, run_id: uuid.UUID) -> list[str]:
    return [e.kind for e in store.read_events(owner, run_id)]


def _payloads(owner: str, run_id: uuid.UUID, kind: str) -> list[dict]:
    return [e.payload for e in store.read_events(owner, run_id) if e.kind == kind]


def _inspect(read: str) -> str:
    return json.dumps({"action": "inspect", "read": read})


def _retry() -> str:
    return json.dumps({"action": "retry", "reason": "the host was unlucky"})


def _abandon() -> str:
    return json.dumps({"action": "abandon", "reason": "nothing else will help"})


def _revise(change: str = "use an image this site actually has") -> str:
    return json.dumps({"action": "revise", "change": change, "reason": "no such image"})


def _search(hours: int = 2) -> str:
    return json.dumps({"action": "search", "query": {
        "reserve_by": "node_type",
        "site": UC, "node_type": "compute_skylake", "duration_hours": hours,
        "intent": "a skylake node",
    }})


def _answer(hours: int = 2) -> str:
    return json.dumps({"action": "answer", "description": {
        "site": UC,
        "duration_hours": hours,
        "items": [
            {"key": "node_type", "value": "compute_skylake", "source": "stated"},
            {"key": "image_name", "value": "CC-Ubuntu24.04", "source": "stated"},
            {"key": "os_release", "value": "Ubuntu 24.04", "source": "stated"},
        ],
        "rubric": [
            {"id": "ssh", "kind": "ssh_reachable", "expected": True, "comparator": "eq"},
            {"id": "os", "kind": "os_release", "expected": "Ubuntu 24.04",
             "comparator": "prefix", "from_requirement": "os_release"},
        ],
    }})


def _decline() -> str:
    return json.dumps({
        "action": "decline",
        "reason": "no image on this testbed reports that release",
        "suggestions": ["ask for Ubuntu 24.04"],
    })


# A reference API page with two free skylake nodes.
_TWO_FREE = page(*[{
    "node_type": "compute_skylake",
    "site_id": "uc",
    "availability": "available",
    "architecture": {"platform_type": "x86_64", "smt_size": 48},
    "main_memory": {"ram_size": 206158430208},
    "storage_devices": [{"size": 480000000000}],
    "gpu": {"gpu": False},
}] * 2)


def _drive(
    model: FakeModel,
    owner: str,
    run_id: uuid.UUID,
    adapter: FakeAdapter,
    config: Config,
    settings: Settings,
) -> tuple[uuid.UUID, list]:
    rounds: list = []
    final = recover(
        model, owner, run_id, adapter, config, settings,
        report=lambda outcome: rounds.extend(outcome.rounds),
    )
    return final, rounds


def test_a_passing_run_is_returned_without_calling_the_model(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config)
    run_id = _start(owner_a, adapter, settings)
    assert store.get_run(owner_a, run_id).state == RunState.passed

    model = FakeModel([])
    final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert final == run_id
    assert rounds == []
    assert model.calls == [], "a build that did not fail must not reach a model"


def test_an_unusable_host_is_retried_and_succeeds(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    assert store.get_run(owner_a, run_id).state == RunState.failed

    model = FakeModel([_retry()])
    final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["retry"]
    assert rounds[0].reasons == []
    assert final == run_id, "a retry stays on the same run"
    assert store.get_run(owner_a, final).state == RunState.passed


def test_the_agent_asks_for_a_revision_and_carries_none_out_itself(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    before = store.live_resources(run_id)

    outcome = build(FakeModel([_revise()]), owner_a, run_id, adapter, settings)

    assert outcome.revision is not None
    assert outcome.revision.change == "use an image this site actually has"
    assert outcome.run_id == run_id, "no new run was provisioned"
    assert store.live_resources(run_id) == before, "nothing was released"
    assert store.get_run(owner_a, run_id).description_version == 1


def test_a_revision_of_a_run_that_holds_hardware_waits_for_the_user_to_release_it(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner_a, adapter, settings, rubric=_WRONG_RUBRIC)
        held = store.live_resources(run_id)
        assert held, "the failed run still holds its hardware"

        model = FakeModel([_revise(), _search(), _answer()])
        final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["revise"]
    assert final == run_id, "nothing new was provisioned"
    assert store.live_resources(run_id) == held, "nothing was released"
    proposal = store.pending_release(owner_a, run_id)
    assert proposal is not None
    assert (proposal.actor, proposal.reason) == ("agent", "no such image")
    assert {r["provider_id"] for r in proposal.resources} == {r.provider_id for r in held}
    assert proposal.version == 2


def test_a_revision_to_another_image_name_waits_for_the_users_approval(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner_a, adapter, settings, image_name="CC-Nonesuch99.04")
        model = FakeModel([_revise(), _search(), _answer()])
        final, _ = _drive(model, owner_a, run_id, adapter, config, settings)

    desc_id = store.get_run(owner_a, run_id).description_id
    assert final == run_id, "nothing new was provisioned"
    assert store.latest_description(owner_a, desc_id).version == 2
    assert [r.description_version for r in store.runs_for_description(owner_a, desc_id)] == [1]


def test_a_revision_to_another_image_waits_for_the_users_approval(owner_a):
    settings = _settings()
    approved = _requirements().model_copy(update={"image": ImageChoice(
        image_id="img-gone", name="CC-Ubuntu24.04", supported=True, testbed="Chameleon"
    )})
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        desc_id, version = store.create_description(owner_a, "test", approved, _RUBRIC)
        thread = store.create_thread(owner_a, "New chat")
        store.append_message(
            thread.thread_id, "assistant", "description", "v1", {}, owner_id=owner_a,
            description_id=desc_id, description_version=version,
        )
        run_id = sequential.provision(
            owner_id=owner_a, description_id=desc_id, version=version, project_id=None,
            keypair=KEYPAIR, adapter=adapter, site=UC, settings=settings,
        )
        model = FakeModel([_revise(), _search(), _answer()])
        final, _ = _drive(model, owner_a, run_id, adapter, config, settings)

    assert final == run_id, "nothing new was provisioned"
    assert len(model.calls) == 3, "the build stopped once the version waited"
    assert [r.description_version for r in store.runs_for_description(owner_a, desc_id)] == [1]
    last = store.read_messages(owner_a, thread.thread_id)[-1]
    assert (last.kind, last.description_version) == ("release", 2)
    held = ", ".join(f"{r.kind} {r.provider_id} at {UC}" for r in store.live_resources(run_id))
    assert last.text == (
        f"The experiment agent wants to release {held}, then build version 2, which changes "
        "the image. It says: no such image"
    )


def test_a_revision_to_a_longer_lease_waits_for_the_users_approval(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner_a, adapter, settings, rubric=_WRONG_RUBRIC)
        model = FakeModel([_revise(), _search(hours=4), _answer(hours=4)])
        final, _ = _drive(model, owner_a, run_id, adapter, config, settings)

    assert final == run_id, "nothing new was provisioned"
    proposal = store.pending_release(owner_a, run_id)
    assert proposal is not None and proposal.detail["changes"] == "the lease length"
    assert [p["result"] for p in _payloads(owner_a, run_id, "tool_called")] == [
        "version 2 replaces this run, so it waits for the user to release it"
    ]


def _revision_awaiting(
    owner: str, project_id: str | None = None
) -> tuple[uuid.UUID, FaultyAdapter, Config]:
    """A failed run holding hardware, from a chat, with the agent's revision of it proposed."""
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner, adapter, settings, rubric=_WRONG_RUBRIC, project_id=project_id)
        description_id = store.get_run(owner, run_id).description_id
        chat = store.create_thread(owner, "a chat", site_keypairs={UC: "my-uc-key"})
        store.append_message(
            chat.thread_id, "assistant", "description", "a description", owner_id=owner,
            description_id=description_id, description_version=1,
        )
        _drive(
            FakeModel([_revise(), _search(), _answer()]), owner, run_id, adapter, config,
            settings,
        )
    return run_id, adapter, config


def _confirm(owner: str, run_id: uuid.UUID, adapter: FaultyAdapter, config: Config, monkeypatch):
    """Answer the agent's proposal yes, and run the build it queues. Return the request."""
    proposal = store.pending_release(owner, run_id)
    assert proposal is not None and proposal.thread_id is not None
    releases.confirm(owner, proposal.request_id, "web")
    job = store.enqueue_job(owner, proposal.thread_id, "build", {
        "description_id": str(store.get_run(owner, run_id).description_id),
        "version": proposal.version,
        "replaces_run_id": str(run_id),
        "request_id": str(proposal.request_id),
        "change": proposal.detail["change"],
    })
    monkeypatch.setattr(
        runs.credentials, "build_adapter", lambda site, config, user=None: audited(adapter)
    )
    runs.build_experiment(job, config)
    return proposal.request_id


def _replacement(owner: str, run_id: uuid.UUID) -> store.Run:
    description_id = store.get_run(owner, run_id).description_id
    return next(
        r for r in store.runs_for_description(owner, description_id) if r.description_version == 2
    )


def test_a_confirmed_revision_releases_the_run_and_records_it(owner_a, monkeypatch):
    run_id, adapter, config = _revision_awaiting(owner_a)
    held = {r.provider_id for r in store.live_resources(run_id)}

    request_id = _confirm(owner_a, run_id, adapter, config, monkeypatch)

    assert store.live_resources(run_id) == []
    entries = store.decision_request(owner_a, request_id)
    assert [(e.step, e.actor, e.action) for e in entries] == [
        ("proposed", "agent", "release_and_build"), ("confirmed", "user", "release_and_build"),
    ]
    with psycopg.connect(TEST_DB_URL) as conn:
        deleted = conn.execute(
            "SELECT provider_id FROM audit.testbed_calls "
            "WHERE request_id = %s AND run_id = %s AND operation LIKE 'delete_%%'",
            [request_id, run_id],
        ).fetchall()
    assert {d for (d,) in deleted} == held
    assert _replacement(owner_a, run_id).state == RunState.passed


def test_a_confirmed_revision_boots_with_the_failed_runs_keypair(owner_a, monkeypatch):
    run_id, adapter, config = _revision_awaiting(owner_a)

    _confirm(owner_a, run_id, adapter, config, monkeypatch)

    plan = _replacement(owner_a, run_id).plan
    assert plan is not None and plan.keypair() == KEYPAIR


def test_a_confirmed_revision_builds_in_the_failed_runs_project(owner_a, monkeypatch):
    run_id, adapter, config = _revision_awaiting(owner_a, project_id="CHI-000002")
    chat = store.pending_release(owner_a, run_id)
    assert chat is not None and chat.thread_id is not None
    store.update_thread(owner_a, chat.thread_id, project_id="CHI-000001")
    scopes: list = []
    monkeypatch.setattr(
        runs, "user_scope", lambda owner, project: scopes.append(project)
    )

    _confirm(owner_a, run_id, adapter, config, monkeypatch)

    assert set(scopes) == {"CHI-000002"}
    assert _replacement(owner_a, run_id).project_id == "CHI-000002"


def test_a_confirmed_revision_builds_nothing_if_the_run_cannot_be_released(
    owner_a, monkeypatch
):
    run_id, adapter, config = _revision_awaiting(owner_a)

    def refuse(site: str, lease_id: str) -> None:
        raise TransientError("the lease service is down")

    adapter.delete_lease = refuse  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="still live"):
        _confirm(owner_a, run_id, adapter, config, monkeypatch)

    description_id = store.get_run(owner_a, run_id).description_id
    built = store.runs_for_description(owner_a, description_id)
    assert [r.description_version for r in built] == [1]


def _start_unplanned(
    owner: str, adapter: FakeAdapter, settings: Settings, site_keypairs: dict[str, str] | None
) -> uuid.UUID:
    """Run a description its plan cannot build, from a chat with site_keypairs."""
    unbuildable = _requirements().model_copy(update={"items": [
        *_requirements().items, Requirement(key="count", value=2, source="stated"),
    ]})
    desc_id, version = store.create_description(owner, "test", unbuildable, _RUBRIC)
    chat = store.create_thread(owner, "a chat", site_keypairs=site_keypairs)
    store.append_message(
        chat.thread_id, "assistant", "description", "a description", owner_id=owner,
        description_id=desc_id, description_version=version,
    )
    run_id = sequential.provision(
        owner_id=owner, description_id=desc_id, version=version, project_id=None,
        keypair=KEYPAIR, adapter=adapter, site=UC, settings=settings,
    )
    assert store.get_run(owner, run_id).plan is None
    return run_id


def test_a_revision_of_a_run_that_never_planned_boots_with_the_chats_keypair(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start_unplanned(owner_a, adapter, settings, {UC: "my-uc-key"})

        final, _ = _drive(
            FakeModel([_revise(), _search(), _answer()]), owner_a, run_id, adapter, config,
            settings,
        )

    plan = store.get_run(owner_a, final).plan
    assert final != run_id
    assert plan is not None and plan.keypair() == "my-uc-key"


def test_a_revision_with_no_keypair_at_the_site_is_refused(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start_unplanned(owner_a, adapter, settings, None)

        final, _ = _drive(
            FakeModel([_revise(), _search(), _answer(), _abandon()]), owner_a, run_id,
            adapter, config, settings,
        )

    assert final == run_id
    refusals = [p["result"] for p in _payloads(owner_a, run_id, "tool_called")]
    assert "no keypair is chosen at CHI@UC" in refusals


def test_a_revise_links_the_new_run_back_to_the_old(owner_a, monkeypatch):
    run_id, adapter, config = _revision_awaiting(owner_a)

    _confirm(owner_a, run_id, adapter, config, monkeypatch)

    new_run = _replacement(owner_a, run_id).run_id
    link = _payloads(owner_a, new_run, "description_revision_requested")
    assert len(link) == 1
    assert link[0]["from_run_id"] == str(run_id)
    assert link[0]["from_version"] == 1
    assert link[0]["version"] == 2


def test_the_change_reaches_the_description_agent_and_nothing_else_does(
    owner_a
):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner_a, adapter, settings, image_name="CC-Nonesuch99.04")
        model = FakeModel([_revise("use CC-Ubuntu24.04 and keep everything else"),
                           _search(), _answer()])
        _drive(model, owner_a, run_id, adapter, config, settings)

    opening = model.calls[1][1]["content"]
    assert "use CC-Ubuntu24.04 and keep everything else" in opening
    assert "No image found" not in opening
    assert str(run_id) not in opening, "the run is not the experiment description agent's concern"


def test_a_declined_revision_continues_the_loop(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start(owner_a, adapter, settings, image_name="CC-Nonesuch99.04")

        model = FakeModel([_revise(), _decline(), _abandon()])
        final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["revise", "abandon"]
    assert [t.round for t in rounds] == [1, 2], "rounds keep counting across the handover"
    assert final == run_id, "no new run was created"
    refusals = [p["result"] for p in _payloads(owner_a, run_id, "tool_called")]
    assert any("declined that change" in r for r in refusals)
    proposal = store.pending_release(owner_a, run_id)
    assert proposal is not None and proposal.detail["action"] == "abandon"


def test_a_revise_at_an_unconfigured_site_is_refused_with_that_reason(owner_a):
    settings = _settings()
    config = Config(defaults=settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    adapter.capabilities = lambda: Capabilities(sites=[])  # type: ignore[method-assign]

    model = FakeModel([_revise(), _abandon()])
    final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["revise", "abandon"]
    assert final == run_id
    refusals = [p["result"] for p in _payloads(owner_a, run_id, "tool_called")]
    assert any("no catalog for" in r for r in refusals)


def test_an_unparseable_reply_produces_a_reason_and_the_loop_continues(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    model = FakeModel(["I would reserve a different node.", _retry()])
    _, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["unparsed", "retry"]
    assert rounds[0].reasons, "the parse failure was recorded"
    assert store.get_run(owner_a, run_id).state == RunState.passed


def test_a_repeated_read_is_refused(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    model = FakeModel([_inspect("leases"), _inspect("leases"), _retry()])
    _, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["inspect", "inspect", "retry"]
    assert rounds[0].reasons == []
    assert "already been read" in rounds[1].reasons[0]


def test_a_read_is_recorded_and_shown_back_to_the_model(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    model = FakeModel([_inspect("availability"), _retry()])
    _drive(model, owner_a, run_id, adapter, config, settings)

    reads = _payloads(owner_a, run_id, "tool_called")
    assert len(reads) == 1 and reads[0]["read"] == "availability"
    assert "compute_skylake" in reads[0]["result"]
    assert reads[0]["result"] in model.calls[1][1]["content"], "the reading came back"


def test_the_agent_can_measure_the_host_against_the_rubric(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, facts=HostFacts(os_release="Ubuntu 22.04"))
    run_id = _start(owner_a, adapter, settings)
    assert store.get_run(owner_a, run_id).state == RunState.failed

    _drive(FakeModel([_inspect("validate"), _abandon()]), owner_a, run_id, adapter,
           config, settings)

    reads = _payloads(owner_a, run_id, "tool_called")
    assert "FAIL os" in reads[0]["result"]
    assert "Ubuntu 22.04" in reads[0]["result"], "the value it actually found"


def test_a_rubric_failure_reaches_the_agent_with_its_measurements(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, facts=HostFacts(os_release="Ubuntu 22.04"))
    run_id = _start(owner_a, adapter, settings)
    run = store.get_run(owner_a, run_id)
    desc = store.get_description(owner_a, run.description_id, run.description_version)

    text = observe.render(
        desc, run, store.read_events(owner_a, run_id), store.live_resources(run_id),
        4, 3, 40,
    )

    assert "expected 'Ubuntu 24.04', got 'Ubuntu 22.04'" in text


def test_the_agent_is_shown_the_retries_the_run_already_made(owner_a):
    settings = _settings(boot_attempts=2)
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=2)
    run_id = _start(owner_a, adapter, settings)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.failed
    desc = store.get_description(owner_a, run.description_id, run.description_version)

    text = observe.render(
        desc, run, store.read_events(owner_a, run_id), store.live_resources(run_id),
        4, 3, 40,
    )

    assert "step_retried" in text, "boot already retried once, and it must be visible"


def test_a_read_may_be_taken_again_after_a_retry(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=2)
    run_id = _start(owner_a, adapter, settings)

    model = FakeModel([_inspect("leases"), _retry(), _inspect("leases"), _abandon()])
    _, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["inspect", "retry", "inspect", "abandon"]
    assert all(not t.reasons for t in rounds), "the retry started the run again"


def test_the_lease_budget_leaves_only_abandon(owner_a):
    settings = _settings(max_leases_per_build=1)
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    assert store.live_resources(run_id), "the failed run still holds its lease"

    model = FakeModel([_retry(), _abandon()])
    final, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["retry", "abandon"]
    assert "lease budget" in rounds[0].reasons[0]
    assert final == run_id
    assert store.pending_release(owner_a, run_id) is not None
    assert store.live_resources(run_id), "nothing is released before the user answers"


def test_abandoning_a_run_that_holds_nothing_marks_it_abandoned(owner_a):
    settings = _settings()
    with serving(_TWO_FREE) as (root, _):
        config = _config(settings, root)
        adapter = _adapter(config)
        run_id = _start_unplanned(owner_a, adapter, settings, None)

        _drive(FakeModel([_abandon()]), owner_a, run_id, adapter, config, settings)

    assert store.get_run(owner_a, run_id).state == RunState.abandoned
    assert store.pending_release(owner_a, run_id) is None


def test_the_round_budget_ends_the_build(owner_a):
    settings = _settings(max_build_rounds=2)
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    model = FakeModel([_inspect("leases"), _inspect("instances"), _retry()])
    _, rounds = _drive(model, owner_a, run_id, adapter, config, settings)

    assert len(rounds) == 2, "the third round was never asked for"
    assert store.get_run(owner_a, run_id).state == RunState.failed


def test_the_budget_is_read_back_from_events_not_from_memory(owner_a):
    settings = _settings(max_build_rounds=2)
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    _drive(FakeModel([_inspect("leases")]), owner_a, run_id, adapter, config, settings)
    _, rounds = _drive(
        FakeModel([_inspect("instances"), _retry()]), owner_a, run_id, adapter,
        config, settings,
    )

    assert len(rounds) == 1, "one round was already spent on the first call"


def test_a_model_that_cannot_be_reached_ends_the_build(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    _, rounds = _drive(FakeModel([]), owner_a, run_id, adapter, config, settings)

    assert [t.action for t in rounds] == ["error"]
    assert rounds[0].reasons == ["the fake model has no replies left"]
    assert store.get_run(owner_a, run_id).state == RunState.failed


def test_every_round_is_recorded_on_the_run(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    _drive(FakeModel([_inspect("leases"), _retry()]), owner_a, run_id, adapter,
          config, settings)

    kinds = _kinds(owner_a, run_id)
    assert kinds.count("model_turn") == 2
    assert kinds.count("tool_called") == 1


def test_the_input_of_each_round_is_kept_out_of_the_events_shown(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    model = FakeModel([_inspect("leases"), _retry()])

    _drive(model, owner_a, run_id, adapter, config, settings)

    turns = [e for e in store.read_events(owner_a, run_id) if e.kind == "model_turn"]
    assert all("sent" not in e.payload for e in turns)
    with psycopg.connect(TEST_DB_URL) as conn:
        stored = conn.execute(
            "SELECT model_input FROM experiments.run_events "
            "WHERE run_id = %s AND kind = 'model_turn' ORDER BY seq",
            [run_id],
        ).fetchall()
    assert [row[0] for row in stored] == model.calls


def test_retry_is_refused_on_a_released_run(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    sequential.teardown(run_id, adapter)
    run = store.get_run(owner_a, run_id)
    assert run.state == RunState.abandoned

    reasons = validate_build_step(RetryStep(action="retry", reason="again"), run, [], 3, None)

    assert reasons and "abandoned" in reasons[0]


def test_retry_is_refused_when_the_run_has_no_plan(owner_a):
    desc_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test",
        requirements=_requirements(), rubric=_RUBRIC,
    )
    run = store.get_run(owner_a, store.create_run(owner_a, desc_id, version, None))

    reasons = validate_build_step(RetryStep(action="retry", reason="again"), run, [], 3, None)

    assert reasons == ["this run has no plan; there is nothing to run again"]


def test_the_runner_does_not_depend_on_a_model():
    runner = Path(sequential.__file__).parent
    offenders = [
        path.name
        for path in runner.glob("*.py")
        if re.search(
            r"^\s*(from|import)\s+cloud_conversations\.(llm|agent)", path.read_text(), re.M
        )
    ]

    assert offenders == []


def test_the_observation_carries_the_failure_the_ask_and_the_run_log(owner_a):
    settings = _settings()
    config = _config(settings)
    adapter = _adapter(config, unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    run = store.get_run(owner_a, run_id)
    desc = store.get_description(owner_a, run.description_id, run.description_version)
    live = store.live_resources(run_id)

    text = observe.render(
        desc, run, store.read_events(owner_a, run_id), live, 4, 3, 40
    )

    assert "kind: plan" in text, "the failure being diagnosed"
    assert "compute_skylake" in text, "the requirements that were approved"
    assert "step_failed" in text, "the run's own history"
    assert f"lease {live[0].provider_id} at {live[0].site}" in text, "what is still live"
    assert "rounds left: 4" in text and "leases left: 3" in text


def test_the_agent_reads_the_knowledge_base_and_the_read_is_on_the_run(owner_a):
    settings = _settings()
    adapter = _adapter(_config(settings), unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    shelf = Shelf()
    model = FakeModel([knowledge_search("what makes a host unschedulable?"), _retry()])

    outcome = build(model, owner_a, run_id, adapter, settings, shelf)

    assert [t.action for t in outcome.rounds] == ["search_knowledge_base", "retry"]
    assert shelf.asked == ["what makes a host unschedulable?"]
    assert _payloads(owner_a, run_id, "tool_called") == [{
        "v": 1,
        "read": "search_knowledge_base",
        "question": "what makes a host unschedulable?",
        "result": shelf.text,
    }]
    assert shelf.text in model.calls[1][-1]["content"]


def test_a_repeated_question_costs_a_round_and_no_search(owner_a):
    settings = _settings(max_build_rounds=3)
    adapter = _adapter(_config(settings), unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    shelf = Shelf()

    outcome = build(
        FakeModel([knowledge_search(), knowledge_search(), _retry()]),
        owner_a, run_id, adapter, settings, shelf,
    )

    assert any("already been asked" in r for r in outcome.rounds[1].reasons)
    assert len(shelf.asked) == 1


def test_a_question_is_refused_where_there_is_no_knowledge_base(owner_a):
    settings = _settings()
    adapter = _adapter(_config(settings), unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    run = store.get_run(owner_a, run_id)

    reasons = validate_build_step(
        KnowledgeStep(action="search_knowledge_base", question="anything?"),
        run, [], 3, None,
    )

    assert reasons == ["there is no knowledge base to search here"]


def test_the_knowledge_tool_is_offered_only_where_there_is_a_knowledge_base(owner_a):
    settings = _settings()
    adapter = _adapter(_config(settings), unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)

    without = FakeModel([_abandon()])
    build(without, owner_a, run_id, adapter, settings)

    second = _adapter(_config(settings), unschedulable_boots=1)
    with_shelf = FakeModel([_abandon()])
    build(with_shelf, owner_a, _start(owner_a, second, settings), second, settings,
          Shelf())

    assert "search_knowledge_base" not in without.calls[0][0]["content"]
    assert "search_knowledge_base" in with_shelf.calls[0][0]["content"]


def test_a_second_question_reaches_the_knowledge_base(owner_a):
    settings = _settings(max_build_rounds=3)
    adapter = _adapter(_config(settings), unschedulable_boots=1)
    run_id = _start(owner_a, adapter, settings)
    shelf = Shelf()

    outcome = build(
        FakeModel([
            knowledge_search("what makes a host unschedulable?"),
            knowledge_search("which image boots on skylake?"),
            _retry(),
        ]),
        owner_a, run_id, adapter, settings, shelf,
    )

    assert [t.reasons for t in outcome.rounds] == [[], [], []]
    assert shelf.asked == [
        "what makes a host unschedulable?",
        "which image boots on skylake?",
    ]
