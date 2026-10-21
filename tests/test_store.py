"""Contract tests for store.py."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.schema.description import Check, Requirement, Requirements
from cloud_conversations.schema.runs import IllegalTransitionError, Plan, PlanStep, RunState


@pytest.fixture()
def reqs() -> Requirements:
    return Requirements(
        duration_hours=2,
        items=[Requirement(key="node_type", value="compute_skylake", source="stated")],
    )


@pytest.fixture()
def rubric() -> list[Check]:
    return [Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq")]


def _run(owner, reqs, rubric) -> uuid.UUID:
    did, ver = store.create_description(owner, "conv-1", reqs, rubric)
    return store.create_run(owner, did, ver, None)


def test_description_roundtrips(owner_a, reqs, rubric):
    did, ver = store.create_description(
        owner_a, "conv-1", reqs, rubric, unsupported=["gpu"], open_questions=["which site?"]
    )
    got = store.get_description(owner_a, did, ver)
    assert got.requirements == reqs
    assert got.rubric[0].kind == "cpu_arch"
    assert got.unsupported == ["gpu"]
    assert got.open_questions == ["which site?"]


def test_a_description_stored_with_a_keypair_still_loads(owner_a, reqs, rubric):
    did, ver = store.create_description(owner_a, "conv-1", reqs, rubric)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE experiments.experiment_descriptions"
            " SET requirements = requirements || '{\"ssh_key_ref\": \"cc-keypair\"}'"
            " WHERE description_id = %s",
            [did],
        )

    assert store.get_description(owner_a, did, ver).requirements == reqs


def test_another_owner_cannot_read_a_description(owner_a, owner_b, reqs, rubric):
    did, ver = store.create_description(owner_a, "conv-1", reqs, rubric)
    with pytest.raises(store.NotFoundError):
        store.get_description(owner_b, did, ver)


def test_another_owner_cannot_append_a_version(owner_a, owner_b, reqs, rubric):
    did, _ = store.create_description(owner_a, "conv-1", reqs, rubric)
    with pytest.raises(store.NotFoundError):
        store.create_description(owner_b, "conv-1", reqs, rubric, description_id=did)


def test_concurrent_version_allocation_is_serialised(owner_a, reqs, rubric):
    did, _ = store.create_description(owner_a, "conv-1", reqs, rubric)
    versions: list[int] = []
    errors: list[Exception] = []

    def writer() -> None:
        try:
            versions.append(
                store.create_description(
                    owner_a, "conv-1", reqs, rubric, description_id=did
                )[1]
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(versions) == list(range(2, 10)), "every writer must get a distinct version"


def test_run_starts_in_planning(owner_a, reqs, rubric):
    run = store.get_run(owner_a, _run(owner_a, reqs, rubric))
    assert run.state == RunState.planning
    assert run.resume_count == 0
    assert run.results is None


def test_another_owner_cannot_read_a_run(owner_a, owner_b, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(store.NotFoundError):
        store.get_run(owner_b, run_id)


def test_unknown_run_id_is_not_found():
    with pytest.raises(store.NotFoundError):
        store.get_run_unscoped(uuid.uuid4())


def test_update_run_writes_jsonb_fields(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    plan = Plan(
        plan_id="p1",
        site="FAKE@BM",
        steps=[PlanStep(step_id="s1", action="reserve", args={"node_type": "x"})],
    )
    store.update_run(owner_a, run_id, plan=plan)
    got = store.get_run(owner_a, run_id)
    assert got.plan is not None and got.plan.steps[0].action == "reserve"


def test_update_run_rejects_unknown_fields(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(store.AccessError):
        store.update_run(owner_a, run_id, project_id="charge-someone-else")


def test_update_run_enforces_the_transition_table(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(IllegalTransitionError):
        store.update_run(owner_a, run_id, state=RunState.passed)


def test_payload_gets_a_version_stamp(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.append_event(run_id, "runner", "run_started", {})
    assert store.read_events(owner_a, run_id)[0].payload["v"] == 1


def test_unknown_event_kind_is_rejected(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(ValueError, match="Unknown event kind"):
        store.append_event(run_id, "runner", "not_a_kind", {})


def test_unknown_actor_is_rejected(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(ValueError, match="Unknown actor"):
        store.append_event(run_id, "gremlin", "run_started", {})


def test_credential_shaped_keys_are_redacted_not_rejected(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.append_event(
        run_id,
        "runner",
        "step_started",
        {"args": {"hf_token": "hunter2", "node_type": "skylake"}},
    )
    payload = store.read_events(owner_a, run_id)[0].payload
    assert payload["args"]["hf_token"] == "[redacted]"
    assert payload["args"]["node_type"] == "skylake"


def test_redaction_recurses_into_lists(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.append_event(
        run_id,
        "tool",
        "tool_called",
        {"args": [{"password": "s3cret"}, {"safe": "yes"}]},
    )
    args = store.read_events(owner_a, run_id)[0].payload["args"]
    assert args[0]["password"] == "[redacted]"
    assert args[1]["safe"] == "yes"


def test_append_event_rejects_a_foreign_owner(owner_a, owner_b, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    with pytest.raises(store.NotFoundError):
        store.append_event(run_id, "runner", "run_started", {}, owner_id=owner_b)


def test_read_events_is_owner_scoped(owner_a, owner_b, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.append_event(run_id, "runner", "run_started", {})
    with pytest.raises(store.NotFoundError):
        store.read_events(owner_b, run_id)


def test_seq_is_gapless_under_concurrency(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    seqs: list[int] = []

    def writer() -> None:
        seqs.append(store.append_event(run_id, "runner", "step_started", {}))

    threads = [threading.Thread(target=writer) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(seqs) == list(range(1, 21))


def test_release_is_idempotent(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.record_resource(run_id, "lease-1", "lease", "FAKE@BM", None)
    assert [r.provider_id for r in store.live_resources(run_id)] == ["lease-1"]
    store.release_resource_with_event(run_id, "lease", "lease-1", "FAKE@BM")
    store.release_resource_with_event(run_id, "lease", "lease-1", "FAKE@BM")
    assert store.live_resources(run_id) == []


def test_kind_disambiguates_provider_ids(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.record_resource(run_id, "same-id", "lease", "FAKE@BM", None)
    store.record_resource(run_id, "same-id", "instance", "FAKE@BM", None)
    assert len(store.live_resources(run_id)) == 2


def test_resource_requires_a_real_run(reqs, rubric):
    with pytest.raises(store.NotFoundError):
        store.record_resource(uuid.uuid4(), "orphan", "lease", "FAKE@BM", None)


def test_lookup_by_key_finds_the_live_resource(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.record_resource(run_id, "lease-1", "lease", "FAKE@BM", None, idempotency_key="k1")
    found = store.resource_by_idempotency_key("k1")
    assert found is not None and found.provider_id == "lease-1"


def test_lookup_by_key_ignores_released_resources(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.record_resource(run_id, "lease-1", "lease", "FAKE@BM", None, idempotency_key="k1")
    store.release_resource_with_event(run_id, "lease", "lease-1", "FAKE@BM")

    assert store.resource_by_idempotency_key("k1") is None
    assert store.record_resource(
        run_id, "lease-2", "lease", "FAKE@BM", None, idempotency_key="k1"
    ) == "inserted"


def test_resource_and_event_are_written_together(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    assert store.create_resource_with_event(
        run_id, "lease-1", "lease", "FAKE@BM", None, idempotency_key="k1"
    ) == "inserted"
    events = [e for e in store.read_events(owner_a, run_id) if e.kind == "resource_created"]
    assert len(events) == 1
    assert events[0].payload["provider_id"] == "lease-1"


def test_no_event_when_no_row_was_inserted(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.create_resource_with_event(
        run_id, "lease-1", "lease", "FAKE@BM", None, idempotency_key="k1"
    )
    assert store.create_resource_with_event(
        run_id, "lease-1", "lease", "FAKE@BM", None, idempotency_key="k1"
    ) == "replay"
    created = [e for e in store.read_events(owner_a, run_id) if e.kind == "resource_created"]
    assert len(created) == 1
    assert len(store.live_resources(run_id)) == 1


def test_a_runs_live_resources_are_only_its_own(owner_a, owner_b, reqs, rubric):
    a = _run(owner_a, reqs, rubric)
    b = _run(owner_b, reqs, rubric)
    store.record_resource(a, "l-a", "lease", "FAKE@BM", None)
    store.record_resource(b, "l-b", "lease", "FAKE@BM", None)
    assert [r.provider_id for r in store.live_resources(a)] == ["l-a"]


def test_a_release_names_the_run_it_releases_for(owner_a, owner_b, reqs, rubric):
    a = _run(owner_a, reqs, rubric)
    b = _run(owner_b, reqs, rubric)
    store.record_resource(a, "l-a", "lease", "FAKE@BM", None)

    store.release_resource_with_event(b, "lease", "l-a", "FAKE@BM")

    assert [r.provider_id for r in store.live_resources(a)] == ["l-a"]


def test_duplicate_key_survives_the_production_write_path(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    assert (
        store.create_resource_with_event(
            run_id, "lease-A", "lease", "FAKE@BM", None, idempotency_key="k1"
        )
        == "inserted"
    )

    with pytest.raises(store.DuplicateKeyError):
        store.create_resource_with_event(
            run_id, "lease-B", "lease", "FAKE@BM", None, idempotency_key="k1"
        )

    ids = {r.provider_id for r in store.live_resources(run_id)}
    assert ids == {"lease-A", "lease-B"}, "the leaked resource must remain reapable"
    created = [e for e in store.read_events(owner_a, run_id) if e.kind == "resource_created"]
    assert len(created) == 1, "the log must not claim the untagged row"


def test_token_counts_survive_redaction(owner_a, reqs, rubric):
    run_id = _run(owner_a, reqs, rubric)
    store.append_event(
        run_id,
        "agent",
        "model_turn",
        {
            "model": "some-model",
            "prompt_tokens": 1234,
            "completion_tokens": 56,
            "api_key": "sk-should-vanish",
        },
    )
    payload = store.read_events(owner_a, run_id)[0].payload
    assert payload["prompt_tokens"] == 1234
    assert payload["completion_tokens"] == 56
    assert payload["api_key"] == "[redacted]"


_TRANSCRIPT = [
    {"round": 1, "action": "search", "text": '{"action":"search"}', "reasons": []},
    {"round": 2, "action": "answer", "text": '{"action":"answer"}', "reasons": []},
]


def test_conversation_roundtrips(owner_a, reqs, rubric):
    did, ver = store.create_description(owner_a, "conv-1", reqs, rubric)
    cid = store.create_conversation(
        owner_a, "a skylake node for two hours", _TRANSCRIPT, did, ver
    )

    got = store.get_conversation(owner_a, cid)
    assert got.prompt == "a skylake node for two hours"
    assert got.transcript == _TRANSCRIPT
    assert (got.description_id, got.description_version) == (did, ver)


def test_another_owner_cannot_read_a_conversation(owner_a, owner_b, reqs, rubric):
    did, ver = store.create_description(owner_a, "conv-1", reqs, rubric)
    cid = store.create_conversation(owner_a, "a skylake node", _TRANSCRIPT, did, ver)
    with pytest.raises(store.NotFoundError):
        store.get_conversation(owner_b, cid)


def test_conversations_for_a_description_come_back_in_version_order(owner_a, reqs, rubric):
    did, v1 = store.create_description(owner_a, "conv-1", reqs, rubric)
    _, v2 = store.create_description(
        owner_a, "conv-2", reqs, rubric, description_id=did, parent_version=v1
    )
    store.create_conversation(owner_a, "make it 4 hours", _TRANSCRIPT, did, v2)
    store.create_conversation(owner_a, "a skylake node", _TRANSCRIPT, did, v1)

    got = store.conversations_for_description(owner_a, did)
    assert [(c.description_version, c.prompt) for c in got] == [
        (1, "a skylake node"),
        (2, "make it 4 hours"),
    ]


def test_a_start_time_survives_the_round_trip(owner_a, rubric):
    tuesday = datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)
    reqs = Requirements(
        site="FAKE@BM",
        start_at=tuesday,
        duration_hours=2,
        items=[Requirement(key="node_type", value="compute_skylake", source="stated")],
    )
    did, ver = store.create_description(owner_a, "conv-1", reqs, rubric)

    assert store.get_description(owner_a, did, ver).requirements.start_at == tuesday


def test_runs_for_a_description_come_back_oldest_first(owner_a, owner_b, bm_requirements):
    did, _ = store.create_description(owner_a, "conv", bm_requirements, [])
    first = store.create_run(owner_a, did, 1, None)
    second = store.create_run(owner_a, did, 1, None)
    other, _ = store.create_description(owner_a, "conv", bm_requirements, [])
    store.create_run(owner_a, other, 1, None)

    assert [r.run_id for r in store.runs_for_description(owner_a, did)] == [first, second]
    assert store.runs_for_description(owner_b, did) == [], "scoped to its owner"
