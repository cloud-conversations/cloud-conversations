"""What a worker records on a job's row."""

from __future__ import annotations

import signal
import threading
import time
import uuid
from types import SimpleNamespace

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.schema import service
from cloud_conversations.schema.service import Service
from cloud_conversations.worker import loop, runs, tasks

pytestmark = pytest.mark.usefixtures("logged_in")


def _work(settings: Service | None = None) -> bool:
    """Carry out one job of any kind, as a worker does."""
    return loop.work_once(("exchange", "build", "teardown"), settings or Service())


def _claim_runs_out(job_id: uuid.UUID) -> None:
    with store.transaction() as conn:
        conn.execute("UPDATE chat.jobs SET claimed_until = now() WHERE job_id = %s", [job_id])


def _job(job_id):
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT state, error, started_at, finished_at FROM chat.jobs WHERE job_id = %s",
            [job_id],
        ).fetchone()
    assert row is not None
    return SimpleNamespace(**row)


@pytest.fixture()
def queued(owner_a):
    thread = store.create_thread(owner_a, "New chat")
    return store.enqueue_job(owner_a, thread.thread_id, "exchange", {"text": "hello"})


def test_a_job_that_finishes_is_marked_done(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: ran.append(str(job.job_id)))

    assert _work()

    settled = _job(queued.job_id)
    assert ran == [str(queued.job_id)]
    assert settled.state == "done"
    assert settled.error is None
    assert settled.started_at is not None and settled.finished_at is not None
    assert store.unfinished_jobs(owner_a, queued.thread_id) == []
    store.enqueue_job(owner_a, queued.thread_id, "exchange", {"text": "again"})


def test_a_job_another_worker_holds_is_not_claimed(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: ran.append(str(job.job_id)))
    store.claim_job(["exchange"], 60)

    assert not _work()
    assert ran == []
    assert _job(queued.job_id).state == "running"


def test_a_job_whose_worker_stopped_is_carried_out_by_another(monkeypatch, owner_a, queued):
    ran: list[int] = []
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: ran.append(job.attempts))
    store.claim_job(["exchange"], 60)
    _claim_runs_out(queued.job_id)

    assert _work()
    assert ran == [2]
    assert _job(queued.job_id).state == "done"


def test_a_job_whose_worker_stopped_job_attempts_times_fails(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: ran.append(job.kind))
    for _ in range(Service().job_attempts):
        store.claim_job(["exchange"], 60)
        _claim_runs_out(queued.job_id)

    _work()

    assert ran == []
    assert _job(queued.job_id).error == "the worker stopped 3 times before this job finished"


def test_a_worker_renews_its_claim_while_the_job_runs(monkeypatch, owner_a, queued):
    taken: list[object] = []

    def slow(job, config):
        time.sleep(0.6)
        taken.append(store.claim_job(["exchange"], 0.3))

    monkeypatch.setitem(tasks.BODIES, "exchange", slow)

    _work(Service(job_claim_s=0.3))

    assert taken == [None]


def test_a_worker_whose_job_another_worker_claimed_stops(monkeypatch, owner_a, queued):
    stopped: list[int] = []
    monkeypatch.setattr(loop, "os", SimpleNamespace(_exit=stopped.append))

    def taken_over(job, config):
        with store.transaction() as conn:
            conn.execute(
                "UPDATE chat.jobs SET claim = gen_random_uuid() WHERE job_id = %s", [job.job_id]
            )
        time.sleep(0.3)

    monkeypatch.setitem(tasks.BODIES, "exchange", taken_over)

    _work(Service(job_claim_s=0.3))

    assert stopped and stopped[0] == 1


def test_a_worker_whose_job_runs_past_its_time_limit_stops(monkeypatch, owner_a, queued):
    stopped: list[int] = []
    monkeypatch.setattr(loop, "os", SimpleNamespace(_exit=stopped.append))
    monkeypatch.setattr(service, "JOB_KILL_GRACE_S", 0)
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: time.sleep(0.3))

    _work(Service(job_timeout_s=0.05, job_claim_s=0.15))

    assert stopped and stopped[0] == 1


def test_a_body_that_raises_leaves_the_reason_on_the_row(monkeypatch, owner_a, queued):
    def explode(job, config):
        raise ValueError("no keypair called that")

    monkeypatch.setitem(tasks.BODIES, "exchange", explode)

    _work()

    settled = _job(queued.job_id)
    assert settled.state == "error"
    assert settled.error == "ValueError: no keypair called that"
    assert settled.finished_at is not None


def test_a_failure_is_told_to_the_user_not_only_to_the_row(
    monkeypatch, owner_a, queued
):
    def explode(job, config):
        raise RuntimeError("the site went away")

    monkeypatch.setitem(tasks.BODIES, "exchange", explode)

    _work()

    said = [
        (m.text, m.payload) for m in store.read_messages(owner_a, queued.thread_id)
        if m.kind == "notice"
    ]
    assert said == [(
        "That did not work: RuntimeError: the site went away",
        {"job_id": str(queued.job_id)},
    )]


def test_a_build_runs_the_build_body(monkeypatch, owner_a, queued):
    asked: list[str] = []
    monkeypatch.setitem(tasks.BODIES, "build", lambda job, config: asked.append(job.kind))
    job = store.enqueue_job(
        owner_a, queued.thread_id, "build", {"description_id": "x", "version": 1}
    )

    loop.work_once(("build",), Service())

    assert asked == ["build"]
    assert _job(job.job_id).state == "done"


def _confirmed(owner: str, run_id: uuid.UUID) -> dict[str, str]:
    """A teardown job's payload, for a release of the whole run the user confirmed."""
    request_id = releases.propose(
        owner, "user", "release", "test", releases.held(run_id), run_id=run_id
    )
    releases.confirm(owner, request_id, "web")
    return {"run_id": str(run_id), "request_id": str(request_id)}


def _approved(owner: str, description_id: uuid.UUID, version: int) -> dict[str, object]:
    """A build job's payload, for a version the user approved."""
    request_id = audit.approved(owner, "web", description_id=description_id, version=version)
    return {"description_id": str(description_id), "version": version,
            "request_id": str(request_id)}


def test_a_teardown_of_a_run_holding_nothing_is_quiet(owner_a, bm_requirements):
    thread = store.create_thread(owner_a, "New chat")
    description_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=bm_requirements, rubric=[]
    )
    run_id = store.create_run(owner_a, description_id, version, None)
    job = store.enqueue_job(owner_a, thread.thread_id, "teardown", _confirmed(owner_a, run_id))

    assert _work()

    assert _job(job.job_id).state == "done"


def test_a_build_runs_in_the_chats_project(owner_a, bm_requirements, bm_rubric):
    from tests.conftest import FAST

    thread = store.create_thread(
        owner_a, "New chat", project_id="CHI-000002", ssh_key_ref="cc-keypair"
    )
    description_id, version = store.create_description(
        owner_id=owner_a,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=bm_rubric,
    )
    job = store.enqueue_job(
        owner_a, thread.thread_id, "build", _approved(owner_a, description_id, version)
    )

    runs.build_experiment(job, FAST)

    (run,) = store.runs_for_description(owner_a, description_id)
    assert run.project_id == "CHI-000002"


def test_a_builds_testbed_calls_are_recorded_under_its_approval(
    owner_a, bm_requirements, bm_rubric
):
    from tests.conftest import FAST, TEST_DB_URL

    thread = store.create_thread(owner_a, "New chat", ssh_key_ref="cc-keypair")
    description_id, version = store.create_description(
        owner_id=owner_a,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=bm_rubric,
    )
    payload = _approved(owner_a, description_id, version)
    job = store.enqueue_job(owner_a, thread.thread_id, "build", payload)

    runs.build_experiment(job, FAST)

    (run,) = store.runs_for_description(owner_a, description_id)
    with psycopg.connect(TEST_DB_URL) as conn:
        calls = conn.execute(
            "SELECT DISTINCT operation, request_id, run_id, outcome FROM audit.testbed_calls "
            "WHERE outcome <> 'started'"
        ).fetchall()
    approval = uuid.UUID(str(payload["request_id"]))
    assert set(calls) == {
        (operation, approval, run.run_id, "ok")
        for operation in ("create_lease", "launch_instance", "attach_floating_ip", "ssh_exec")
    }


def test_a_build_boots_with_the_chats_keypair_at_its_site(owner_a, bm_requirements, bm_rubric):
    from tests.conftest import FAST

    thread = store.create_thread(
        owner_a, "New chat", site_keypairs={"KVM@X": "my-kvm-key", "FAKE@BM": "cc-keypair"}
    )
    description_id, version = store.create_description(
        owner_id=owner_a,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=bm_rubric,
    )
    job = store.enqueue_job(
        owner_a, thread.thread_id, "build", _approved(owner_a, description_id, version)
    )

    runs.build_experiment(job, FAST)

    (run,) = store.runs_for_description(owner_a, description_id)
    assert run.plan is not None and run.plan.keypair() == "cc-keypair"


def test_a_build_past_its_time_limit_stops_before_recovery(
    monkeypatch, owner_a, bm_requirements, bm_rubric
):
    from tests.conftest import FAST

    thread = store.create_thread(owner_a, "New chat", ssh_key_ref="cc-keypair")
    unbootable = bm_requirements.model_copy(update={"site": "FAKE@BM", "items": [
        i.model_copy(update={"value": "CC-Nonesuch99.04"}) if i.key == "image_name" else i
        for i in bm_requirements.items
    ]})
    description_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=unbootable, rubric=bm_rubric,
    )
    job = store.enqueue_job(
        owner_a, thread.thread_id, "build", _approved(owner_a, description_id, version)
    )
    clock = iter([0.0, FAST.service.job_timeout_s])
    monkeypatch.setattr(runs, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    recovered: list[uuid.UUID] = []
    monkeypatch.setattr(runs, "recover", lambda client, owner, run_id, *a: recovered.append(run_id))

    with pytest.raises(TimeoutError, match="the build ran past job_timeout_s"):
        runs.build_experiment(job, FAST)
    assert recovered == []


def test_a_build_stops_an_install_before_its_time_limit(
    monkeypatch, owner_a, bm_requirements, bm_rubric
):
    from cloud_conversations.adapter.fake import FakeAdapter
    from cloud_conversations.adapter.protocol import ExecResult
    from cloud_conversations.environment.install import InstallSeconds
    from cloud_conversations.schema.description import Requirement
    from cloud_conversations.validator.rubric import Check
    from tests.conftest import FAST

    # The job may run two seconds past one SSH call, and an install may take five.
    config = FAST.model_copy(update={
        "service": FAST.service.model_copy(
            update={"job_timeout_s": FAST.defaults.install_call_s + 2}
        ),
        "defaults": FAST.defaults.model_copy(
            update={"install_s": InstallSeconds(system_package=5)}
        ),
    })
    real = FakeAdapter.ssh_exec

    def never_done(self, site, host, user, command, timeout_s):
        if 'echo "${status:-missing}"' in command:
            return ExecResult(stdout="running", stderr="", exit_code=0)
        return real(self, site, host, user, command, timeout_s)

    monkeypatch.setattr(FakeAdapter, "ssh_exec", never_done)
    thread = store.create_thread(owner_a, "New chat", ssh_key_ref="cc-keypair")
    jq = Requirement(key="system_package", value="jq", source="stated")
    check = Check(id="jq", kind="package_installed", target="jq", expected=True,
                  comparator="eq", from_requirement="system_package")
    requirements = bm_requirements.model_copy(
        update={"site": "FAKE@BM", "items": [*bm_requirements.items, jq]}
    )
    description_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=requirements,
        rubric=[*bm_rubric, check],
    )
    job = store.enqueue_job(
        owner_a, thread.thread_id, "build", _approved(owner_a, description_id, version)
    )

    with pytest.raises(TimeoutError, match="Release its run, or approve the version again"):
        runs.build_experiment(job, config)

    (run,) = store.runs_for_description(owner_a, description_id)
    assert run.failure is not None
    assert run.failure.message.startswith("the build ran out of time with the install still")


def test_a_build_with_no_keypair_at_its_site_fails_naming_the_site(owner_a, bm_requirements):
    thread = store.create_thread(owner_a, "New chat", site_keypairs={"FAKE@KVM": "my-kvm-key"})
    description_id, version = store.create_description(
        owner_id=owner_a,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=[],
    )
    job = store.enqueue_job(
        owner_a, thread.thread_id, "build", _approved(owner_a, description_id, version)
    )

    _work()

    assert _job(job.job_id).error == (
        "ValueError: the chat has no keypair at FAKE@BM"
    )


def test_a_teardown_reaches_the_project_the_run_was_built_in(
    monkeypatch, owner_a, bm_requirements
):
    from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
    from tests.conftest import FAST

    thread = store.create_thread(owner_a, "New chat", project_id="CHI-000001")
    description_id, version = store.create_description(
        owner_id=owner_a,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=[],
    )
    run_id = store.create_run(owner_a, description_id, version, "CHI-000002")
    store.record_resource(run_id, "lease-1", "lease", "FAKE@BM", None)
    reached: list = []

    def build_adapter(site, config, user=None):
        reached.append(user)
        return FakeAdapter(sites=default_fake_sites(), config=config)

    monkeypatch.setattr(runs.credentials, "build_adapter", build_adapter)
    job = store.enqueue_job(owner_a, thread.thread_id, "teardown", _confirmed(owner_a, run_id))

    with pytest.raises(ValueError, match="lease-1"):
        runs.release(job, FAST)

    assert [u.project for u in reached] == ["CHI-000002"]


def test_a_teardown_of_a_run_holding_only_an_unrecorded_lease_names_it(
    monkeypatch, owner_a, bm_requirements, interrupted
):
    from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
    from tests.conftest import FAST

    thread = store.create_thread(owner_a, "New chat")
    description_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=bm_requirements, rubric=[]
    )
    run_id = store.create_run(owner_a, description_id, version, None)
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    interrupted(lambda: adapter.create_lease(
        "FAKE@BM", duration_hours=1.0, node_type="compute_skylake",
        run_id=run_id, idempotency_key="k1",
    ))
    (made,) = adapter.leases
    monkeypatch.setattr(runs.credentials, "build_adapter", lambda *args, **kwargs: adapter)
    job = store.enqueue_job(owner_a, thread.thread_id, "teardown", _confirmed(owner_a, run_id))

    with pytest.raises(store.ConflictError, match=f"lease {made}"):
        runs.release(job, FAST)


def test_a_disabled_users_queued_job_is_refused(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: ran.append(job.kind))
    store.disable_user(owner_a, "a runaway script")

    _work()

    assert ran == []
    assert _job(queued.job_id).error == (
        "PermissionError: an admin disabled this account"
    )


def test_an_idle_worker_watches_the_leases_until_it_is_told_to_stop(monkeypatch):
    from cloud_conversations.config import Config

    watched: list[None] = []
    stop: list = []
    monkeypatch.setattr(loop.leases, "watch", lambda config: watched.append(None))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: stop.append(handler))
    settings = Config(service=Service(lease_watch_s=0.1, job_poll_s=0.02))
    threading.Timer(0.35, lambda: stop[0](signal.SIGTERM, None)).start()

    loop.run(("exchange",), settings)

    assert 3 <= len(watched) <= 5


def test_a_worker_told_to_stop_hands_its_job_to_another_at_once(monkeypatch, owner_a, queued):
    stopped: list[int] = []
    monkeypatch.setattr(loop, "os", SimpleNamespace(_exit=stopped.append))
    taken: list[object] = []

    def told_to_stop(job, config):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        taken.append(store.claim_job(["exchange"], 60))

    monkeypatch.setitem(tasks.BODIES, "exchange", told_to_stop)

    _work()

    assert stopped == [0]
    (job,) = taken
    assert isinstance(job, store.Job) and job.attempts == 1


def test_a_worker_answers_sigterm_as_before_once_its_job_is_done(monkeypatch, owner_a, queued):
    monkeypatch.setitem(tasks.BODIES, "exchange", lambda job, config: None)
    before = signal.getsignal(signal.SIGTERM)

    _work()

    assert signal.getsignal(signal.SIGTERM) is before


def test_a_paused_worker_starts_no_build_or_release_but_answers_the_chat(
    monkeypatch, owner_a, queued
):
    ran: list[str] = []
    for kind in ("exchange", "build", "teardown"):
        monkeypatch.setitem(tasks.BODIES, kind, lambda job, config: ran.append(job.kind))
    store.enqueue_job(owner_a, queued.thread_id, "build", {})
    store.enqueue_job(owner_a, queued.thread_id, "teardown", {})
    store.set_paused(True)

    while _work():
        pass

    assert ran == ["exchange"]
