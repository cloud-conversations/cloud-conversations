"""Resolving settings from the defaults, the file, a site, and CLI flags."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cloud_conversations.config import ConfigError, Settings, load_config, owner_id
from cloud_conversations.schema.description import Requirements


def _write(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_no_config_file_leaves_the_built_in_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("CC_CONFIG_FILE")
    monkeypatch.chdir(tmp_path)
    assert load_config().for_site(None) == Settings()


def test_a_site_table_overrides_only_that_site(tmp_path):
    path = _write(
        tmp_path,
        '[defaults]\nssh_user = "cc"\n'
        '[adapters.c.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.c.sites."CHI@UC"]\nkinds = ["bm"]\nssh_user = "ubuntu"\n'
        '[adapters.c.sites."CHI@TACC"]\nkinds = ["bm"]\n',
    )
    config = load_config(path)
    assert config.for_site("CHI@UC", "physical_host").ssh_user == "ubuntu"
    assert config.for_site("CHI@TACC", "physical_host").ssh_user == "cc"


def test_a_site_with_two_kinds_resolves_each_apart(tmp_path):
    path = _write(
        tmp_path,
        '[adapters.c.kinds.bm]\nreservation_kind = "physical_host"\nlease_active_s = 1800\n'
        '[adapters.c.kinds.vm]\nreservation_kind = "flavor_instance"\nlease_active_s = 600\n'
        '[adapters.c.sites."CHI@TACC"]\nkinds = ["vm", "bm"]\n',
    )
    config = load_config(path)
    assert config.for_site("CHI@TACC", "flavor_instance").lease_active_s == 600
    assert config.for_site("CHI@TACC", "physical_host").lease_active_s == 1800


def test_an_unset_cli_flag_does_not_clear_a_configured_value(tmp_path):
    path = _write(tmp_path, '[defaults]\nssh_user = "fromfile"\n')
    assert load_config(path, ssh_user=None).for_site(None).ssh_user == "fromfile"


def test_a_temperature_outside_the_allowed_range_is_refused(tmp_path):
    path = _write(tmp_path, "[defaults]\n")
    with pytest.raises(ConfigError, match="llm_temperature"):
        load_config(path, llm_temperature=5.0)


def test_a_missing_named_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="no config file"):
        load_config(tmp_path / "absent.toml")


def test_an_unknown_key_is_an_error(tmp_path):
    path = _write(tmp_path, "[defaults]\nnot_a_setting = 1\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_an_admin_listed_twice_is_an_error(tmp_path):
    path = _write(tmp_path, '[adapters.chameleon]\nadmins = ["alice", "bob", "alice"]\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "listed more than once: alice" in str(raised.value)


def test_a_blank_admin_is_an_error(tmp_path):
    path = _write(tmp_path, '[adapters.chameleon]\nadmins = [" "]\n')
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert "an admin is blank" in str(raised.value)


def test_an_admin_is_an_admin_only_at_their_testbed(tmp_path):
    path = _write(tmp_path, '[adapters.chameleon]\nadmins = ["alice"]\n[adapters.other]\n')
    config = load_config(path)
    assert config.is_admin("chameleon", "alice")
    assert not config.is_admin("other", "alice")


def test_cc_admins_replaces_each_testbeds_admins(tmp_path, monkeypatch):
    path = _write(tmp_path, '[adapters.chameleon]\nadmins = ["alice"]\n[adapters.other]\n')
    monkeypatch.setenv("CC_ADMINS", "chameleon:bob, other:carol")
    config = load_config(path)
    assert (config.adapters["chameleon"].admins, config.adapters["other"].admins) == (
        ["bob"], ["carol"]
    )


def test_an_empty_cc_admins_leaves_the_files_admins(tmp_path, monkeypatch):
    path = _write(tmp_path, '[adapters.chameleon]\nadmins = ["alice"]\n')
    monkeypatch.setenv("CC_ADMINS", "")
    assert load_config(path).adapters["chameleon"].admins == ["alice"]


def test_a_cc_admin_at_no_configured_testbed_is_an_error(tmp_path, monkeypatch):
    path = _write(tmp_path, "[adapters.chameleon]\n")
    monkeypatch.setenv("CC_ADMINS", "alice")
    with pytest.raises(ConfigError) as raised:
        load_config(path)
    assert str(raised.value) == (
        f"{path} with CC_ADMINS: 'alice' names no configured testbed; write it as "
        "<testbed>:<username>, with one of: chameleon"
    )


def test_a_nonsense_value_is_an_error(tmp_path):
    path = _write(tmp_path, "[defaults]\nlease_active_s = 0\n")
    with pytest.raises(ConfigError):
        load_config(path)


def test_the_committed_config_matches_the_settings_model():
    config = load_config(Path("etc/config.toml"))
    assert [k.reservation_kind for k in config.kinds_at("CHI@TACC")] == [
        "flavor_instance", "physical_host"
    ], "VMs first"


_LAYERED = (
    "[defaults]\n"
    'ssh_user = "from-defaults"\n'
    "lease_active_s = 1\n"
    "probe_s = 1\n"
    "[adapters.c]\n"
    'ssh_user = "from-adapter"\n'
    "lease_active_s = 2\n"
    "[adapters.c.kinds.bm]\n"
    'reservation_kind = "physical_host"\n'
    "lease_active_s = 3\n"
    "instance_active_s = 3\n"
    '[adapters.c.sites."S"]\n'
    'kinds = ["bm"]\n'
    "lease_active_s = 4\n"
)


def test_each_level_overrides_the_one_above(tmp_path):
    s = load_config(_write(tmp_path, _LAYERED)).for_site("S", "physical_host")
    assert s.lease_active_s == 4, "site wins"
    assert s.instance_active_s == 3, "kind wins where the site is silent"
    assert s.ssh_user == "from-adapter", "adapter wins where kind and site are silent"
    assert s.probe_s == 1, "defaults where nothing else sets it"


def test_a_cli_flag_beats_every_level(tmp_path):
    config = load_config(_write(tmp_path, _LAYERED), ssh_user="from-cli")
    assert config.for_site("S", "physical_host").ssh_user == "from-cli"
    assert config.for_site("S", "physical_host").lease_active_s == 4, "the site table survives"


def test_a_site_names_its_adapter(tmp_path):
    config = load_config(_write(tmp_path, _LAYERED))
    assert config.adapter_for("S") == "c"
    assert config.adapter_for("NOPE") is None
    assert config.sites() == {"S": "c"}


def test_an_unknown_kind_is_an_error(tmp_path):
    path = _write(tmp_path, '[adapters.c.sites."S"]\nkinds = ["absent"]\n')
    with pytest.raises(ConfigError, match="absent, which are not defined"):
        load_config(path)


def test_two_kinds_sharing_a_reservation_kind_are_an_error(tmp_path):
    path = _write(
        tmp_path,
        '[adapters.c.kinds.a]\nreservation_kind = "physical_host"\n'
        '[adapters.c.kinds.b]\nreservation_kind = "physical_host"\n'
        '[adapters.c.sites."S"]\nkinds = ["a", "b"]\n',
    )
    with pytest.raises(ConfigError, match="lists a reservation kind more than once"):
        load_config(path)


def test_an_unknown_key_in_a_site_table_is_an_error(tmp_path):
    path = _write(
        tmp_path,
        '[adapters.c.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.c.sites."S"]\nkinds = ["bm"]\nlease_activ_s = 9\n',
    )
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(path)


def test_an_unknown_table_is_an_error(tmp_path):
    path = _write(tmp_path, "[servce]\nport = 9\n")
    with pytest.raises(ConfigError, match=r"unknown key servce$"):
        load_config(path)


def test_a_bad_value_in_a_kind_table_is_an_error_at_load(tmp_path):
    path = _write(
        tmp_path,
        '[adapters.c.kinds.bm]\nreservation_kind = "physical_host"\nlease_active_s = "slow"\n'
        '[adapters.c.sites."S"]\nkinds = ["bm"]\n',
    )
    with pytest.raises(ConfigError, match="lease_active_s"):
        load_config(path)


def test_a_site_under_two_adapters_is_an_error(tmp_path):
    path = _write(
        tmp_path,
        '[adapters.a.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.a.sites."S"]\nkinds = ["bm"]\n'
        '[adapters.b.kinds.bm]\nreservation_kind = "physical_host"\n'
        '[adapters.b.sites."S"]\nkinds = ["bm"]\n',
    )
    with pytest.raises(ConfigError, match='site "S" is declared by both a and b'):
        load_config(path)


def test_the_committed_config_lets_a_bare_metal_build_finish():
    config = load_config(Path("etc/config.toml"))
    baremetal = config.for_site("CHI@TACC", "physical_host")

    assert config.service.job_timeout_s > (
        baremetal.lease_active_s + baremetal.instance_active_s
    )


_RETIRING = (
    '[adapters.c.kinds.vm]\nreservation_kind = "flavor_instance"\n'
    '[adapters.c.sites."KVM@TACC"]\nkinds = ["vm"]\n'
    '[adapters.c.sites."KVM@TACC".retirement]\n'
    "retires_on = 2027-01-15\nwarn_from = 2027-01-01\n"
    'notice = "KVM@TACC retires in January."\nretired_notice = "KVM@TACC has retired."\n'
)


def test_a_site_leaves_the_config_on_the_day_it_retires(tmp_path, monkeypatch):
    config = load_config(_write(tmp_path, _RETIRING))

    monkeypatch.setattr("cloud_conversations.config.today", lambda: date(2027, 1, 14))
    assert config.sites() == {"KVM@TACC": "c"}
    assert config.retired_at("c") == {}

    monkeypatch.setattr("cloud_conversations.config.today", lambda: date(2027, 1, 15))
    assert config.sites() == {}
    assert config.retired_at("c") == {"KVM@TACC": "KVM@TACC has retired."}


def test_only_a_lease_ending_from_warn_from_gets_the_notice(tmp_path):
    config = load_config(_write(tmp_path, _RETIRING))

    def lease(day: int) -> Requirements:
        start = datetime(2026, 12, day, tzinfo=timezone.utc)
        return Requirements(site="KVM@TACC", start_at=start, duration_hours=168)

    assert config.retirement_notice(lease(24)) is None, "ends on December 31"
    assert config.retirement_notice(lease(25)) == "KVM@TACC retires in January."


def test_a_warning_later_than_the_retirement_is_an_error(tmp_path):
    path = _write(tmp_path, _RETIRING.replace("warn_from = 2027-01-01", "warn_from = 2027-02-01"))
    with pytest.raises(ConfigError, match="warn_from is later than retires_on"):
        load_config(path)


_SOURCE = """
[knowledge.sources.readthedocs]
kind = "sphinx"
url = "https://docs.invalid/"
label = "Chameleon Docs"
primary = true
min_chars = 500
exclude = ["index.html"]
"""


def test_an_unknown_source_kind_is_an_error(tmp_path):
    path = _write(
        tmp_path,
        '[knowledge.sources.wiki]\nkind = "mediawiki"\nurl = "https://w.invalid/"\n'
        'label = "Wiki"\n',
    )

    with pytest.raises(ConfigError, match="mediawiki"):
        load_config(path)


def test_a_source_option_belonging_to_another_kind_is_an_error(tmp_path):
    path = _write(
        tmp_path,
        '[knowledge.sources.blog]\nkind = "blog"\nurl = "https://b.invalid"\n'
        'label = "Blog"\nindex = "contents.html"\n',
    )

    with pytest.raises(ConfigError, match="index"):
        load_config(path)


def test_an_overlap_larger_than_its_chunk_is_an_error(tmp_path):
    path = _write(tmp_path, "[knowledge]\nchild_chars = 40\nchild_overlap = 40\n")

    with pytest.raises(ConfigError, match="child_overlap 40 is not smaller"):
        load_config(path)


def test_a_cli_override_leaves_the_knowledge_config_alone(tmp_path):
    path = _write(tmp_path, f'[defaults]\nssh_user = "fromfile"\n{_SOURCE}')

    config = load_config(path, ssh_user="fromcli")

    assert config.for_site(None).ssh_user == "fromcli"
    assert set(config.knowledge.sources) == {"readthedocs"}


def test_ccx_is_the_only_testbeds_cli_user_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("CC_OWNER_ID", raising=False)

    assert owner_id(load_config(_write(tmp_path, "[adapters.one]\n"))) == "one:cli"


def test_ccx_needs_an_owner_named_when_several_testbeds_are_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("CC_OWNER_ID", raising=False)
    config = load_config(_write(tmp_path, "[adapters.one]\n\n[adapters.two]\n"))

    with pytest.raises(ConfigError, match="set CC_OWNER_ID"):
        owner_id(config)


def test_local_login_needs_a_testbed_for_its_users(tmp_path):
    with pytest.raises(ConfigError, match="local_login needs local_users_testbed"):
        load_config(_write(tmp_path, "[service]\nlocal_login = true\n"))
