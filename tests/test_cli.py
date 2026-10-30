"""CLI tests, driven through main()."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.rows import dict_row

import cloud_conversations.db.store as store
from cloud_conversations import releases
from cloud_conversations.adapter.chameleon.sites import load_capabilities
from cloud_conversations.adapter.fake import HostFacts
from cloud_conversations.cli.main import main
from cloud_conversations.config import load_config
from cloud_conversations.llm.client import LLMError
from cloud_conversations.schema.description import Requirements
from cloud_conversations.schema.knowledge import Chunk, Document, Parent
from tests.conftest import TEST_DB_URL, VMS
from tests.fakes import FakeModel, FaultyAdapter
from tests.loopback import serving, serving_pages


@pytest.fixture(autouse=True)
def owner_env(monkeypatch):
    monkeypatch.setenv("CC_OWNER_ID", "chameleon:cli-test")
    store.ensure_user("chameleon:cli-test")
    return "chameleon:cli-test"


def _main(argv: list[str], monkeypatch) -> tuple[int, str]:
    """Run the CLI and return its exit status and SystemExit message."""
    monkeypatch.setattr("sys.argv", ["cc", *argv])
    with pytest.raises(SystemExit) as exc:
        main()
    code = exc.value.code
    if code is None:
        return 0, ""
    if isinstance(code, int):
        return code, ""
    return 1, str(code)


def _live_resources() -> list[store.Resource]:
    """Every unreleased resource, across every run and owner."""
    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT * FROM experiments.run_resources WHERE released_at IS NULL"
        ).fetchall()
    return [store.Resource(**r) for r in rows]


def test_run_baremetal_end_to_end(monkeypatch, capsys, owner_env):
    code, msg = _main(["run", "--fixture", "baremetal"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 0, out
    assert "run_terminal" in out, "the event stream must be printed"
    assert "check   [PASS]" in out, "check results must be reported"
    assert "state   passed" in out


def test_run_software_installs_and_checks_it(monkeypatch, capsys, owner_env):
    code, msg = _main(["run", "--fixture", "software"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "check   [PASS] numpy: expected '1', got '1.0.0'" in out.splitlines()


def test_run_vm_end_to_end(monkeypatch, capsys, owner_env):
    code, msg = _main(["run", "--fixture", "vm"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "state   passed" in out


def test_run_with_teardown_leaves_nothing_live(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["run", "--fixture", "baremetal", "--teardown", "--yes"], monkeypatch
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "All resources released." in out
    assert _live_resources() == [], "no orphans"


def _decisions(owner: str) -> list[tuple[str, str, str, str | None]]:
    with psycopg.connect(TEST_DB_URL) as conn:
        return conn.execute(
            "SELECT step, action, actor, via FROM audit.decisions "
            "WHERE owner_id = %s ORDER BY seq",
            [owner],
        ).fetchall()


def _calls(owner: str) -> set[tuple[str, str, str]]:
    with psycopg.connect(TEST_DB_URL) as conn:
        rows = conn.execute(
            """
            SELECT c.operation, c.outcome, d.action FROM audit.testbed_calls c
              JOIN (SELECT DISTINCT request_id, action FROM audit.decisions) d USING (request_id)
             WHERE c.owner_id = %s AND c.outcome <> 'started'
            """,
            [owner],
        ).fetchall()
    return set(rows)


def test_teardown_asks_and_a_no_releases_nothing(monkeypatch, capsys, owner_env):
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    held = _live_resources()
    run_id = held[0].run_id
    asked: list[str] = []

    def no(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", no)

    code, _ = _main(["teardown", "--run-id", str(run_id)], monkeypatch)

    assert code == 1
    (prompt,) = asked
    assert prompt.startswith("Release ") and prompt.endswith("? [y/N] ")
    assert set(prompt[len("Release "):-len("? [y/N] ")].split(", ")) == {
        f"{r.kind} {r.provider_id} at {r.site}" for r in held
    }
    assert _live_resources() == held
    assert _decisions(owner_env) == [
        ("confirmed", "build", "user", "cli"),
        ("proposed", "release", "user", "cli"),
        ("declined", "release", "user", "cli"),
    ]


def test_teardown_releases_after_a_yes_and_records_it(monkeypatch, capsys, owner_env):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    code, _ = _main(["run", "--fixture", "baremetal", "--teardown"], monkeypatch)

    assert code == 0
    assert _live_resources() == []
    assert _decisions(owner_env) == [
        ("confirmed", "build", "user", "cli"),
        ("proposed", "release", "user", "cli"),
        ("confirmed", "release", "user", "cli"),
    ]
    assert _calls(owner_env) == {
        ("create_lease", "ok", "build"),
        ("launch_instance", "ok", "build"),
        ("attach_floating_ip", "ok", "build"),
        ("ssh_exec", "ok", "build"),
        ("delete_instance", "ok", "release"),
        ("delete_lease", "ok", "release"),
    }


def test_a_closed_stdin_is_a_no(monkeypatch, capsys, owner_env):
    def closed(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)

    code, _ = _main(["run", "--fixture", "baremetal", "--teardown"], monkeypatch)

    assert code == 1
    assert _live_resources() != []


def test_build_asks_before_the_agent_releases_anything(monkeypatch, capsys, owner_env):
    def lying_adapter(**kwargs):
        return FaultyAdapter(**kwargs, facts_overrides={"cc-": HostFacts(cpu_count=1)})

    monkeypatch.setattr("cloud_conversations.adapter.credentials.FakeAdapter", lying_adapter)
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    held = _live_resources()
    monkeypatch.setattr(
        "cloud_conversations.cli.main.build_client",
        lambda settings, model=None, temperature=None: FakeModel(
            [json.dumps({"action": "abandon", "reason": "the host is too small"})]
        ),
    )
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    capsys.readouterr()

    _main(["build", "--run-id", str(held[0].run_id)], monkeypatch)

    assert "It says: the host is too small" in capsys.readouterr().out
    assert _live_resources() == held
    assert _decisions(owner_env)[-3:] == [
        ("confirmed", "resume", "user", "cli"),
        ("proposed", "release", "agent", None),
        ("declined", "release", "user", "cli"),
    ]


def test_build_does_not_ask_again_about_a_proposal_from_before(
    monkeypatch, capsys, owner_env
):
    def lying_adapter(**kwargs):
        return FaultyAdapter(**kwargs, facts_overrides={"cc-": HostFacts(cpu_count=1)})

    monkeypatch.setattr("cloud_conversations.adapter.credentials.FakeAdapter", lying_adapter)
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    run_id = _live_resources()[0].run_id
    releases.offer(owner_env, store.get_run(owner_env, run_id), "too small", {}, None)

    def unreachable(settings, model=None, temperature=None):
        raise LLMError("the model endpoint is down")

    def never(prompt: str) -> str:
        raise AssertionError(f"asked {prompt!r}")

    monkeypatch.setattr("cloud_conversations.cli.main.build_client", unreachable)
    monkeypatch.setattr("builtins.input", never)

    _main(["build", "--run-id", str(run_id)], monkeypatch)

    assert "The experiment agent wants" not in capsys.readouterr().out


def test_run_exits_nonzero_when_the_rubric_fails(monkeypatch, capsys, owner_env):
    def lying_adapter(**kwargs):
        return FaultyAdapter(**kwargs, facts_overrides={"cc-": HostFacts(cpu_count=1)})

    monkeypatch.setattr("cloud_conversations.adapter.credentials.FakeAdapter", lying_adapter)
    code, msg = _main(["run", "--fixture", "baremetal"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 1
    assert "check   [FAIL]" in out
    assert "state   failed" in out


def test_show_run_prints_the_event_stream(monkeypatch, capsys, owner_env):
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    capsys.readouterr()
    run_id = _live_resources()[0].run_id

    code, msg = _main(["show-run", "--run-id", str(run_id), "--events"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    assert "plan_proposed" in out
    assert "validation_completed" in out


def test_show_run_json_is_machine_readable(monkeypatch, capsys, owner_env):
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    capsys.readouterr()
    run_id = _live_resources()[0].run_id

    code, msg = _main(["show-run", "--run-id", str(run_id), "--json", "--events"], monkeypatch)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["state"] == "passed"
    assert payload["results"]
    assert payload["events"]


def test_show_run_is_owner_scoped(monkeypatch, capsys, owner_env):
    """The CLI must not read the owner out of the row."""
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    capsys.readouterr()
    run_id = _live_resources()[0].run_id

    monkeypatch.setenv("CC_OWNER_ID", "chameleon:somebody-else")
    code, msg = _main(["show-run", "--run-id", str(run_id)], monkeypatch)
    assert code != 0
    assert "not found" in msg.lower()


def test_teardown_is_owner_scoped(monkeypatch, capsys, owner_env):
    _main(["run", "--fixture", "baremetal"], monkeypatch)
    capsys.readouterr()
    run_id = _live_resources()[0].run_id

    monkeypatch.setenv("CC_OWNER_ID", "chameleon:somebody-else")
    code, msg = _main(["teardown", "--run-id", str(run_id)], monkeypatch)
    assert code != 0
    assert _live_resources(), "another owner must not be able to tear this down"


def test_bad_uuid_is_an_error_not_a_traceback(monkeypatch, capsys, owner_env):
    code, msg = _main(["show-run", "--run-id", "not-a-uuid"], monkeypatch)
    assert code != 0
    assert "ValueError: badly formed" in msg


def test_capabilities_lists_the_configured_sites(monkeypatch, capsys, owner_env):
    code, msg = _main(["capabilities"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    for site in load_capabilities(load_config(), "chameleon").sites:
        assert site.site in out
        assert ",".join(k.reservation_kind for k in site.kinds) in out


def _desc_id(out: str) -> str:
    return next(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("description_id")
    )


def _describe(monkeypatch, capsys, name: str = "baremetal") -> str:
    code, msg = _main(["describe-fixture", "--name", name], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, msg
    return _desc_id(out)


def test_describe_fixture_then_provision(monkeypatch, capsys, owner_env):
    desc_id = _describe(monkeypatch, capsys)

    code, msg = _main(
        ["provision", "--description-id", desc_id,
         "--site", "FAKE@BM", "--teardown", "--yes"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg or out
    assert "at FAKE@BM (from --site)" in out
    assert "state   passed" in out
    assert _live_resources() == []


def test_provision_defaults_to_the_latest_version(monkeypatch, capsys, owner_env):
    desc_id = _describe(monkeypatch, capsys)
    code, _ = _main(
        ["describe-fixture", "--name", "baremetal", "--description-id", desc_id],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "version        : 2" in out

    code, msg = _main(
        ["provision", "--description-id", desc_id,
         "--site", "FAKE@BM"],
        monkeypatch,
    )
    capsys.readouterr()
    assert code == 0, msg
    run_id = _live_resources()[0].run_id
    assert store.get_run(owner_env, run_id).description_version == 2


def test_provision_needs_a_site_it_can_resolve(monkeypatch, capsys, owner_env):
    desc_id = _describe(monkeypatch, capsys, "vm")
    code, msg = _main(
        ["provision", "--description-id", desc_id,
         "--site", "NOPE@NOWHERE"],
        monkeypatch,
    )
    assert code != 0
    assert "unknown site" in msg


def test_provision_rejects_another_owners_description(monkeypatch, capsys, owner_env):
    desc_id = _describe(monkeypatch, capsys)
    monkeypatch.setenv("CC_OWNER_ID", "chameleon:somebody-else")
    code, msg = _main(
        ["provision", "--description-id", desc_id,
         "--site", "FAKE@BM"],
        monkeypatch,
    )
    assert code != 0
    assert "not found" in msg.lower()


def _check_args():
    return ["check", "--site", "FAKE@BM",
            "--keypair", "cc-keypair", "--node-type", "compute_skylake"]


def test_check_passes_against_the_fake(monkeypatch, capsys, owner_env):
    code, msg = _main(_check_args(), monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "[FAIL]" not in out
    for label in ("login + lease list", "instance list", "keypair", "image", "compute catalog",
                  "availability calendar"):
        assert label in out


def test_check_fails_on_a_missing_keypair(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["check", "--site", "FAKE@BM",
         "--keypair", "no-such-key", "--node-type", "compute_skylake"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "[FAIL] keypair" in out


def test_check_fails_on_an_unknown_node_type(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["check", "--site", "FAKE@BM",
         "--keypair", "cc-keypair", "--node-type", "not_a_type"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "not in the" in out and "node types at this site" in out


def test_describe_fixture_unchanged_without_overrides(monkeypatch, capsys, owner_env):
    code, msg = _main(["describe-fixture", "--name", "baremetal"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "site           : FAKE@BM" in out
    assert "image_name     : CC-Ubuntu24.04" in out
    assert "start_at" not in out


def test_describe_fixture_overrides_for_a_real_site(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--site", "CHI@UC",
         "--node-type", "compute_haswell",
         "--hours", "3", "--min-cpus", "24"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "site           : CHI@UC" in out
    assert "duration_hours : 3" in out

    desc = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert desc.requirements.site == "CHI@UC"
    assert {i.key for i in desc.requirements.items} == {
        "node_type", "count", "image_name", "os_release",
    }
    items = desc.requirements.items
    assert next(i.value for i in items if i.key == "node_type") == "compute_haswell"
    assert next(c.expected for c in desc.rubric if c.kind == "cpu_count") == 24
    assert {c.kind for c in desc.rubric}.isdisjoint({"mem_gb", "disk_gb"})


def test_describe_fixture_rejects_the_wrong_reservation_kind(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--site", "KVM@TACC",
         "--node-type", "compute_skylake"],
        monkeypatch,
    )
    assert code != 0
    assert "KVM@TACC takes --flavor, not --node-type" in msg


def test_describe_fixture_kvm_override(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "vm", "--site", "KVM@TACC",
         "--flavor", "m1.xlarge"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    desc = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert next(i.value for i in desc.requirements.items if i.key == "flavor") == "m1.xlarge"


def test_the_flag_picks_the_kind_at_a_site_with_both(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--site", "CHI@TACC",
         "--flavor", "m1.small"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    desc = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert [(i.key, i.value) for i in desc.requirements.items][0] == ("flavor", "m1.small")


def test_provision_falls_back_to_the_descriptions_site(monkeypatch, capsys, owner_env):
    desc_id = _describe(monkeypatch, capsys)

    code, msg = _main(
        ["provision", "--description-id", desc_id, "--teardown", "--yes"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg or out
    assert "at FAKE@BM (from description)" in out, "and it must say where the site came from"
    assert "state   passed" in out
    assert _live_resources() == []


def test_run_records_the_site_it_was_pointed_at(monkeypatch, capsys, owner_env):
    code, _ = _main(
        ["run", "--fixture", "baremetal",
         "--site", "FAKE@KVM", "--keypair", "cc-keypair"],
        monkeypatch,
    )
    capsys.readouterr()
    assert code != 0

    with psycopg.connect(TEST_DB_URL, row_factory=dict_row) as conn:
        rows = conn.execute(
            "SELECT requirements FROM experiments.experiment_descriptions WHERE owner_id = %s",
            [owner_env],
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["requirements"]["site"] == "FAKE@KVM"


def test_run_off_the_fixture_site_requires_a_keypair(monkeypatch, capsys, owner_env):
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_ID", "CHI-000001")
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_SECRET", "secret")
    code, msg = _main(
        ["run", "--fixture", "baremetal", "--site", "CHI@UC"],
        monkeypatch,
    )
    assert code != 0
    assert msg == (
        "ERROR: CHI@UC needs --keypair; name a keypair at the site or an imported key's"
    )
    assert _live_resources() == [], "nothing should have been built"


def test_run_applies_a_keypair_without_a_site_override(monkeypatch, capsys, owner_env):
    code, _ = _main(
        ["run", "--fixture", "baremetal",
         "--keypair", "not-a-real-key"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code != 0, out
    assert "not-a-real-key" in out
    assert "state   failed" in out


def test_a_non_site_override_stays_on_the_fixture_site(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--hours", "8"], monkeypatch
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "site           : FAKE@BM" in out
    assert "duration_hours : 8" in out

    desc = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert desc.requirements.duration_hours == 8
    assert desc.requirements.site == "FAKE@BM"


def test_a_patched_fixture_still_provisions(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--hours", "8", "--min-cpus", "32"],
        monkeypatch,
    )
    desc_id = _desc_id(capsys.readouterr().out)
    assert code == 0, msg
    desc = store.latest_description(owner_env, uuid.UUID(desc_id))
    assert desc.requirements.duration_hours == 8
    assert next(c.expected for c in desc.rubric if c.kind == "cpu_count") == 32

    code, msg = _main(
        ["provision", "--description-id", desc_id, "--teardown", "--yes"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg or out
    assert "state   passed" in out


def test_patching_the_os_release_moves_its_check(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--os-release", "Ubuntu 22.04"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    desc = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert next(i.value for i in desc.requirements.items if i.key == "os_release") == "Ubuntu 22.04"
    assert next(c.expected for c in desc.rubric if c.kind == "os_release") == "Ubuntu 22.04"


def test_patching_a_fixture_rejects_the_wrong_reservation_kind(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--flavor", "m1.large"], monkeypatch
    )
    assert code != 0
    assert "use --node-type, not --flavor" in msg


def test_a_patched_duration_is_still_validated(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--hours", "0"], monkeypatch
    )
    assert code != 0
    assert "duration_hours" in msg


def test_a_duration_over_the_site_cap_is_refused(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--hours", "100000"], monkeypatch
    )
    assert code != 0
    assert "exceeds max_lease_hours=168" in msg


def test_provision_at_a_real_site_requires_a_keypair(monkeypatch, capsys, owner_env):
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_ID", "CHI-000001")
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_SECRET", "secret")
    _main(
        ["describe-fixture", "--name", "baremetal", "--site", "CHI@UC",
         "--node-type", "compute_haswell"],
        monkeypatch,
    )
    desc_id = _desc_id(capsys.readouterr().out)

    code, msg = _main(["provision", "--description-id", desc_id], monkeypatch)

    assert code != 0
    assert msg == (
        "ERROR: CHI@UC needs --keypair; name a keypair at the site or an imported key's"
    )


def test_check_does_not_call_a_skipped_run_a_pass(monkeypatch, capsys, owner_env):
    code, msg = _main(["check", "--site", "FAKE@BM"], monkeypatch)
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "[SKIP]" in out
    assert "All read-only checks passed" not in out
    assert "not applicable here" in out
    assert "keypair None" not in out
    assert f"[PASS] image '{load_config().defaults.image_name}'" in out


def test_check_says_all_passed_when_nothing_was_skipped(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["check", "--site", "FAKE@KVM",
         "--keypair", "cc-keypair", "--flavor", "m1.large"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "[SKIP]" not in out
    assert "All read-only checks passed" in out


def test_image_name_pins_the_image_that_boots(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal",
         "--image-name", "CC-Ubuntu22.04", "--os-release", "Ubuntu 22.04"],
        monkeypatch,
    )
    desc_id = _desc_id(capsys.readouterr().out)
    assert code == 0, msg

    code, msg = _main(
        ["provision", "--description-id", desc_id, "--teardown", "--yes"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg or out
    assert "state   passed" in out
    assert "CC-Ubuntu22.04" in out


def test_run_honours_image_name(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["run", "--fixture", "baremetal",
         "--image-name", "CC-Ubuntu22.04"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code != 0, out
    assert "check   [FAIL] os" in out


def test_an_unknown_image_name_is_a_description_failure(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--image-name", "CC-NoSuchImage99"],
        monkeypatch,
    )
    desc_id = _desc_id(capsys.readouterr().out)
    assert code == 0, msg

    code, msg = _main(
        ["provision", "--description-id", desc_id], monkeypatch
    )
    out = capsys.readouterr().out
    assert code != 0
    assert "image_name='CC-NoSuchImage99'" in out
    assert "os_release=" not in out.split("No image found")[1][:60]
    assert "[description]" in out, "a missing image is the description's fault, not infra"
    assert _live_resources(), "the lease was already taken before step-0002"


def test_check_fails_on_a_missing_image(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["check", "--site", "FAKE@BM", "--image-name", "CC-Nope99"],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "no image named 'CC-Nope99'" in out


def test_teardown_names_a_lease_it_cannot_release(
    monkeypatch, capsys, owner_env, bm_requirements, bm_rubric
):
    desc_id, version = store.create_description(
        owner_env, "conv-td", bm_requirements, bm_rubric
    )
    run_id = store.create_run(owner_env, desc_id, version, None)
    store.record_resource(run_id, "lease-abc", "lease", "FAKE@BM", None)

    code, _ = _main(["teardown", "--run-id", str(run_id), "--yes"], monkeypatch)
    captured = capsys.readouterr()

    assert code == 1
    assert "lease-abc" in captured.err


def test_teardown_says_so_when_a_run_has_nothing_live(
    monkeypatch, capsys, owner_env, bm_requirements, bm_rubric
):
    desc_id, version = store.create_description(
        owner_env, "conv-td2", bm_requirements, bm_rubric
    )
    run_id = store.create_run(owner_env, desc_id, version, None)

    code, _ = _main(["teardown", "--run-id", str(run_id)], monkeypatch)
    assert code == 0
    assert "No live resources" in capsys.readouterr().out


def test_the_fake_path_does_not_import_the_provider_libraries():
    import subprocess
    import sys

    probe = (
        "import sys, cloud_conversations.cli.main, cloud_conversations.runner.sequential\n"
        "from cloud_conversations.adapter import credentials\n"
        "from cloud_conversations.config import load_config\n"
        "credentials.build_adapter('FAKE@BM', load_config())\n"
        "print(','.join(m for m in ('chi','paramiko','keystoneauth1','openstack')"
        " if m in sys.modules))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"provider libraries imported: {result.stdout.strip()}"


def test_capabilities_rejects_a_site_with_no_catalog(monkeypatch, capsys, owner_env):
    code, msg = _main(["capabilities", "--site", "KVM@TACC"], monkeypatch)
    assert code != 0
    assert "publishes no node catalog" in msg


def test_capabilities_reports_an_unreachable_catalog(monkeypatch, capsys, owner_env):
    from cloud_conversations.adapter.chameleon import catalog

    def boom(url, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(catalog.urllib.request, "urlopen", boom)
    code, msg = _main(["capabilities", "--site", "CHI@UC"], monkeypatch)
    assert code != 0
    assert "no route" in msg


def test_a_chameleon_site_selects_the_chameleon_adapter(monkeypatch):
    for var in ("OS_CLOUD", "OS_APPLICATION_CREDENTIAL_ID", "OS_USERNAME"):
        monkeypatch.delenv(var, raising=False)
    code, msg = _main(["check", "--site", "CHI@UC", "--keypair", "k"], monkeypatch)
    assert code != 0
    assert "OS_CLOUD" in msg


def test_describe_fixture_sets_a_lease_start(monkeypatch, capsys, owner_env):
    tuesday = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--start-at", tuesday.isoformat()],
        monkeypatch,
    )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert f"start_at       : {tuesday.isoformat()}" in out

    stored = store.latest_description(owner_env, uuid.UUID(_desc_id(out)))
    assert stored.requirements.start_at == tuesday


def test_describe_fixture_reads_a_bare_time_as_utc(monkeypatch, capsys, owner_env):
    tuesday = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal",
         "--start-at", tuesday.strftime("%Y-%m-%dT%H:%M:%S")],
        monkeypatch,
    )
    assert code == 0, msg
    assert tuesday.isoformat() in capsys.readouterr().out


def test_describe_fixture_rejects_an_unusable_start(monkeypatch, capsys, owner_env):
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--start-at", "next tuesday"],
        monkeypatch,
    )
    assert code == 1 and "ISO 8601" in msg

    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    code, msg = _main(
        ["describe-fixture", "--name", "baremetal", "--start-at", yesterday.isoformat()],
        monkeypatch,
    )
    assert code == 1 and "in the past" in msg


_UC = "CHI@UC"


def _uc_nodes(node_type: str = "compute_skylake", n: int = 2) -> dict:
    item = {
        "node_type": node_type, "site_id": "uc", "availability": "available",
        "architecture": {"platform_type": "x86_64", "smt_size": 48},
        "main_memory": {"ram_size": 206158430208},
        "storage_devices": [{"size": 240000000000}],
        "gpu": {"gpu": False},
    }
    return {"total": n, "offset": 0, "items": [dict(item) for _ in range(n)]}


@pytest.fixture()
def describe_env(monkeypatch, tmp_path):
    """A config whose reference API and model are both local."""

    @contextmanager
    def _setup(replies: list[str], payload: dict | None = None):
        with serving(payload or _uc_nodes()) as (root, _):
            config = tmp_path / "config.toml"
            config.write_text(
                "[defaults]\nmax_rounds = 4\n\n"
                "[adapters.chameleon]\n"
                f'site_index_url = "{root}/sites"\n\n'
                "[adapters.chameleon.images]\n"
                'prefix = "CC-"\n\n'
                "[adapters.chameleon.kinds.bm]\n"
                'reservation_kind = "physical_host"\n'
                f'[adapters.chameleon.sites."{_UC}"]\n'
                'kinds = ["bm"]\n'
                f'node_catalog_url = "{root}/nodes"\n'
            )
            monkeypatch.setenv("CC_CONFIG_FILE", str(config))
            monkeypatch.setattr(
                "cloud_conversations.cli.main.build_client",
                lambda settings, model=None, temperature=None: FakeModel(replies),
            )
            yield

    return _setup


def _describe_reply(action: str, **over) -> str:
    if action == "search":
        return json.dumps({"action": "search", "query": {
            "reserve_by": "node_type",
            "site": _UC, "node_type": "compute_skylake", "duration_hours": 2,
            "intent": "a skylake node", **over}})
    return json.dumps({"action": "answer", "description": {
        "site": _UC, "duration_hours": 2,
        "items": [
            {"key": "node_type", "value": "compute_skylake", "source": "stated"},
            {"key": "os_release", "value": "Ubuntu 24.04", "source": "default"},
        ],
        "rubric": [{"id": "ssh", "kind": "ssh_reachable", "expected": True,
                    "comparator": "eq"},
                   {"id": "os", "kind": "os_release", "expected": "Ubuntu 24.04",
                    "comparator": "prefix", "from_requirement": "os_release"}],
        **over}})


def test_describe_stores_a_description_and_a_conversation(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        code, msg = _main(
            ["describe", "a skylake node for two hours", "--site", _UC],
            monkeypatch,
        )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "compute_skylake" in out
    assert "ccx provision --description-id" in out

    desc_id = _desc_id(out)
    stored = store.latest_description(owner_env, uuid.UUID(desc_id))
    assert stored.requirements.value_of("node_type") == "compute_skylake"

    conversations = store.conversations_for_description(owner_env, uuid.UUID(desc_id))
    assert len(conversations) == 1
    assert conversations[0].prompt == "a skylake node for two hours"
    assert [t["action"] for t in conversations[0].transcript] == ["search", "answer"]


def test_describe_revises_an_existing_lineage(monkeypatch, capsys, owner_env, describe_env):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a skylake node", "--site", _UC], monkeypatch)
    desc_id = _desc_id(capsys.readouterr().out)

    with describe_env([
        _describe_reply("search", duration_hours=4, intent="four hours"),
        _describe_reply("answer", duration_hours=4, duration_source="stated"),
    ]):
        code, msg = _main(
            ["describe", "make it four hours", "--description-id", desc_id],
            monkeypatch,
        )
    out = capsys.readouterr().out
    assert code == 0, msg
    assert "version        : 2" in out

    latest = store.latest_description(owner_env, uuid.UUID(desc_id))
    assert latest.version == 2
    assert latest.parent_version == 1
    assert latest.revision_reason == "make it four hours"
    assert latest.requirements.duration_hours == 4


def test_describe_that_never_answers_still_stores_the_conversation(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("answer")] * 4):
        code, msg = _main(
            ["describe", "a node made of cheese", "--site", _UC],
            monkeypatch,
        )
    out = capsys.readouterr().out
    assert code == 1
    assert "No description after 4 rounds" in out
    assert "no search found" in out

    conv_id = next(line.split(":", 1)[1].strip().split()[0] for line in out.splitlines()
                   if line.startswith("conversation_id"))
    conversation = store.get_conversation(owner_env, uuid.UUID(conv_id))
    assert conversation.description_id is None
    assert len(conversation.transcript) == 4


def test_verbose_reports_rounds_on_stderr_leaving_stdout_clean(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a node", "--site", _UC, "--verbose"], monkeypatch)
    captured = capsys.readouterr()

    assert "round 1  search    2 free" in captured.err
    assert "round 2  answer    accepted" in captured.err
    assert "round 1" not in captured.out, "progress must not pollute the result"
    assert "description_id" in captured.out


def test_verbose_names_a_decline_instead_of_calling_it_unparsed(
    monkeypatch, capsys, owner_env, describe_env
):
    decline = json.dumps({"action": "decline", "reason": "no quantum computer here"})
    with describe_env([decline]):
        code, _ = _main(["describe", "a quantum computer", "--site", _UC, "--verbose"], monkeypatch)
    captured = capsys.readouterr()

    assert code == 1
    assert "round 1  decline" in captured.err
    assert "unparsed" not in captured.err, "the model replied exactly as asked"


def test_describe_without_verbose_is_quiet_until_it_finishes(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a node", "--site", _UC], monkeypatch)
    assert capsys.readouterr().err == ""


def test_show_conversation_prints_the_transcript(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a node", "--site", _UC], monkeypatch)
    out = capsys.readouterr().out
    conv_id = next(line.split(":", 1)[1].strip().split()[0] for line in out.splitlines()
                   if line.startswith("conversation_id"))

    code, msg = _main(["show-conversation", "--conversation-id", conv_id], monkeypatch)
    printed = capsys.readouterr().out
    assert code == 0, msg
    assert "--- round 1  search" in printed
    assert "--- round 2  answer" in printed
    assert "compute_skylake" in printed


def test_describe_links_the_description_to_its_conversation(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a skylake node", "--site", _UC], monkeypatch)
    out = capsys.readouterr().out

    desc_id = _desc_id(out)
    conv_id = next(line.split(":", 1)[1].strip().split()[0] for line in out.splitlines()
                   if line.startswith("conversation_id"))

    stored = store.latest_description(owner_env, uuid.UUID(desc_id))
    assert stored.conversation_id == conv_id


def _decline_reply(reason: str, suggestions: list[str]) -> str:
    return json.dumps({"action": "decline", "reason": reason, "suggestions": suggestions})


def test_describe_reports_a_refusal_and_stores_it(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_decline_reply(
        "this testbed provisions computers, not ovens", ["ask for a bare-metal node"]
    )]):
        code, msg = _main(
            ["describe", "bake me a cake", "--site", _UC], monkeypatch
        )
    out = capsys.readouterr().out
    assert code == 1, msg
    assert "Declined: this testbed provisions computers, not ovens" in out
    assert "- ask for a bare-metal node" in out
    assert "description_id" not in out

    conv_id = next(line.split(":", 1)[1].strip().split()[0] for line in out.splitlines()
                   if line.startswith("conversation_id"))
    conversation = store.get_conversation(owner_env, uuid.UUID(conv_id))
    assert conversation.description_id is None
    assert [t["action"] for t in conversation.transcript] == ["decline"]


def test_describe_shows_what_each_requirement_resolves_to(
    monkeypatch, capsys, owner_env, describe_env
):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a skylake node", "--site", _UC], monkeypatch)
    out = capsys.readouterr().out

    assert "48 cpu" in out, "the hardware behind the name, sits on the requirement"
    assert "CC-Ubuntu24.04" in out, "derived from the os_release requirement"
    assert out.rstrip().splitlines()[-1].startswith("ccx provision --description-id")
    assert out.index("rubric") < out.index("ccx provision"), "rubric before the command"


def test_the_resolved_image_carries_an_id_only_when_images_are_known(
    monkeypatch, capsys, owner_env
):
    from cloud_conversations.adapter.fake import FakeAdapter, default_fake_sites
    from cloud_conversations.agent.catalog import get_catalog
    from cloud_conversations.agent.contract import ProposedDescription
    from cloud_conversations.cli.main import _print_description
    from tests.conftest import FAST, logged_out

    answer = ProposedDescription.model_validate({
        "site": "FAKE@BM", "duration_hours": 2,
        "items": [{"key": "node_type", "value": "compute_skylake", "source": "stated"},
                  {"key": "os_release", "value": "Ubuntu 24.04", "source": "default"}],
        "rubric": [{"id": "ssh", "kind": "ssh_reachable", "expected": True,
                    "comparator": "eq"}],
    })
    adapter = FakeAdapter(sites=default_fake_sites(), config=FAST)
    args = (uuid.uuid4(), 1, uuid.uuid4(), 2)

    _print_description(answer, get_catalog(logged_out(*adapter.capabilities().sites), FAST), *args)
    without = capsys.readouterr().out
    assert "CC-Ubuntu24.04" in without
    assert "img-ubuntu2404" not in without

    _print_description(answer, get_catalog(adapter, FAST), *args)
    assert "CC-Ubuntu24.04  img-ubuntu2404" in capsys.readouterr().out


def test_a_gpu_flavor_resolves_to_its_own_hardware(capsys):
    from cloud_conversations.adapter.protocol import Flavor, SiteDescriptor
    from cloud_conversations.agent.catalog import get_catalog
    from cloud_conversations.agent.contract import ProposedDescription
    from cloud_conversations.cli.main import _print_description
    from tests.conftest import PUBLISHED_CONFIG, logged_out

    config = PUBLISHED_CONFIG
    caps = logged_out(SiteDescriptor(
        site="KVM@TACC", auth_url="https://kvm.tacc.invalid:5000/v3",
        kinds=VMS,
        flavors=[
            Flavor(flavor_id="1", name="m1.large", vcpus=4, mem_gb=8, disk_gb=40),
            Flavor(flavor_id="2", name="g1.h100.pci.1", vcpus=48, mem_gb=244,
                   disk_gb=40, gpu=True),
        ],
    ))

    def _answer_for(flavor: str) -> ProposedDescription:
        return ProposedDescription.model_validate({
            "site": "KVM@TACC", "duration_hours": 24,
            "items": [{"key": "flavor", "value": flavor, "source": "stated"},
                      {"key": "os_release", "value": "Ubuntu 24.04", "source": "default"}],
            "rubric": [{"id": "ssh", "kind": "ssh_reachable", "expected": True,
                        "comparator": "eq"}],
        })

    catalog = get_catalog(caps, config)
    args = (uuid.uuid4(), 1, uuid.uuid4(), 2)

    _print_description(_answer_for("m1.large"), catalog, *args)
    assert (
        "  flavor       'm1.large'               stated   4 vcpu, 8 GB ram, 40 GB disk"
        in capsys.readouterr().out.splitlines()
    )

    _print_description(_answer_for("g1.h100.pci.1"), catalog, *args)
    assert "48 vcpu, 244 GB ram, 40 GB disk, gpu" in capsys.readouterr().out


def test_the_start_is_always_stated(monkeypatch, capsys, owner_env, describe_env):
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        _main(["describe", "a node", "--site", _UC], monkeypatch)
    assert "start_at       : when provisioned" in capsys.readouterr().out


def test_a_named_start_is_shown_as_the_time(monkeypatch, capsys, owner_env, describe_env):
    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    replies = [
        _describe_reply("search", start_at=when.isoformat()),
        _describe_reply("answer", start_at=when.isoformat()),
    ]
    with describe_env(replies):
        _main(["describe", "a node on Thursday", "--site", _UC], monkeypatch)
    assert f"start_at       : {when.isoformat()}" in capsys.readouterr().out


def _revised_build(owner: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A build of two runs: v1 failed, the agent revised it, v2 passed."""
    desc_id, _ = store.create_description(
        owner_id=owner, conversation_id=str(uuid.uuid4()),
        requirements=Requirements(site="FAKE@BM", duration_hours=1),
        rubric=[],
    )
    first = store.create_run(owner, desc_id, 1, None)
    store.append_event(first, "agent", "model_turn", {
        "round": 1, "action": "revise", "text": '{"action":"revise"}',
        "usage": {}, "finish_reason": "stop", "reasons": [],
    }, owner_id=owner)

    conversation = uuid.uuid4()
    store.create_description(
        owner_id=owner, conversation_id=str(conversation),
        requirements=Requirements(site="FAKE@BM", duration_hours=1),
        rubric=[], description_id=desc_id, parent_version=1,
        revision_reason="use an image this site has",
    )
    store.create_conversation(
        owner, "use an image this site has",
        [{"round": 1, "action": "answer", "text": '{"action":"answer"}', "reasons": []}],
        desc_id, 2, conversation_id=conversation,
    )
    second = store.create_run(owner, desc_id, 2, None)
    store.append_event(second, "agent", "description_revision_requested", {
        "from_run_id": str(first), "from_version": 1, "version": 2,
        "change": "use an image this site has",
    }, owner_id=owner)
    return desc_id, first, second


def test_show_build_walks_both_runs_and_both_agents(monkeypatch, capsys, owner_env):
    desc_id, first, second = _revised_build(owner_env)

    _main(["show-build", "--run-id", str(second)], monkeypatch)
    out = capsys.readouterr().out

    assert "build of 2 run(s)" in out
    assert out.index(str(first)) < out.index(str(second)), "oldest run first"
    assert "experiment agent, round 1: revise" in out
    assert "asked for: use an image this site has" in out, "the sentence that crossed"
    assert out.index("version 1") < out.index("version 2")


def test_show_build_accepts_a_description_id(monkeypatch, capsys, owner_env):
    desc_id, _, second = _revised_build(owner_env)

    _main(["show-build", "--description-id", str(desc_id)], monkeypatch)
    out = capsys.readouterr().out

    assert "2 runs against this description; showing the most recent" in out
    assert str(second) in out


def test_show_build_needs_something_to_look_up(monkeypatch, capsys, owner_env):
    code, message = _main(["show-build"], monkeypatch)

    assert code == 1
    assert "--run-id or --description-id" in message


_DOCS_INDEX = '<html><body><a href="leases.html">leases</a></body></html>'
_DOCS_PAGE = (
    "<html><head><title>Leases</title></head><body>"
    '<div role="main"><h1>Leases</h1>'
    "<p>Reserve a bare-metal node by creating a lease in Blazar.</p></div>"
    "</body></html>"
)


@pytest.fixture()
def knowledge_env(monkeypatch, tmp_path, embedder, reranker):
    """A config whose documentation site, embedder, reranker and model are all local."""

    @contextmanager
    def _setup(replies: list[str] | None = None):
        pages = {"/docs/contents.html": _DOCS_INDEX, "/docs/leases.html": _DOCS_PAGE}
        with serving_pages(pages) as (root, _):
            config = tmp_path / "config.toml"
            config.write_text(
                "[knowledge]\n"
                f'cache_dir = "{tmp_path / "cache"}"\n'
                "fetch_attempts = 1\nfetch_retry_s = 0\n"
                'query_prefix = ""\n\n'
                "[knowledge.sources.readthedocs]\n"
                'kind = "sphinx"\n'
                f'url = "{root}/docs/"\n'
                'label = "Chameleon Docs"\n'
                "primary = true\n"
            )
            monkeypatch.setenv("CC_CONFIG_FILE", str(config))
            monkeypatch.setattr(
                "cloud_conversations.knowledge.embed.BgeEmbedder", lambda knowledge: embedder
            )
            monkeypatch.setattr(
                "cloud_conversations.knowledge.retrieve._load_models",
                lambda knowledge: (embedder, reranker),
            )
            monkeypatch.setattr(
                "cloud_conversations.cli.main.build_client",
                lambda settings, model=None, temperature=None: FakeModel(replies or []),
            )
            yield

    return _setup


def test_index_refuses_a_disabled_knowledge_base(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[knowledge]\nenabled = false\n")
    monkeypatch.setenv("CC_CONFIG_FILE", str(config))

    code, msg = _main(["index"], monkeypatch)

    assert code == 1
    assert msg == "ERROR: the knowledge base is disabled; set [knowledge] enabled = true"


def test_index_makes_the_fetched_documentation_live(monkeypatch, capsys, knowledge_env):
    with knowledge_env():
        code, msg = _main(["index"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 0, msg
    assert "readthedocs        1 documents (fetched)" in out
    assert "index live: 1 documents" in out
    live = store.latest_index()
    assert live is not None
    assert live.documents == 1
    assert live.dimensions == 64


def test_index_reads_the_cache_when_told_to(monkeypatch, capsys, knowledge_env):
    with knowledge_env():
        _main(["index"], monkeypatch)
        capsys.readouterr()
        code, msg = _main(["index", "--use-cache"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 0, msg
    assert "readthedocs        1 documents (cache)" in out


def test_index_refuses_a_source_it_does_not_know(monkeypatch, capsys, knowledge_env):
    with knowledge_env():
        code, msg = _main(["index", "--refresh", "forum"], monkeypatch)

    assert code == 1
    assert "unknown source(s) forum" in msg


def test_index_without_configured_sources_says_so(monkeypatch, capsys, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[knowledge]\npassages = 4\n")
    monkeypatch.setenv("CC_CONFIG_FILE", str(config))

    code, msg = _main(["index"], monkeypatch)

    assert code == 1
    assert "no [knowledge.sources]" in msg


def test_knowledge_reports_what_the_live_index_holds(
    monkeypatch, capsys, knowledge_env
):
    with knowledge_env():
        _main(["index"], monkeypatch)
        capsys.readouterr()
        code, msg = _main(["knowledge"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 0, msg
    assert "documents : 1" in out
    assert "readthedocs" in out and "primary" in out


def test_knowledge_without_an_index_says_to_build_one(monkeypatch, capsys):
    code, msg = _main(["knowledge"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 1
    assert "run ccx index" in out


def test_ask_answers_from_the_index_and_stores_the_conversation(
    monkeypatch, capsys, owner_env, knowledge_env
):
    with knowledge_env(["Create a lease in Blazar."]):
        _main(["index"], monkeypatch)
        capsys.readouterr()
        code, msg = _main(["ask", "creating a lease in Blazar"], monkeypatch)
    out = capsys.readouterr().out

    assert code == 0, msg
    assert "Create a lease in Blazar." in out
    assert "/docs/leases.html" in out

    conversation_id = next(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("conversation_id")
    )
    stored = store.get_conversation(owner_env, uuid.UUID(conversation_id))
    assert stored.prompt == "creating a lease in Blazar"
    assert stored.description_id is None
    assert stored.transcript[0]["text"] == "Create a lease in Blazar."
    assert len(stored.transcript[0]["sources"]) == 1
    assert stored.transcript[0]["sources"][0].endswith("/docs/leases.html")


def test_ask_reports_what_the_reranker_scored(monkeypatch, capsys, knowledge_env):
    with knowledge_env(["An answer."]):
        _main(["index"], monkeypatch)
        capsys.readouterr()
        code, msg = _main(
            ["ask", "creating a lease in Blazar", "--verbose"], monkeypatch
        )
    err = capsys.readouterr().err

    assert code == 0, msg
    assert "candidate(s) reranked, 1 kept" in err
    assert "kept +1.000" in err


def test_ask_without_an_index_says_to_build_one(monkeypatch, capsys, knowledge_env):
    with knowledge_env(["An answer."]):
        code, msg = _main(["ask", "anything"], monkeypatch)

    assert code == 1
    assert "run ccx index" in msg


def _stale_index() -> None:
    """An index built with a model the config does not name."""
    store.replace_knowledge(
        [
            Document(
                source="readthedocs",
                url="https://docs.invalid/leases",
                title="Leases",
                content="Reserve a node with a lease.",
                fetched_at=datetime.now(timezone.utc),
                parents=[
                    Parent(
                        content="Reserve a node with a lease.",
                        chunks=[Chunk(content="a lease", embedding=[1.0, 0.0])],
                    )
                ],
            )
        ],
        "a-model-the-config-does-not-name",
    )


def test_describe_carries_on_without_an_unusable_knowledge_base(
    monkeypatch, capsys, owner_env, describe_env
):
    _stale_index()
    with describe_env([_describe_reply("search"), _describe_reply("answer")]):
        code, msg = _main(
            ["describe", "a skylake node for two hours", "--site", _UC],
            monkeypatch,
        )
    captured = capsys.readouterr()

    assert code == 0, msg
    assert "WARNING: the knowledge base is unusable" in captured.err
    assert "description_id" in captured.out


def test_ask_refuses_an_unusable_knowledge_base(monkeypatch, capsys, knowledge_env):
    with knowledge_env(["An answer."]):
        _stale_index()
        code, msg = _main(["ask", "anything"], monkeypatch)

    assert code == 1
    assert "rebuild it with ccx index" in msg


def _inventory_cli(monkeypatch, replies: list[str]) -> None:
    monkeypatch.setattr(
        "cloud_conversations.cli.main.build_client",
        lambda settings, model=None, temperature=None: FakeModel(replies),
    )


def test_inventory_prints_every_table_and_the_summary(monkeypatch, capsys, owner_env):
    _inventory_cli(monkeypatch, [json.dumps({
        "action": "holdings", "holdings": ["images"], "site": "FAKE@BM",
    })])

    code, msg = _main(
        ["inventory", "my images", "--site", "FAKE@BM"], monkeypatch
    )
    out = capsys.readouterr().out

    assert code == 0, msg or out
    assert "# images" in out
    assert "my-experiment-snapshot  img-snapshot" in out
    assert "1 image at FAKE@BM." in out
    assert "conversation_id:" in out


def test_inventory_narrows_to_the_site_named(monkeypatch, capsys, owner_env):
    _inventory_cli(monkeypatch, [
        json.dumps({"action": "holdings", "holdings": ["leases"]}),
    ])

    _main(["inventory", "my leases", "--site", "FAKE@KVM"], monkeypatch)
    out = capsys.readouterr().out

    assert "## FAKE@KVM" in out
    assert "## FAKE@BM" not in out, "--site is the whole world for this call"


def test_inventory_reports_what_it_cannot_list(monkeypatch, capsys, owner_env):
    _inventory_cli(monkeypatch, [json.dumps({
        "action": "decline",
        "reason": "I cannot read allocations here",
        "suggestions": ["your leases"],
    })])

    code, _ = _main(
        ["inventory", "my allocation", "--site", "FAKE@BM"], monkeypatch
    )
    out = capsys.readouterr().out

    assert code == 1
    assert "Declined: I cannot read allocations here" in out
    assert "  - your leases" in out
    assert "conversation_id:" in out, "the transcript is still readable back"


def test_show_system_key_prints_the_line_instances_trust(monkeypatch, capsys):
    from cloud_conversations.adapter import system_key

    key = system_key.current()

    code, _ = _main(["show-system-key"], monkeypatch)
    lines = capsys.readouterr().out.splitlines()

    assert code == 0
    assert lines[0].startswith(f"{key.fingerprint}  created ")
    assert lines[0].endswith("  current")
    assert lines[1] == f"  ssh-rsa {key.get_base64()} cloud-conversations-system"


def test_show_system_key_prints_a_private_half_by_fingerprint(monkeypatch, capsys):
    from cloud_conversations.adapter import system_key

    old = system_key.current()
    system_key.rotate()

    code, _ = _main(["show-system-key", "--private", old.fingerprint], monkeypatch)

    import io

    import paramiko

    printed = capsys.readouterr().out
    assert code == 0
    assert paramiko.RSAKey.from_private_key(io.StringIO(printed)).fingerprint == old.fingerprint


def test_rotate_system_key_names_the_retired_key_and_its_replacement(monkeypatch, capsys):
    from cloud_conversations.adapter import system_key

    old = system_key.current()

    code, _ = _main(["rotate-system-key"], monkeypatch)
    lines = capsys.readouterr().out.splitlines()

    new = system_key.current()
    assert code == 0
    assert lines == [
        f"retired {old.fingerprint}",
        f"current {new.fingerprint}",
        f"  ssh-rsa {new.get_base64()} cloud-conversations-system",
    ]


def _public_key(tmp_path) -> tuple[str, str, str]:
    """A public key's file, text and fingerprint."""
    import paramiko

    key = paramiko.RSAKey.generate(2048)
    text = f"ssh-rsa {key.get_base64()} me@laptop\n"
    path = tmp_path / "id_rsa.pub"
    path.write_text(text)
    return str(path), text, key.fingerprint


def test_import_key_prints_the_keypair_name_to_pass_as_keypair(monkeypatch, capsys, tmp_path):
    path, _, fingerprint = _public_key(tmp_path)

    code, _ = _main(["import-key", "--name", "laptop", "--public-key", path], monkeypatch)

    (key,) = store.user_keys("chameleon:cli-test")
    assert code == 0
    assert capsys.readouterr().out == f"laptop  {key.keypair_name}  {fingerprint}\n"


def test_show_keys_lists_the_owners_imported_keys(monkeypatch, capsys, tmp_path):
    from cloud_conversations.adapter import user_key

    _, text, fingerprint = _public_key(tmp_path)
    key = user_key.import_key("chameleon:cli-test", "laptop", text)
    store.ensure_user("chameleon:someone-else")
    user_key.import_key("chameleon:someone-else", "desktop", text)

    code, _ = _main(["show-keys"], monkeypatch)

    assert code == 0
    assert capsys.readouterr().out == f"laptop  {key.keypair_name}  {fingerprint}\n"


def test_show_keys_reads_the_config_flag(monkeypatch, tmp_path):
    missing = tmp_path / "missing.toml"

    code, msg = _main(["show-keys", "--config", str(missing)], monkeypatch)

    assert (code, msg) == (1, f"ERROR: no config file at {missing}")


def test_config_names_the_prompt_bundle(monkeypatch, tmp_path):
    from cloud_conversations import bundle

    config = tmp_path / "config.toml"
    config.write_text(f'[service]\nbundle_dir = "{tmp_path / "bundle"}"\n')

    _main(["capabilities", "--config", str(config)], monkeypatch)

    assert bundle.default().root == tmp_path / "bundle"


def test_check_passes_an_imported_key_the_site_does_not_hold_yet(monkeypatch, capsys, tmp_path):
    from cloud_conversations.adapter import user_key

    _, text, _ = _public_key(tmp_path)
    key = user_key.import_key("chameleon:cli-test", "laptop", text)

    _main(["check", "--site", "FAKE@BM", "--keypair", key.keypair_name], monkeypatch)

    assert (
        f"[PASS] {f'keypair {key.keypair_name!r}':<44}"
        "imported key laptop; the first launch here creates it"
    ) in capsys.readouterr().out.splitlines()


def test_the_api_will_not_start_without_the_login_settings(monkeypatch):
    import uvicorn

    monkeypatch.delenv("CC_OIDC_CHAMELEON_ISSUER")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("the API started"))

    code, msg = _main(["api"], monkeypatch)

    assert code == 1
    assert msg == "ERROR: the API needs the login settings: CC_OIDC_CHAMELEON_ISSUER is unset"


def test_the_api_access_log_leaves_out_query_strings(monkeypatch, caplog):
    import uvicorn

    access = logging.getLogger("uvicorn.access")

    def run(app, **kwargs):
        uvicorn.Config(app, **kwargs)
        access.addHandler(caplog.handler)
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "192.0.2.1:5000", "GET", "/auth/callback?code=c&state=s", "1.1", 307,
        )

    monkeypatch.setattr(uvicorn, "run", run)
    try:
        _main(["api"], monkeypatch)
    finally:
        access.removeHandler(caplog.handler)

    assert caplog.messages == ['192.0.2.1:5000 - "GET /auth/callback HTTP/1.1" 307']


def test_the_api_will_not_start_without_the_public_url(monkeypatch):
    import uvicorn

    monkeypatch.delenv("CC_PUBLIC_URL")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("the API started"))

    code, msg = _main(["api"], monkeypatch)

    assert code == 1
    assert msg == "ERROR: the API needs the login settings: CC_PUBLIC_URL is unset"


def test_ccx_makes_its_owner_a_user_on_first_use(monkeypatch, capsys):
    monkeypatch.setenv("CC_OWNER_ID", "chameleon:someone-new")

    code, msg = _main(["describe-fixture", "--name", "baremetal"], monkeypatch)

    assert code == 0, msg
    assert store.get_user("chameleon:someone-new").username == "someone-new"


def test_an_owner_that_names_no_testbed_is_refused(monkeypatch):
    monkeypatch.setenv("CC_OWNER_ID", "cli")

    assert _main(["show-keys"], monkeypatch) == (
        1, "ERROR: CC_OWNER_ID names no testbed; set it to <testbed>:<name>"
    )


def test_ccx_adds_resets_and_deletes_a_local_user(monkeypatch, capsys):
    from cloud_conversations import local_users

    monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
    assert _main(["add-local-user", "--name", "alice"], monkeypatch) == (0, "")
    assert local_users.check("chameleon", "alice", "correct horse battery") == (
        "chameleon:local:alice"
    )

    monkeypatch.setattr("sys.stdin", io.StringIO("a different password\n"))
    _main(["set-local-password", "--name", "alice"], monkeypatch)
    assert local_users.check("chameleon", "alice", "a different password") == (
        "chameleon:local:alice"
    )

    _main(["delete-local-user", "--name", "alice"], monkeypatch)
    assert capsys.readouterr().out.splitlines() == [
        "added chameleon:local:alice",
        "set the password of chameleon:local:alice",
        "deleted chameleon:local:alice",
    ]
    with pytest.raises(store.NotFoundError):
        store.get_user("chameleon:local:alice")
