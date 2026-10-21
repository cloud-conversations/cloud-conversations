"""Releases happen only as the user confirmed them, and each call is in the audit trail."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import TypeAdapter

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.adapter.fake import FAKE_KEYPAIR, default_fake_sites
from cloud_conversations.agent.contract import BuildStep, ReviseStep
from cloud_conversations.runner import sequential
from tests.conftest import BM_SITE, FAST, TEST_DB_URL
from tests.fakes import FailureConfig, FaultyAdapter


def _provision(owner: str, requirements, rubric, adapter: FaultyAdapter) -> uuid.UUID:
    """Build a description the user approved, through the audited adapter."""
    did, version = store.create_description(owner, "conv-releases", requirements, rubric)
    request_id = audit.approved(owner, "cli", description_id=did, version=version)
    with audit.acting(owner, request_id):
        return sequential.provision(
            owner_id=owner, description_id=did, version=version, project_id=None,
            keypair=FAKE_KEYPAIR, adapter=audit.audited(adapter), site=BM_SITE,
            settings=FAST.defaults,
        )


def _calls(owner: str) -> list[dict]:
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT * FROM audit.testbed_calls WHERE owner_id = %s ORDER BY seq", [owner]
        ).fetchall()


def _outcomes(owner: str) -> list[dict]:
    return [c for c in _calls(owner) if c["outcome"] != "started"]


def _deletes(owner: str) -> list[dict]:
    return [c for c in _outcomes(owner) if c["operation"].startswith("delete_")]


def _confirmed(owner: str, run_id: uuid.UUID) -> uuid.UUID:
    request_id = releases.propose(
        owner, "user", "release", "test", releases.held(run_id), run_id=run_id
    )
    releases.confirm(owner, request_id, "web")
    return request_id


@pytest.fixture()
def adapter() -> FaultyAdapter:
    return FaultyAdapter(sites=default_fake_sites())


@pytest.fixture()
def run_id(owner_a, bm_requirements, bm_rubric, adapter) -> uuid.UUID:
    """A run that passed and holds its hardware."""
    return _provision(owner_a, bm_requirements, bm_rubric, adapter)


def test_nothing_is_released_before_the_user_says_yes(owner_a, run_id, adapter):
    held = store.live_resources(run_id)
    request_id = releases.propose(
        owner_a, "agent", "release", "nothing else will help", releases.held(run_id),
        run_id=run_id,
    )

    with pytest.raises(store.ConflictError, match="not confirmed"):
        releases.carry_out(owner_a, request_id, audit.audited(adapter))

    assert store.live_resources(run_id) == held
    assert _deletes(owner_a) == []


def test_nothing_is_released_once_the_run_holds_more_than_the_user_confirmed(
    owner_a, run_id, adapter
):
    request_id = _confirmed(owner_a, run_id)
    store.record_resource(run_id, "lease-late", "lease", BM_SITE, None)
    held = store.live_resources(run_id)

    with pytest.raises(store.ConflictError, match=f"lease lease-late at {BM_SITE}"):
        releases.carry_out(owner_a, request_id, audit.audited(adapter))

    assert store.live_resources(run_id) == held
    assert _deletes(owner_a) == []


@pytest.mark.parametrize(("table", "change"), [
    ("audit.decisions", "UPDATE audit.decisions SET reason = 'nothing happened'"),
    ("audit.testbed_calls", "DELETE FROM audit.testbed_calls"),
])
def test_the_audit_trail_cannot_be_changed(owner_a, run_id, table, change):
    def rows() -> list[tuple]:
        with psycopg.connect(TEST_DB_URL) as conn:
            return conn.execute(f"SELECT * FROM {table} ORDER BY seq").fetchall()

    before = rows()

    with psycopg.connect(TEST_DB_URL) as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute(change)

    assert before and rows() == before


def test_the_runner_records_why_it_gave_a_lease_back(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(unschedulable_boots=2)
    )
    run_id = _provision(owner_a, bm_requirements, bm_rubric, adapter)

    released = next(
        e.payload["provider_id"] for e in store.read_events(owner_a, run_id)
        if e.kind == "resource_released" and e.payload["kind"] == "lease"
    )
    (call,) = [c for c in _deletes(owner_a) if c["kind"] == "lease"]
    assert (call["provider_id"], call["run_id"]) == (released, run_id)
    assert call["reason"].startswith("boot failed, reserving again: ")


def test_the_runner_records_why_it_deleted_an_instance(owner_a, bm_requirements, bm_rubric):
    adapter = FaultyAdapter(
        sites=default_fake_sites(), failures=FailureConfig(instance_boot_fails=True)
    )
    _provision(owner_a, bm_requirements, bm_rubric, adapter)

    (call,) = _deletes(owner_a)
    assert call["operation"] == "delete_instance"
    assert call["reason"].startswith("it failed to boot: ")


def test_the_agents_prose_reaches_the_user_without_control_characters():
    step = TypeAdapter(BuildStep).validate_python({
        "action": "revise", "change": "keep it\n\x1b[8m", "reason": "no \x07image",
    })

    assert isinstance(step, ReviseStep)
    assert (step.change, step.reason) == ("keep it[8m", "no image")


def test_the_cli_is_asked_only_about_the_agents_proposals(owner_a, run_id):
    releases.propose(owner_a, "user", "release", "test", releases.held(run_id), run_id=run_id)

    assert store.pending_release(owner_a, run_id) is None


def test_only_a_confirmed_release_reaches_teardown():
    src = Path(__file__).parents[1] / "src/cloud_conversations"
    callers = {
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr == "teardown"
        and isinstance(node.value, ast.Name) and node.value.id == "sequential"
    }
    assert callers == {"releases.py"}
