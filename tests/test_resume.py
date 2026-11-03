"""A build resumes after its worker stops, unless it is too late."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations import audit
from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
from cloud_conversations.runner import sequential
from cloud_conversations.schema.runs import RunState
from cloud_conversations.schema.service import Service
from cloud_conversations.worker import loop, runs
from tests.conftest import FAST

pytestmark = pytest.mark.usefixtures("logged_in")


class _Stopped(BaseException):
    """The worker's process stopped."""


def _stop(*args, **kwargs):
    raise _Stopped()


@pytest.fixture()
def adapter(monkeypatch) -> FakeAdapter:
    """One fake testbed for every attempt at a build."""
    fake = FakeAdapter(sites=default_fake_sites(), config=FAST)
    monkeypatch.setattr(runs.credentials, "build_adapter", lambda *args, **kwargs: fake)
    return fake


def _queued(owner: str, requirements, rubric) -> store.Job:
    thread = store.create_thread(owner, "New chat", ssh_key_ref="cc-keypair")
    did, version = store.create_description(
        owner, "test", requirements.model_copy(update={"site": "FAKE@BM"}), rubric
    )
    request_id = audit.approved(owner, "web", description_id=did, version=version)
    return store.enqueue_job(owner, thread.thread_id, "build", {
        "description_id": str(did), "version": version, "request_id": str(request_id),
    })


def _claimed() -> store.Job:
    job = store.claim_job(["build"], 60)
    assert job is not None
    return job


def _stopped_while_booting(monkeypatch, adapter: FakeAdapter) -> store.Job:
    """Stop the worker of a build once it has a lease and an instance."""
    job = _claimed()
    with monkeypatch.context() as m:
        m.setattr(adapter, "wait_instance_active", _stop)
        with pytest.raises(_Stopped):
            runs.build_experiment(job, FAST)
    with store.transaction() as conn:
        conn.execute("UPDATE chat.jobs SET claimed_until = now() WHERE job_id = %s", [job.job_id])
    return job


def _approved_ago(job: store.Job, hours: float) -> None:
    with store.transaction() as conn:
        conn.execute(
            "UPDATE chat.jobs SET created_at = now() - make_interval(hours => %s) "
            "WHERE job_id = %s",
            [hours, job.job_id],
        )


def _notices(owner: str, job: store.Job) -> list[str]:
    return [m.text for m in store.read_messages(owner, job.thread_id) if m.kind == "notice"]


def _run(owner: str, job: store.Job) -> store.Run:
    (run,) = store.runs_for_description(owner, job.payload["description_id"])
    return run


def test_a_build_whose_worker_stopped_resumes_with_its_lease(
    monkeypatch, owner_a, bm_requirements, bm_rubric, adapter
):
    queued = _queued(owner_a, bm_requirements, bm_rubric)
    _stopped_while_booting(monkeypatch, adapter)

    runs.build_experiment(_claimed(), FAST)

    assert _run(owner_a, queued).state == RunState.passed
    assert (len(adapter.leases), len(adapter.instances)) == (1, 1)
    assert _notices(owner_a, queued) == ["The build was interrupted, and is resuming."]


def test_a_late_build_that_holds_a_lease_resumes(
    monkeypatch, owner_a, bm_requirements, bm_rubric, adapter
):
    queued = _queued(owner_a, bm_requirements, bm_rubric)
    _stopped_while_booting(monkeypatch, adapter)
    _approved_ago(queued, hours=5)

    runs.build_experiment(_claimed(), FAST)

    assert _run(owner_a, queued).state == RunState.passed
    assert len(adapter.leases) == 1


def test_a_late_build_with_nothing_reserved_is_not_started(
    owner_a, bm_requirements, bm_rubric, adapter
):
    queued = _queued(owner_a, bm_requirements, bm_rubric)
    _approved_ago(queued, hours=2)
    job = _claimed()

    runs.build_experiment(job, FAST)

    assert store.runs_for_description(owner_a, queued.payload["description_id"]) == []
    assert adapter.leases == {}
    assert _notices(owner_a, queued) == [runs.LATE.format(due=job.created_at)]


def test_a_build_whose_lease_ended_while_it_was_stopped_is_stopped_and_says_so(
    monkeypatch, owner_a, bm_requirements, bm_rubric, adapter
):
    queued = _queued(owner_a, bm_requirements, bm_rubric)
    _stopped_while_booting(monkeypatch, adapter)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE experiments.run_resources SET expires_at = now() - interval '1 minute' "
            "WHERE kind = 'lease'"
        )

    runs.build_experiment(_claimed(), FAST)

    run = _run(owner_a, queued)
    assert run.state == RunState.failed
    assert run.failure is not None
    assert run.failure.message == runs.LEASE_ENDED
    assert _notices(owner_a, queued)[-1] == runs.LEASE_ENDED
    assert len(adapter.leases) == 1


def test_a_build_whose_lease_starts_later_gives_its_worker_back_until_then(
    owner_a, bm_requirements, bm_rubric, adapter
):
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    queued = _queued(owner_a, bm_requirements.model_copy(update={"start_at": start}), bm_rubric)

    loop.work_once(("build",), Service())

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT state, run_after FROM chat.jobs WHERE job_id = %s", [queued.job_id]
        ).fetchone()
    assert row == {"state": "queued", "run_after": start}
    assert store.claim_job(["build"], 60) is None
    assert _notices(owner_a, queued) == [
        f"The lease at FAKE@BM starts at {start:%Y-%m-%d %H:%M} UTC. The build continues then."
    ]


def test_a_build_that_waited_for_its_lease_continues_once_it_starts(
    owner_a, bm_requirements, bm_rubric, adapter
):
    start = datetime.now(timezone.utc) + timedelta(days=2)
    queued = _queued(owner_a, bm_requirements.model_copy(update={"start_at": start}), bm_rubric)
    loop.work_once(("build",), Service())
    (lease,) = adapter.leases.values()
    lease.start = datetime.now(timezone.utc)
    with store.transaction() as conn:
        conn.execute("UPDATE chat.jobs SET run_after = now() WHERE job_id = %s", [queued.job_id])

    assert loop.work_once(("build",), Service())

    assert _run(owner_a, queued).state == RunState.passed
    assert len(adapter.leases) == 1


def test_a_build_that_stopped_before_planning_its_run_starts_another(
    owner_a, bm_requirements, bm_rubric, adapter
):
    queued = _queued(owner_a, bm_requirements, bm_rubric)
    job = _claimed()
    unplanned = store.create_run(owner_a, queued.payload["description_id"], 1, None)

    runs.build_experiment(job, FAST)

    states = {
        r.run_id: r.state
        for r in store.runs_for_description(owner_a, queued.payload["description_id"])
    }
    assert states.pop(unplanned) == RunState.abandoned
    assert list(states.values()) == [RunState.passed]


def test_a_build_approved_hours_ago_for_a_start_still_to_come_is_not_late(
    owner_a, bm_requirements, bm_rubric, adapter
):
    start = datetime.now(timezone.utc) + timedelta(days=1)
    queued = _queued(owner_a, bm_requirements.model_copy(update={"start_at": start}), bm_rubric)
    _approved_ago(queued, hours=5)

    loop.work_once(("build",), Service())

    assert len(adapter.leases) == 1


def test_a_build_waiting_for_its_lease_ends_if_the_user_released_its_run(
    monkeypatch, owner_a, bm_requirements, bm_rubric, adapter
):
    recovered: list[object] = []
    monkeypatch.setattr(runs, "build_client", lambda settings: object())
    monkeypatch.setattr(runs, "recover", lambda *args: recovered.append(args))
    start = datetime.now(timezone.utc) + timedelta(days=2)
    queued = _queued(owner_a, bm_requirements.model_copy(update={"start_at": start}), bm_rubric)
    loop.work_once(("build",), Service())
    with audit.acting(owner_a, uuid.UUID(queued.payload["request_id"])):
        sequential.teardown(_run(owner_a, queued).run_id, adapter)
    with store.transaction() as conn:
        conn.execute("UPDATE chat.jobs SET run_after = now() WHERE job_id = %s", [queued.job_id])

    loop.work_once(("build",), Service())

    assert recovered == []
    assert [x.state for x in adapter.leases.values()] == ["TERMINATED"]
    assert store.unfinished_jobs(owner_a, queued.thread_id) == []
