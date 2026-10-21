"""The HTTP surface: what it returns, what it queues, and what it refuses."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

import cloud_conversations.db.store as store
from cloud_conversations import releases
from cloud_conversations.api.deps import config, session_hash
from cloud_conversations.schema.runs import Connection, Plan, PlanStep, RunState
from tests.conftest import ALICE, BOB, TEST_DB_URL

pytestmark = pytest.mark.usefixtures("logged_in")


def _thread(client) -> str:
    return client.post("/threads", headers=ALICE).json()["thread_id"]


def test_a_new_chat_is_named_by_its_first_message(client, dispatched):
    thread_id = _thread(client)
    assert client.get(f"/threads/{thread_id}", headers=ALICE).json()["thread"][
        "title"
    ] == "New experiment"

    client.post(
        f"/threads/{thread_id}/messages",
        json={"text": "how do I reserve a node with a GPU?"},
        headers=ALICE,
    )

    body = client.get(f"/threads/{thread_id}", headers=ALICE).json()
    assert body["thread"]["title"] == "how do I reserve a node with a GPU?"


def test_a_long_first_message_is_cut_to_a_title(client, dispatched):
    thread_id = _thread(client)
    said = "I need an A100 for a day to fine-tune a model on the ImageNet data I hold"

    client.post(f"/threads/{thread_id}/messages", json={"text": said}, headers=ALICE)

    title = client.get(f"/threads/{thread_id}", headers=ALICE).json()["thread"]["title"]
    assert title == said[: config().service.thread_title_chars]
    assert len(title) < len(said)


def test_posting_a_message_queues_one_exchange(client, dispatched):
    thread_id = _thread(client)

    posted = client.post(
        f"/threads/{thread_id}/messages", json={"text": "hello"}, headers=ALICE
    )

    assert posted.status_code == 202
    body = posted.json()
    assert body["message"]["kind"] == "user"
    assert dispatched == [("exchange", body["job"]["job_id"])]
    in_flight = client.get(f"/threads/{thread_id}", headers=ALICE).json()["jobs"]
    assert [j["kind"] for j in in_flight] == ["exchange"]


def test_a_second_message_is_refused_while_the_first_is_unfinished(client, dispatched):
    thread_id = _thread(client)
    client.post(f"/threads/{thread_id}/messages", json={"text": "one"}, headers=ALICE)

    again = client.post(
        f"/threads/{thread_id}/messages", json={"text": "two"}, headers=ALICE
    )

    assert (again.status_code, again.json()) == (
        409, {"detail": f"Thread {thread_id} already has an unfinished exchange job"}
    )
    assert len(dispatched) == 1
    messages = client.get(f"/threads/{thread_id}", headers=ALICE).json()["messages"]
    assert [m["text"] for m in messages] == ["one"]


def test_another_owners_chat_is_not_there(client, dispatched):
    thread_id = _thread(client)

    assert client.get(f"/threads/{thread_id}", headers=BOB).status_code == 404
    assert client.get("/threads", headers=BOB).json() == []
    assert len(client.get("/threads", headers=ALICE).json()) == 1


def test_a_caller_without_a_session_is_told_to_log_in(client, dispatched):
    refused = client.get("/threads", headers={"Cookie": "cc_session=made-up"})

    assert refused.status_code == 401
    assert refused.json() == {"detail": "log in first"}


def test_a_change_sent_from_another_site_is_refused(client, dispatched):
    refused = client.post(
        "/threads", headers={**ALICE, "Origin": "https://evil.example.org"}
    )

    assert refused.status_code == 403
    assert refused.json() == {"detail": "this request came from another site"}
    assert store.list_threads("chameleon:alice") == []


def test_a_change_with_no_origin_is_refused(client, dispatched):
    refused = client.post("/threads", headers={"Cookie": ALICE["Cookie"]})

    assert refused.status_code == 403


def test_a_new_chat_builds_in_the_callers_first_project(client, dispatched):
    created = client.post("/threads", headers=ALICE).json()

    assert created["project_id"] == "CHI-000001"


def test_a_chat_moves_only_to_a_project_the_caller_is_in(client, dispatched):
    thread_id = _thread(client)

    refused = client.patch(
        f"/threads/{thread_id}", json={"project_id": "CHI-999999"}, headers=ALICE
    )

    assert refused.status_code == 422
    assert refused.json() == {"detail": "you are not in Chameleon project CHI-999999"}
    assert store.get_thread("chameleon:alice", uuid.UUID(thread_id)).project_id == "CHI-000001"


def test_a_patch_that_names_nothing_is_refused(client, dispatched):
    refused = client.patch(f"/threads/{_thread(client)}", json={}, headers=ALICE)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "name a keypair or a project"}


def test_an_expired_session_is_told_to_log_in(client, dispatched):
    store.record_login(
        "chameleon:carol",
        "carol",
        [],
        "encrypted-refresh-token",
        session_hash("carol-session"),
        datetime.now(timezone.utc) - timedelta(seconds=1),
        email=None,
    )

    refused = client.get("/threads", headers={"Cookie": "cc_session=carol-session"})

    assert refused.status_code == 401


def test_a_chat_takes_one_keypair_at_each_site(client):
    thread_id = _thread(client)

    refused = client.patch(
        f"/threads/{thread_id}", json={"site_keypairs": {"CHI@UC": "my-uc-key"}}, headers=ALICE
    )

    sites = ", ".join(config().sites_at("chameleon"))
    assert refused.status_code == 422
    assert refused.json() == {"detail": f"pick one keypair at each of {sites}"}


def test_keypairs_at_each_site_and_an_imported_key_replace_each_other(client):
    thread_id = _thread(client)
    at_sites = {site: f"key-at-{site}" for site in config().sites_at("chameleon")}
    client.patch(f"/threads/{thread_id}", json={"ssh_key_ref": "cc-laptop-a1b2c3"}, headers=ALICE)

    by_site = client.patch(
        f"/threads/{thread_id}", json={"site_keypairs": at_sites}, headers=ALICE
    ).json()
    imported = client.patch(
        f"/threads/{thread_id}", json={"ssh_key_ref": "cc-laptop-a1b2c3"}, headers=ALICE
    ).json()

    assert (by_site["ssh_key_ref"], by_site["site_keypairs"]) == (None, at_sites)
    assert (imported["ssh_key_ref"], imported["site_keypairs"]) == ("cc-laptop-a1b2c3", None)


def test_an_imported_key_belongs_to_the_caller_and_names_its_keypair(client):
    import paramiko

    key = paramiko.RSAKey.generate(2048)

    imported = client.post(
        "/keys",
        json={"name": "laptop", "public_key": f"ssh-rsa {key.get_base64()} me@laptop"},
        headers=ALICE,
    )

    (stored,) = store.user_keys("chameleon:alice")
    assert imported.status_code == 201
    assert imported.json() == {
        "name": "laptop",
        "keypair_name": stored.keypair_name,
        "fingerprint": key.fingerprint,
    }
    assert store.user_keys("chameleon:bob") == []


def test_importing_what_is_not_a_public_key_is_refused_with_the_reason(client):
    refused = client.post(
        "/keys", json={"name": "laptop", "public_key": "hello"}, headers=ALICE
    )

    assert refused.status_code == 422
    assert refused.json() == {"detail": "not an OpenSSH public key"}


def _description(owner: str, bm_requirements, bm_rubric) -> tuple[uuid.UUID, int]:
    return store.create_description(
        owner_id=owner,
        conversation_id="test",
        requirements=bm_requirements.model_copy(update={"site": "FAKE@BM"}),
        rubric=bm_rubric,
        open_questions=["assumed Ubuntu 24.04"],
    )


def test_a_thread_carries_every_version_of_its_description(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    description_id, _ = _description(owner_a, bm_requirements, bm_rubric)
    store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=bm_requirements,
        rubric=bm_rubric, description_id=description_id, parent_version=1,
        revision_reason="make it 4 hours",
    )
    store.update_thread(owner_a, uuid.UUID(thread_id), description_id=description_id)

    versions = client.get(f"/threads/{thread_id}", headers=ALICE).json()["versions"]

    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["open_questions"] == ["assumed Ubuntu 24.04"]
    assert versions[0]["rubric"][0]["kind"] == "cpu_arch"


def test_approving_a_version_queues_a_build_and_says_so_on_the_thread(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    store.update_thread(owner_a, uuid.UUID(thread_id), ssh_key_ref="cc-keypair")
    description_id, version = _description(owner_a, bm_requirements, bm_rubric)

    approved = client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(description_id), "version": version},
        headers=ALICE,
    )

    assert approved.status_code == 202
    assert dispatched == [("build", approved.json()["job_id"])]
    messages = client.get(f"/threads/{thread_id}", headers=ALICE).json()["messages"]
    assert messages[-1]["kind"] == "run"
    assert messages[-1]["description_version"] == version
    assert "FAKE@BM" in messages[-1]["text"]


def test_approving_a_version_is_the_request_its_build_runs_under(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    store.update_thread(owner_a, uuid.UUID(thread_id), ssh_key_ref="cc-keypair")
    description_id, version = _description(owner_a, bm_requirements, bm_rubric)

    client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(description_id), "version": version},
        headers=ALICE,
    )

    (job,) = store.unfinished_jobs(owner_a, uuid.UUID(thread_id))
    (approval,) = store.decision_request(owner_a, uuid.UUID(job.payload["request_id"]))
    assert (approval.step, approval.actor, approval.via, approval.action) == (
        "confirmed", "user", "web", "build"
    )
    assert (approval.description_id, approval.version) == (description_id, version)


def test_a_second_build_is_refused_while_one_is_live(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    store.update_thread(owner_a, uuid.UUID(thread_id), ssh_key_ref="cc-keypair")
    description_id, version = _description(owner_a, bm_requirements, bm_rubric)
    body = {"description_id": str(description_id), "version": version}
    client.post(f"/threads/{thread_id}/builds", json=body, headers=ALICE)

    again = client.post(f"/threads/{thread_id}/builds", json=body, headers=ALICE)

    assert (again.status_code, again.json()) == (
        409, {"detail": f"Thread {thread_id} already has an unfinished build job"}
    )
    assert len(dispatched) == 1


def test_approving_without_a_keypair_at_the_site_asks_for_one(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    description_id, version = _description(owner_a, bm_requirements, bm_rubric)

    refused = client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(description_id), "version": version},
        headers=ALICE,
    )

    assert refused.status_code == 409
    assert refused.json() == {"detail": "pick the keypair to log in to FAKE@BM with, then approve"}
    assert dispatched == []
    messages = client.get(f"/threads/{thread_id}", headers=ALICE).json()["messages"]
    (notice,) = [m for m in messages if m["kind"] == "notice"]
    assert (notice["payload"], notice["description_id"], notice["description_version"]) == (
        {"needs": "ssh_key_ref"}, str(description_id), version
    )


def test_approving_a_lease_that_starts_too_late_to_finish_is_refused(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    store.update_thread(owner_a, uuid.UUID(thread_id), ssh_key_ref="cc-keypair")
    settings = config().for_site("FAKE@BM", "physical_host")
    budget_s = config().service.job_timeout_s - (
        settings.lease_active_s + settings.instance_active_s + settings.ssh_s
    )
    start = (datetime.now(timezone.utc) + timedelta(seconds=budget_s + 600)).replace(
        microsecond=0
    )
    description_id, version = _description(
        owner_a, bm_requirements.model_copy(update={"start_at": start}), bm_rubric
    )

    refused = client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(description_id), "version": version},
        headers=ALICE,
    )

    assert refused.status_code == 422
    assert refused.json() == {"detail": (
        f"the lease starts {start:%Y-%m-%d %H:%M} UTC, too late for a build to finish; "
        f"ask for a start within {budget_s / 3600:.1f} hours of now"
    )}
    assert dispatched == []

def test_a_job_running_past_the_kill_limit_is_failed_on_the_next_request(client, owner_a):
    from cloud_conversations.api.deps import STALE_JOB

    thread_id = uuid.UUID(_thread(client))
    job = store.enqueue_job(owner_a, thread_id, "exchange", {"text": "hello"})
    store.start_job(job.job_id)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE chat.jobs SET started_at = now() - make_interval(secs => %s) "
            "WHERE job_id = %s",
            [config().service.job_kill_s + 1, job.job_id],
        )

    client.get("/me", headers=ALICE)

    assert store.unfinished_jobs(owner_a, thread_id) == []
    notices = [
        (m.text, m.payload) for m in store.read_messages(owner_a, thread_id) if m.kind == "notice"
    ]
    assert notices == [(f"That did not work: {STALE_JOB}", {"job_id": str(job.job_id)})]


def test_approving_a_description_with_no_site_is_refused(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    thread_id = _thread(client)
    description_id, version = store.create_description(
        owner_id=owner_a, conversation_id="test", requirements=bm_requirements,
        rubric=bm_rubric,
    )

    approved = client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(description_id), "version": version},
        headers=ALICE,
    )

    assert approved.status_code == 422
    assert "names no site" in approved.json()["detail"]
    assert dispatched == []


def test_approving_a_version_that_is_not_there_is_a_404(client, dispatched):
    thread_id = _thread(client)

    approved = client.post(
        f"/threads/{thread_id}/builds",
        json={"description_id": str(uuid.uuid4()), "version": 1},
        headers=ALICE,
    )

    assert approved.status_code == 404
    assert dispatched == []


@pytest.fixture()
def run(owner_a, bm_requirements, bm_rubric):
    description_id, version = _description(owner_a, bm_requirements, bm_rubric)
    run_id = store.create_run(owner_a, description_id, version, None)
    thread_id = store.create_thread(owner_a, "New chat").thread_id
    store.update_thread(owner_a, thread_id, description_id=description_id)
    store.append_message(
        thread_id, "user", "run", f"Approved version {version}.",
        owner_id=owner_a, description_id=description_id,
        description_version=version,
    )
    return run_id


def test_a_run_reports_its_state_its_endpoint_and_what_it_holds(client, run, owner_a):
    store.update_run(
        owner_a, run,
        plan=Plan(plan_id="p", site="FAKE@BM", steps=[PlanStep(step_id="s", action="reserve")]),
        connection=Connection(host="10.0.0.1", user="cc", port=22),
    )
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)

    body = client.get(f"/runs/{run}", headers=ALICE).json()

    assert body["state"] == RunState.planning.value
    assert body["connection"]["host"] == "10.0.0.1"
    assert body["live_resources"] == [
        {"kind": "lease", "provider_id": "lease-1", "site": "FAKE@BM"}
    ]


def test_a_runs_events_are_the_step_timeline(client, run, owner_a):
    store.append_event(run, "runner", "run_started", {}, owner_id=owner_a)
    store.append_event(
        run, "runner", "step_succeeded", {"step_id": "step-0001", "action": "reserve"},
        owner_id=owner_a,
    )

    events = client.get(f"/runs/{run}/events", headers=ALICE).json()

    assert [e["kind"] for e in events] == ["run_started", "step_succeeded"]
    assert events[1]["payload"]["step_id"] == "step-0001"


_LEASE = {"kind": "lease", "provider_id": "lease-1", "site": "FAKE@BM"}
_INSTANCE = {"kind": "instance", "provider_id": "instance-1", "site": "FAKE@BM"}


def _audit(owner: str) -> list[tuple[str, str, list[dict]]]:
    with psycopg.connect(TEST_DB_URL) as conn:
        rows = conn.execute(
            "SELECT step, actor, resources FROM audit.decisions WHERE owner_id = %s ORDER BY seq",
            [owner],
        ).fetchall()
    return [(step, actor, resources) for step, actor, resources in rows]


def test_releasing_resources_queues_a_teardown_and_records_the_confirmation(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)

    queued = client.post(
        f"/runs/{run}/teardown", json={"resources": [_LEASE]}, headers=ALICE
    )

    assert queued.status_code == 202
    assert queued.json()["kind"] == "teardown"
    assert dispatched == [("teardown", queued.json()["job_id"])]
    assert _audit(owner_a) == [("proposed", "user", [_LEASE]), ("confirmed", "user", [_LEASE])]


def test_a_release_that_leaves_out_a_resource_the_run_holds_is_refused(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    store.record_resource(run, "instance-1", "instance", "FAKE@BM", None)

    refused = client.post(
        f"/runs/{run}/teardown", json={"resources": [_LEASE]}, headers=ALICE
    )

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "the run holds lease lease-1 at FAKE@BM, instance instance-1 at FAKE@BM. "
        "Review the run and release it again"
    )
    assert dispatched == []
    assert _audit(owner_a) == []


def test_a_release_that_names_a_resource_the_run_does_not_hold_is_refused(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)

    refused = client.post(
        f"/runs/{run}/teardown", json={"resources": [_LEASE, _INSTANCE]},
        headers=ALICE,
    )

    assert refused.status_code == 409
    assert dispatched == []
    assert _audit(owner_a) == []


def test_a_release_of_a_run_that_holds_nothing_is_refused(client, dispatched, run, owner_a):
    refused = client.post(
        f"/runs/{run}/teardown", json={"resources": [_LEASE]}, headers=ALICE
    )

    assert refused.status_code == 409
    assert refused.json()["detail"] == f"run {run} holds nothing to release"
    assert _audit(owner_a) == []


def _offer(owner: str, run_id: uuid.UUID, version: int | None = None, **detail) -> uuid.UUID:
    """Fail the run, and propose releasing it as the agent."""
    if store.get_run(owner, run_id).state != RunState.failed:
        store.update_run(owner, run_id, state=RunState.failed)
    return releases.offer(
        owner, store.get_run(owner, run_id), "nothing else will help",
        {"action": "abandon", **detail}, version,
    )


def test_the_agents_proposal_is_put_to_the_user_in_the_chat(client, dispatched, run, owner_a):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)

    request_id = _offer(owner_a, run)

    thread_id = store.list_threads(owner_a)[0].thread_id
    body = client.get(f"/threads/{thread_id}", headers=ALICE).json()
    asked = body["messages"][-1]
    assert asked["kind"] == "release"
    assert asked["text"] == (
        "The experiment agent wants to release lease lease-1 at FAKE@BM. "
        "It says: nothing else will help"
    )
    assert asked["payload"]["request_id"] == str(request_id)
    assert body["decisions"] == {}
    assert dispatched == [], "nothing is released before the user answers"


def test_confirming_the_agents_proposal_queues_its_release(client, dispatched, run, owner_a):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    request_id = _offer(owner_a, run)

    queued = client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    assert queued.status_code == 202
    assert dispatched == [("teardown", queued.json()["job_id"])]
    assert _audit(owner_a) == [("proposed", "agent", [_LEASE]), ("confirmed", "user", [_LEASE])]
    thread_id = store.list_threads(owner_a)[0].thread_id
    decisions = client.get(f"/threads/{thread_id}", headers=ALICE).json()["decisions"]
    assert decisions == {str(request_id): "confirmed"}


def test_declining_the_agents_proposal_keeps_the_run(client, dispatched, run, owner_a):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    request_id = _offer(owner_a, run)

    assert client.post(f"/releases/{request_id}/decline", headers=ALICE).status_code == 204

    assert dispatched == []
    assert _audit(owner_a)[-1] == ("declined", "user", [_LEASE])
    assert [r.provider_id for r in store.live_resources(run)] == ["lease-1"]


def test_an_answered_proposal_cannot_be_answered_again(client, dispatched, run, owner_a):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    request_id = _offer(owner_a, run)
    client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    again = client.post(f"/releases/{request_id}/decline", headers=ALICE)

    assert again.status_code == 409
    assert [step for step, _, _ in _audit(owner_a)] == ["proposed", "confirmed"]


def test_a_proposal_is_refused_once_its_run_is_no_longer_failed(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    request_id = _offer(owner_a, run)
    store.update_run(owner_a, run, state=RunState.provisioning)

    refused = client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "the run has changed since the experiment agent asked. "
        "Review the run and release it from its card"
    )
    assert dispatched == []


def test_an_older_proposal_is_refused_once_the_agent_has_made_another(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    older = _offer(owner_a, run)
    _offer(owner_a, run)

    assert client.post(f"/releases/{older}/confirm", headers=ALICE).status_code == 409
    assert dispatched == []


def test_a_proposal_is_refused_once_the_run_holds_more_than_it_names(
    client, dispatched, run, owner_a
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    request_id = _offer(owner_a, run)
    store.record_resource(run, "instance-1", "instance", "FAKE@BM", None)

    refused = client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    assert refused.status_code == 409
    assert dispatched == []


def test_confirming_a_revision_queues_a_build_that_replaces_the_run(
    client, dispatched, run, owner_a, bm_requirements, bm_rubric
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    thread_id = store.list_threads(owner_a)[0].thread_id
    store.update_thread(owner_a, thread_id, ssh_key_ref="cc-keypair")
    description_id = store.get_run(owner_a, run).description_id
    store.create_description(
        owner_a, "test", bm_requirements.model_copy(update={"site": "FAKE@BM"}), bm_rubric,
        description_id=description_id, parent_version=1,
    )
    request_id = _offer(owner_a, run, action="revise", version=2, change="use Ubuntu 22.04")

    queued = client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    assert queued.status_code == 202
    assert dispatched == [("build", queued.json()["job_id"])]
    (job,) = store.unfinished_jobs(owner_a, thread_id)
    assert job.payload == {
        "description_id": str(description_id), "version": 2, "replaces_run_id": str(run),
        "request_id": str(request_id), "change": "use Ubuntu 22.04",
    }
    assert _audit(owner_a)[-1] == ("confirmed", "user", [_LEASE])


def test_a_revision_of_a_run_already_released_counts_against_the_limit(
    client, dispatched, run, owner_a, bm_requirements, bm_rubric
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    thread_id = store.list_threads(owner_a)[0].thread_id
    store.update_thread(owner_a, thread_id, ssh_key_ref="cc-keypair")
    description_id = store.get_run(owner_a, run).description_id
    store.create_description(
        owner_a, "test", bm_requirements.model_copy(update={"site": "FAKE@BM"}), bm_rubric,
        description_id=description_id, parent_version=1,
    )
    request_id = _offer(owner_a, run, action="revise", version=2, change="use Ubuntu 22.04")
    store.release_resource_with_event(run, "lease", "lease-1", "FAKE@BM")
    other = store.create_run(owner_a, description_id, 1, None)
    store.record_resource(other, "lease-2", "lease", "FAKE@BM", None)
    store.update_user(owner_a, max_active_experiments_per_user=1)

    refused = client.post(f"/releases/{request_id}/confirm", headers=ALICE)

    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "you have 1 active experiments, the most allowed; release one from your profile"
    )
    assert dispatched == []


def test_declining_a_revision_offers_its_version_for_approval(
    client, dispatched, run, owner_a, bm_requirements, bm_rubric
):
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)
    description_id = store.get_run(owner_a, run).description_id
    store.create_description(
        owner_a, "test", bm_requirements.model_copy(update={"site": "FAKE@BM"}), bm_rubric,
        description_id=description_id, parent_version=1,
    )
    request_id = _offer(owner_a, run, action="revise", version=2, change="use Ubuntu 22.04")

    client.post(f"/releases/{request_id}/decline", headers=ALICE)

    thread_id = store.list_threads(owner_a)[0].thread_id
    last = store.read_messages(owner_a, thread_id)[-1]
    assert (last.kind, last.description_version) == ("description", 2)
    assert dispatched == []


def test_the_thread_lists_the_runs_of_its_description(client, dispatched, run, owner_a):
    thread_id = store.list_threads(owner_a)[0].thread_id

    runs = client.get(f"/threads/{thread_id}", headers=ALICE).json()["runs"]

    assert [r["run_id"] for r in runs] == [str(run)]
    assert runs[0]["description_version"] == 1


def test_a_thread_carries_every_experiment_it_has_worked_on(
    client, dispatched, run, owner_a, bm_requirements, bm_rubric
):
    thread_id = store.list_threads(owner_a)[0].thread_id
    second, version = _description(owner_a, bm_requirements, bm_rubric)
    store.append_message(
        thread_id, "assistant", "description", "a second experiment",
        owner_id=owner_a, description_id=second, description_version=version,
    )
    store.update_thread(owner_a, thread_id, description_id=second)

    body = client.get(f"/threads/{thread_id}", headers=ALICE).json()

    assert [v["description_id"] for v in body["versions"]] == [
        str(store.get_run(owner_a, run).description_id),
        str(second),
    ]
    assert [r["run_id"] for r in body["runs"]] == [str(run)]


def test_a_run_of_an_earlier_experiment_can_still_be_released(
    client, dispatched, run, owner_a, bm_requirements, bm_rubric
):
    thread_id = store.list_threads(owner_a)[0].thread_id
    second, _ = _description(owner_a, bm_requirements, bm_rubric)
    store.update_thread(owner_a, thread_id, description_id=second)
    store.record_resource(run, "lease-1", "lease", "FAKE@BM", None)

    queued = client.post(
        f"/runs/{run}/teardown", json={"resources": [_LEASE]}, headers=ALICE
    )

    assert queued.status_code == 202
    assert queued.json()["run_id"] == str(run)


def test_a_thread_carries_what_one_approval_permits(client, dispatched):
    thread_id = _thread(client)

    limits = client.get(f"/threads/{thread_id}", headers=ALICE).json()["limits"]

    settings = config().for_site(None)
    assert limits == {
        "max_build_rounds": settings.max_build_rounds,
        "max_leases_per_build": settings.max_leases_per_build,
    }


def test_healthz_says_nothing_about_the_caller(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_a_deleted_chat_moves_from_the_sidebar_to_the_trash(client, dispatched):
    thread_id = _thread(client)
    client.post(f"/threads/{thread_id}/messages", json={"text": "hello"}, headers=ALICE)
    store.finish_job(uuid.UUID(dispatched[0][1]))

    assert client.delete(f"/threads/{thread_id}", headers=ALICE).status_code == 204

    assert client.get(f"/threads/{thread_id}", headers=ALICE).status_code == 404
    assert client.get("/threads", headers=ALICE).json() == []
    (trashed,) = client.get("/trash", headers=ALICE).json()
    assert (trashed["thread_id"], trashed["title"]) == (thread_id, "hello")
    deleted = datetime.fromisoformat(trashed["deleted_at"])
    restorable = datetime.fromisoformat(trashed["restorable_until"]) - deleted
    assert restorable == timedelta(days=config().service.restore_days)


def _deleted_at(table: str, column: str, value) -> list:
    with psycopg.connect(TEST_DB_URL) as conn:
        return [
            r[0] for r in conn.execute(
                f"SELECT deleted_at FROM {table} WHERE {column} = %s", [value]
            ).fetchall()
        ]


def _chat_with_a_run(owner: str, requirements, rubric) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Returns the IDs of the chat, its description, and its run."""
    description_id, version = _description(owner, requirements, rubric)
    run_id = store.create_run(owner, description_id, version, None)
    thread_id = store.create_thread(owner, "New chat").thread_id
    store.create_conversation(owner, "describe it", [], description_id, version)
    routing = store.create_conversation(owner, "route it", [])
    store.append_message(
        thread_id, "assistant", "description", "a description",
        {"routing_conversation_id": str(routing)},
        owner_id=owner, description_id=description_id, description_version=version,
    )
    return thread_id, description_id, run_id


def test_deleting_a_chat_stamps_what_it_made_with_one_time(
    client, owner_a, bm_requirements, bm_rubric
):
    thread_id, description_id, run_id = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)

    client.delete(f"/threads/{thread_id}", headers=ALICE)

    stamps = [
        *_deleted_at("chat.threads", "thread_id", thread_id),
        *_deleted_at("experiments.experiment_descriptions", "description_id", description_id),
        *_deleted_at("experiments.runs", "run_id", run_id),
        *_deleted_at("chat.conversations", "owner_id", owner_a),
    ]
    assert len(stamps) == 5
    assert stamps[0] is not None
    assert set(stamps) == {stamps[0]}
    assert client.get(f"/runs/{run_id}", headers=ALICE).status_code == 404


def test_a_chat_whose_run_holds_hardware_is_not_deleted(
    client, owner_a, bm_requirements, bm_rubric
):
    thread_id, _, run_id = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    store.record_resource(run_id, "lease-1", "lease", "FAKE@BM", None)

    refused = client.delete(f"/threads/{thread_id}", headers=ALICE)

    assert refused.status_code == 409
    assert refused.json() == {
        "detail": f"run {run_id} still holds hardware; release it before deleting the chat"
    }
    assert _deleted_at("experiments.runs", "run_id", run_id) == [None]


def test_restoring_a_chat_brings_back_what_its_deletion_stamped_and_nothing_else(
    client, owner_a, bm_requirements, bm_rubric
):
    thread_id, _, run_id = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    other_id, _, other_run = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    client.delete(f"/threads/{thread_id}", headers=ALICE)
    client.delete(f"/threads/{other_id}", headers=ALICE)

    restored = client.post(f"/trash/{thread_id}/restore", headers=ALICE)

    assert restored.status_code == 200
    assert client.get(f"/runs/{run_id}", headers=ALICE).status_code == 200
    assert client.get(f"/runs/{other_run}", headers=ALICE).status_code == 404
    assert [t["thread_id"] for t in client.get("/trash", headers=ALICE).json()] == [str(other_id)]


def test_a_description_in_the_trash_cannot_be_built_from_another_chat(
    client, dispatched, owner_a, bm_requirements, bm_rubric
):
    trashed, description_id, _ = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    client.delete(f"/threads/{trashed}", headers=ALICE)

    refused = client.post(
        f"/threads/{_thread(client)}/builds",
        json={"description_id": str(description_id), "version": 1},
        headers=ALICE,
    )

    assert refused.status_code == 404
    assert dispatched == []


def test_a_chat_out_of_the_trash_cannot_be_restored(client, owner_a, bm_requirements, bm_rubric):
    thread_id, _, _ = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    client.delete(f"/threads/{thread_id}", headers=ALICE)
    with psycopg.connect(TEST_DB_URL) as conn:
        conn.execute(
            "UPDATE chat.threads SET deleted_at = now() - interval '31 days' "
            "WHERE thread_id = %s",
            [thread_id],
        )

    assert client.get("/trash", headers=ALICE).json() == []
    assert client.post(f"/trash/{thread_id}/restore", headers=ALICE).status_code == 404
    assert _deleted_at("chat.threads", "thread_id", thread_id) != [None]


def test_a_chat_with_work_in_flight_is_not_deleted(client, dispatched):
    thread_id = _thread(client)
    client.post(f"/threads/{thread_id}/messages", json={"text": "hello"}, headers=ALICE)

    refused = client.delete(f"/threads/{thread_id}", headers=ALICE)

    assert refused.status_code == 409
    assert "running a exchange" in refused.json()["detail"]
    assert client.get(f"/threads/{thread_id}", headers=ALICE).status_code == 200


def test_me_says_who_is_logged_in_and_whether_they_are_an_admin(client, alice_is_an_admin):
    assert client.get("/me", headers=ALICE).json() == {
        "testbed": "Chameleon",
        "username": "alice",
        "projects": [{"id": "CHI-000001", "nickname": ""}],
        "admin": True,
    }
    assert client.get("/me", headers=BOB).json()["admin"] is False


def test_an_admin_sees_the_current_system_key(client, alice_is_an_admin):
    from cloud_conversations.adapter import system_key

    key = system_key.current()

    listed = client.get("/admin/system-keys", headers=ALICE)

    assert listed.json() == [
        {
            "fingerprint": key.fingerprint,
            "created_at": listed.json()[0]["created_at"],
            "retired_at": None,
            "authorized_key": system_key.authorized_key(key),
        }
    ]


def test_a_queued_build_and_release_wait_while_someone_elses_build_runs(
    client, owner_a, owner_b
):
    alices = uuid.UUID(_thread(client))
    alices_build = store.enqueue_job(owner_a, alices, "build", {})
    store.enqueue_job(owner_a, alices, "teardown", {})
    store.enqueue_job(owner_a, alices, "exchange", {})
    bobs = store.enqueue_job(
        owner_b, uuid.UUID(client.post("/threads", headers=BOB).json()["thread_id"]), "build", {}
    )

    def waiting() -> dict[str, bool]:
        jobs = client.get(f"/threads/{alices}", headers=ALICE).json()["jobs"]
        return {j["kind"]: j["waiting"] for j in jobs}

    assert waiting() == {"build": False, "teardown": False, "exchange": False}
    store.start_job(bobs.job_id)
    assert waiting() == {"build": True, "teardown": True, "exchange": False}
    store.start_job(alices_build.job_id)
    assert waiting() == {"build": False, "teardown": True, "exchange": False}


def test_an_admin_rotation_retires_the_key_new_instances_got(client, alice_is_an_admin):
    from cloud_conversations.adapter import system_key

    old = system_key.current()

    rotated = client.post("/admin/system-keys/rotate", headers=ALICE)

    assert rotated.status_code == 201
    assert rotated.json()["fingerprint"] == system_key.current().fingerprint != old.fingerprint
    assert store.system_key(old.fingerprint).retired_at is not None


def test_the_chat_list_marks_chats_running_or_still_reserving_hardware(
    client, owner_a, bm_requirements, bm_rubric
):
    building, _, building_run = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    store.record_resource(building_run, "lease-1", "lease", "FAKE@BM", None)
    store.enqueue_job(owner_a, building, "build", {})
    reserving, _, reserving_run = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    store.record_resource(reserving_run, "lease-2", "lease", "FAKE@BM", None)
    idle, _, idle_run = _chat_with_a_run(owner_a, bm_requirements, bm_rubric)
    store.record_resource(idle_run, "lease-3", "lease", "FAKE@BM", None)
    store.release_resource_with_event(idle_run, "lease", "lease-3", "FAKE@BM")

    listed = {t["thread_id"]: t["activity"] for t in client.get("/threads", headers=ALICE).json()}

    assert listed == {str(building): "running", str(reserving): "reserved", str(idle): None}


def _answered(owner: str) -> tuple[uuid.UUID, uuid.UUID]:
    """A chat with one answer in it."""
    thread = store.create_thread(owner, "a chat")
    said = store.append_message(
        thread.thread_id, "assistant", "answer", "Use a lease.", owner_id=owner
    )
    return thread.thread_id, said.message_id


def test_rating_a_message_again_replaces_its_rating(client, owner_a):
    thread_id, message_id = _answered(owner_a)

    for rating in ("up", "down"):
        client.post(
            f"/threads/{thread_id}/feedback",
            json={"message_id": str(message_id), "rating": rating}, headers=ALICE,
        )

    feedback = client.get(f"/threads/{thread_id}", headers=ALICE).json()["feedback"]
    assert [(f["message_id"], f["rating"]) for f in feedback] == [(str(message_id), "down")]


def test_a_message_from_another_chat_cannot_be_rated_there(client, owner_a):
    _, message_id = _answered(owner_a)
    other, _ = _answered(owner_a)

    reply = client.post(
        f"/threads/{other}/feedback", json={"message_id": str(message_id), "rating": "up"},
        headers=ALICE,
    )

    assert reply.status_code == 404


def test_feedback_with_neither_a_rating_nor_a_comment_is_refused(client, owner_a):
    thread_id, message_id = _answered(owner_a)

    reply = client.post(
        f"/threads/{thread_id}/feedback", json={"message_id": str(message_id)}, headers=ALICE
    )

    assert reply.status_code == 422
