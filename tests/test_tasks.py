"""What a task records on its job row."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.worker import chat, runs, tasks
from cloud_conversations.worker.app import app as celery_app

pytestmark = pytest.mark.usefixtures("logged_in")


@pytest.fixture(autouse=True)
def eager():
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    yield
    celery_app.conf.task_always_eager = False


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
    monkeypatch.setattr(chat, "exchange", lambda job, config: ran.append(str(job.job_id)))

    tasks.run_exchange.delay(str(queued.job_id))

    settled = _job(queued.job_id)
    assert ran == [str(queued.job_id)]
    assert settled.state == "done"
    assert settled.error is None
    assert settled.started_at is not None and settled.finished_at is not None
    assert store.unfinished_jobs(owner_a, queued.thread_id) == []
    store.enqueue_job(owner_a, queued.thread_id, "exchange", {"text": "again"})


def test_a_job_delivered_again_while_it_runs_is_skipped(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setattr(chat, "exchange", lambda job, config: ran.append(str(job.job_id)))
    store.start_job(queued.job_id)

    tasks.run_exchange.delay(str(queued.job_id))

    assert ran == []
    assert _job(queued.job_id).state == "running"


def test_a_body_that_raises_leaves_the_reason_on_the_row(monkeypatch, owner_a, queued):
    def explode(job, config):
        raise ValueError("no keypair called that")

    monkeypatch.setattr(chat, "exchange", explode)

    tasks.run_exchange.delay(str(queued.job_id))

    settled = _job(queued.job_id)
    assert settled.state == "error"
    assert settled.error == "ValueError: no keypair called that"
    assert settled.finished_at is not None


def test_a_failure_is_told_to_the_user_not_only_to_the_row(
    monkeypatch, owner_a, queued
):
    def explode(job, config):
        raise RuntimeError("the site went away")

    monkeypatch.setattr(chat, "exchange", explode)

    tasks.run_exchange.delay(str(queued.job_id))

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
    monkeypatch.setattr(
        runs, "build_experiment", lambda job, config: asked.append(job.kind)
    )
    job = store.enqueue_job(
        owner_a, queued.thread_id, "build", {"description_id": "x", "version": 1}
    )

    tasks.run_build.delay(str(job.job_id))

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

    tasks.run_release.delay(str(job.job_id))

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
    monkeypatch.setattr(runs.time, "monotonic", lambda: next(clock))
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

    with pytest.raises(TimeoutError, match="resume or release its run"):
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

    tasks.run_build.delay(str(job.job_id))

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


def test_a_disabled_users_queued_job_is_refused(monkeypatch, owner_a, queued):
    ran: list[str] = []
    monkeypatch.setattr(chat, "exchange", lambda job, config: ran.append(job.kind))
    store.disable_user(owner_a, "a runaway script")

    tasks.run_exchange.delay(str(queued.job_id))

    assert ran == []
    assert _job(queued.job_id).error == (
        "PermissionError: an admin disabled this account"
    )
