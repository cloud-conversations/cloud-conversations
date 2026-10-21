"""ccx's login at each site."""

from __future__ import annotations

import pytest

from cloud_conversations.adapter.chameleon.logins import cli_logins
from cloud_conversations.adapter.credentials import CredentialsError
from tests.conftest import PUBLISHED_CONFIG

_OIDC_CLOUDS_YAML = """
clouds:
  chameleon:
    auth_type: v3chameleonoidc
    auth:
      auth_url: https://chi.uc.chameleoncloud.org:5000/v3
      identity_provider: chameleon
      protocol: openid
      client_id: chi-cli-device-token
      discovery_endpoint: https://auth.example.org/realms/chameleon/.well-known/openid-configuration
      project_id: proj-abc
    region_name: CHI@UC
"""


def _clouds_yaml(tmp_path, body: str) -> str:
    p = tmp_path / "clouds.yaml"
    p.write_text(body)
    return str(p)


def _by_site(monkeypatch, tmp_path, body: str):
    monkeypatch.setenv("OS_CLIENT_CONFIG_FILE", _clouds_yaml(tmp_path, body))
    return cli_logins(PUBLISHED_CONFIG)


def _plugin(login):
    return login.auth("https://site.invalid:5000/v3")


def test_a_ccauth_clouds_yaml_entry_logs_in_with_ccauth_in_its_project(monkeypatch, tmp_path):
    monkeypatch.delenv("OS_CLOUD", raising=False)

    plugin = _plugin(_by_site(monkeypatch, tmp_path, _OIDC_CLOUDS_YAML)["CHI@UC"])

    assert type(plugin).__name__ == "ChameleonDeviceAuth"
    assert plugin.project_id == "proj-abc"


def test_an_application_credential_entry_carries_its_secret_to_redact(monkeypatch, tmp_path):
    monkeypatch.delenv("OS_CLOUD", raising=False)

    login = _by_site(monkeypatch, tmp_path, _TWO_SITE_CLOUDS_YAML)["CHI@UC"]

    assert _plugin(login).auth_methods[0].application_credential_id == "cred-uc"
    assert login.secrets == ("shh-uc",)


_TWO_SITE_CLOUDS_YAML = """
clouds:
  uc:
    auth_type: v3applicationcredential
    auth:
      auth_url: https://chi.uc.chameleoncloud.org:5000/v3
      application_credential_id: cred-uc
      application_credential_secret: shh-uc
    region_name: CHI@UC
  tacc:
    auth_type: v3applicationcredential
    auth:
      auth_url: https://chi.tacc.chameleoncloud.org:5000/v3
      application_credential_id: cred-tacc
      application_credential_secret: shh-tacc
    region_name: CHI@TACC
  elsewhere:
    auth_type: v3applicationcredential
    auth:
      auth_url: https://example.invalid:5000/v3
      application_credential_id: cred-x
      application_credential_secret: shh-x
    region_name: NOPE@NOWHERE
"""


def test_every_configured_site_in_clouds_yaml_gets_its_own_login(monkeypatch, tmp_path):
    monkeypatch.delenv("OS_CLOUD", raising=False)
    by_site = _by_site(monkeypatch, tmp_path, _TWO_SITE_CLOUDS_YAML)

    assert sorted(by_site) == ["CHI@TACC", "CHI@UC"]
    assert _plugin(by_site["CHI@TACC"]).auth_methods[0].application_credential_id == "cred-tacc"


def test_os_cloud_narrows_to_one_site(monkeypatch, tmp_path):
    monkeypatch.setenv("OS_CLOUD", "tacc")
    assert list(_by_site(monkeypatch, tmp_path, _TWO_SITE_CLOUDS_YAML)) == ["CHI@TACC"]


def test_an_empty_os_cloud_uses_every_site(monkeypatch, tmp_path):
    monkeypatch.setenv("OS_CLOUD", "")
    assert sorted(_by_site(monkeypatch, tmp_path, _TWO_SITE_CLOUDS_YAML)) == [
        "CHI@TACC", "CHI@UC"
    ]


def test_an_unknown_cloud_is_an_error_not_a_traceback(monkeypatch, tmp_path):
    monkeypatch.setenv("OS_CLOUD", "no-such-cloud")
    with pytest.raises(CredentialsError) as exc:
        _by_site(monkeypatch, tmp_path, _OIDC_CLOUDS_YAML)
    assert str(exc.value) == "cloud 'no-such-cloud' is not in clouds.yaml"


def test_a_cloud_naming_an_unconfigured_site_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OS_CLOUD", "elsewhere")
    with pytest.raises(CredentialsError) as exc:
        _by_site(monkeypatch, tmp_path, _TWO_SITE_CLOUDS_YAML)
    assert "names no configured site" in str(exc.value)


def test_an_application_credential_in_the_environment_logs_in_at_each_sites_keystone(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OS_CLOUD", raising=False)
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_ID", "env-cred")
    monkeypatch.setenv("OS_APPLICATION_CREDENTIAL_SECRET", "env-secret")

    by_site = _by_site(monkeypatch, tmp_path, "clouds: {}\n")

    assert sorted(by_site) == sorted(PUBLISHED_CONFIG.sites())
    plugin = by_site["CHI@UC"].auth("https://chi.uc.invalid:5000/v3")
    assert plugin.auth_url == "https://chi.uc.invalid:5000/v3"
    assert plugin.auth_methods[0].application_credential_id == "env-cred"
    assert by_site["CHI@UC"].secrets == ("env-secret",)
