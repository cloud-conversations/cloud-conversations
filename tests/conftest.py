"""Shared fixtures. Needs Postgres."""

from __future__ import annotations

import math
import os
import re
import urllib.request
import zlib
from collections.abc import Generator
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import paramiko
import psycopg
import pytest
from alembic import command
from alembic.config import Config as AlembicConfig
from cryptography.fernet import Fernet
from psycopg import sql

from cloud_conversations.adapter.chameleon import sites as capabilities
from cloud_conversations.adapter.chameleon.catalog import PublishedSite
from cloud_conversations.adapter.fake import FakeAdapter, FakeSite, default_fake_sites
from cloud_conversations.adapter.protocol import SiteDescriptor
from cloud_conversations.config import CONFIG_FILE_ENV, Adapter, Config, KindFacts, Settings
from cloud_conversations.knowledge import retrieve
from cloud_conversations.schema.description import Check, Requirement, Requirements
from cloud_conversations.schema.images import ImageRules

TEST_DB_URL: str = os.environ.get(
    "CC_TEST_DATABASE_URL",
    "postgresql://dev:dev@localhost:5432/cloudconversations_test",
)

_REPO_ROOT = Path(__file__).parent.parent

BM_SITE = "FAKE@BM"
KVM_SITE = "FAKE@KVM"

BARE_METAL = [KindFacts(reservation_kind="physical_host")]
VMS = [KindFacts(reservation_kind="flavor_instance")]

# One kind table per reservation kind.
KINDS = {
    "baremetal": {"reservation_kind": "physical_host"},
    "virtual": {"reservation_kind": "flavor_instance"},
}


def _create_test_database() -> None:
    name = TEST_DB_URL.rsplit("/", 1)[-1]
    admin = TEST_DB_URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", [name]
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    os.environ["DATABASE_URL"] = TEST_DB_URL
    _create_test_database()
    import importlib

    import cloud_conversations.db.conn

    importlib.reload(cloud_conversations.db.conn)
    command.upgrade(alembic_config(), "head")


def alembic_config() -> AlembicConfig:
    """Alembic, pointed at the test database's migrations."""
    cfg = AlembicConfig(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


@pytest.fixture(autouse=True)
def truncate_tables(apply_migrations: None) -> Generator[None, None, None]:
    """The suite is unsafe to run twice concurrently."""
    with psycopg.connect(TEST_DB_URL, autocommit=True) as conn:
        conn.execute(
            """
            TRUNCATE
                chat.jobs,
                chat.messages,
                chat.threads,
                knowledge.chunks,
                knowledge.parents,
                knowledge.documents,
                knowledge.builds,
                chat.conversations,
                experiments.run_resources,
                experiments.run_events,
                experiments.runs,
                experiments.experiment_descriptions,
                auth.system_keys,
                auth.user_keys,
                auth.sessions,
                auth.refresh_tokens,
                chat.announcement,
                audit.decisions,
                audit.testbed_calls,
                auth.users
            RESTART IDENTITY CASCADE
            """
        )
    yield


_SECRET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def secret_key(monkeypatch) -> None:
    """A key to encrypt under."""
    monkeypatch.setenv("CC_SECRET_KEY", _SECRET_KEY)


# The login settings every test runs with.
LOGIN = {
    "CC_OIDC_CHAMELEON_ISSUER": "https://auth.example.org/realms/chameleon",
    "CC_OIDC_CHAMELEON_CLIENT_ID": "cloud-conversations-test",
    "CC_OIDC_CHAMELEON_CLIENT_SECRET": "client-secret",
    "CC_PUBLIC_URL": "http://page.example.org",
}


# Headers the page sends as Alice or Bob after logging in.
ALICE = {"Cookie": "cc_session=alice-session", "Origin": LOGIN["CC_PUBLIC_URL"]}
BOB = {"Cookie": "cc_session=bob-session", "Origin": LOGIN["CC_PUBLIC_URL"]}


# The image and login user at the fake sites.
FAKE_SETTINGS = {"ssh_user": "cc", "image_name": "CC-Ubuntu24.04", "os_release": "Ubuntu 24.04"}

# The config file a test reads unless it names its own.
_TEST_CONFIG = "[defaults]\n" + "".join(f'{k} = "{v}"\n' for k, v in FAKE_SETTINGS.items()) + f"""
[service]
bundle_dir = "{_REPO_ROOT / "bundle"}"
local_users_testbed = "chameleon"

[adapters.chameleon]
label = "Chameleon"

[adapters.chameleon.login]

[adapters.chameleon.kinds.baremetal]
reservation_kind = "physical_host"

[adapters.chameleon.kinds.virtual]
reservation_kind = "flavor_instance"

[adapters.chameleon.sites."CHI@UC"]
kinds = ["baremetal"]

[adapters.chameleon.sites."CHI@TACC"]
kinds = ["virtual", "baremetal"]

[adapters.chameleon.sites."KVM@TACC"]
kinds = ["virtual"]
"""


@pytest.fixture(scope="session")
def _test_config_file(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("config") / "config.toml"
    path.write_text(_TEST_CONFIG)
    return path


@pytest.fixture(autouse=True)
def fresh_config(monkeypatch, _test_config_file: Path) -> None:
    """Every test reads the test config file afresh."""
    from cloud_conversations import bundle
    from cloud_conversations.api.deps import config

    monkeypatch.setenv(CONFIG_FILE_ENV, str(_test_config_file))
    config.cache_clear()
    bundle.default.cache_clear()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from cloud_conversations.api.app import app

    return TestClient(app)


@pytest.fixture()
def dispatched(monkeypatch) -> list[tuple[str, str]]:
    """Every task dispatch, recorded."""
    from cloud_conversations.worker import tasks

    sent: list[tuple[str, str]] = []
    for name, task in (
        ("exchange", tasks.run_exchange),
        ("build", tasks.run_build),
        ("teardown", tasks.run_release),
    ):
        monkeypatch.setattr(
            task, "delay", lambda job_id, name=name: sent.append((name, job_id))
        )
    return sent


@pytest.fixture(autouse=True)
def login_settings(monkeypatch) -> None:
    """The login settings, and no admins, from the environment."""
    for env, value in LOGIN.items():
        monkeypatch.setenv(env, value)
    monkeypatch.delenv("CC_ADMINS", raising=False)


@pytest.fixture()
def alice_is_an_admin(monkeypatch) -> None:
    monkeypatch.setenv("CC_ADMINS", "chameleon:alice")


@pytest.fixture(autouse=True)
def users(owner_a: str, owner_b: str) -> None:
    """Both owners are users, with no login."""
    import cloud_conversations.db.store as store

    store.ensure_user(owner_a)
    store.ensure_user(owner_b)


@pytest.fixture()
def logged_in(owner_a: str, owner_b: str) -> None:
    """Both owners have logged in, each a member of CHI-000001, with session <username>-session."""
    import cloud_conversations.db.store as store
    from cloud_conversations import crypt
    from cloud_conversations.api.deps import session_hash

    for owner in (owner_a, owner_b):
        username = owner.partition(":")[2]
        store.record_login(
            owner,
            username,
            [{"id": "CHI-000001", "nickname": ""}],
            crypt.encrypt("a refresh token"),
            session_hash(f"{username}-session"),
            datetime.now(timezone.utc) + timedelta(days=1),
            email=None,
        )


@pytest.fixture()
def owner_a() -> str:
    return "chameleon:alice"


@pytest.fixture()
def owner_b() -> str:
    return "chameleon:bob"


# Collapsed waits.
FAST = Config(
    defaults=Settings(
        poll_interval_s=0.01,
        delete_poll_interval_s=0.01,
        read_retry_s=0.01,
        ssh_poll_interval_s=0.01,
        boot_retry_s=0.01,
    ).model_copy(update=FAKE_SETTINGS)
)


PUBLISHED_CONFIG = Config(
    defaults=Settings(),
    adapters={"chameleon": Adapter.model_validate({
        "images": ImageRules(
            prefix="CC-",
            arch_markers={"aarch64": ["arm64", "aarch64"]},
            supported_properties={"chameleon-supported": "true"},
        ),
        "ssh_user": "cc",
        "image_name": "CC-Ubuntu24.04",
        "os_release": "Ubuntu 24.04",
        "instance_network": "sharednet1",
        "floating_ip_pool": "public",
        "identity_provider": "chameleon",
        "federation_protocol": "openid",
        "project_domain": "chameleon",
        "kinds": {
            "baremetal": {"reservation_kind": "physical_host", "baremetal_flavor": "baremetal"},
            "virtual": {"reservation_kind": "flavor_instance", "max_lease_hours": 4320},
        },
        "sites": {
            "CHI@UC": {"kinds": ["baremetal"]},
            "CHI@TACC": {"kinds": ["virtual", "baremetal"]},
            "KVM@TACC": {"kinds": ["virtual"]},
        },
    })},
)


# The sites the stubbed index publishes.
_PUBLISHED = {
    "CHI@UC": PublishedSite(
        uid="uc",
        auth_url="https://chi.uc.invalid:5000/v3",
        node_catalog_url="https://api.invalid/sites/uc/clusters/chameleon/nodes",
    ),
    "CHI@TACC": PublishedSite(
        uid="tacc",
        auth_url="https://chi.tacc.invalid:5000/v3",
        node_catalog_url="https://api.invalid/sites/tacc/clusters/chameleon/nodes",
    ),
    "KVM@TACC": PublishedSite(
        uid="kvm",
        auth_url="https://kvm.tacc.invalid:5000/v3",
        node_catalog_url="https://api.invalid/sites/kvm/clusters/chameleon/nodes",
    ),
}


@pytest.fixture(scope="session")
def _rsa_keys() -> list[paramiko.RSAKey]:
    return [paramiko.RSAKey.generate(2048) for _ in range(3)]


@pytest.fixture(autouse=True)
def _premade_rsa_keys(monkeypatch, _rsa_keys: list[paramiko.RSAKey]) -> None:
    """Hand out pre-made RSA keys in place of new ones."""
    keys = iter(_rsa_keys)
    monkeypatch.setattr(
        paramiko.RSAKey, "generate", staticmethod(lambda bits, progress_func=None: next(keys))
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    real = urllib.request.urlopen

    def guarded(url, *args, **kwargs):
        target = url if isinstance(url, str) else url.full_url
        if urlsplit(target).hostname not in ("127.0.0.1", "localhost"):
            raise AssertionError(f"test reached the network: {target}")
        return real(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded)


@pytest.fixture(autouse=True)
def _no_clouds_yaml(monkeypatch, tmp_path):
    for name in [n for n in os.environ if n.startswith("OS_")]:
        monkeypatch.delenv(name)
    empty = tmp_path / "empty-clouds.yaml"
    empty.write_text("clouds: {}\n")
    monkeypatch.setenv("OS_CLIENT_CONFIG_FILE", str(empty))
    monkeypatch.setenv("OS_CLIENT_SECURE_FILE", str(empty))


@pytest.fixture(autouse=True)
def _no_reference_api(monkeypatch):
    monkeypatch.setattr(
        capabilities, "_published", lambda index_url, timeout_s: _PUBLISHED
    )


# The day tests take as today.
TODAY = date(2026, 10, 9)


@pytest.fixture(autouse=True)
def _today(monkeypatch):
    monkeypatch.setattr("cloud_conversations.config.today", lambda: TODAY)


@pytest.fixture()
def fake_adapter() -> FakeAdapter:
    return FakeAdapter(sites=default_fake_sites(), config=FAST)


FAKE_DIMENSIONS = 64


class FakeEmbedder:
    """Bag-of-words vectors over a fixed number of buckets."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        counts = [0.0] * FAKE_DIMENSIONS
        for word in _words(text):
            counts[zlib.crc32(word.encode()) % FAKE_DIMENSIONS] += 1.0
        length = math.sqrt(sum(c * c for c in counts)) or 1.0
        return [c / length for c in counts]


class FakeReranker:
    def score(self, query: str, texts: list[str]) -> list[float]:
        wanted = set(_words(query))
        if not wanted:
            return [0.0] * len(texts)
        return [len(wanted & set(_words(t))) / len(wanted) for t in texts]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@pytest.fixture()
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture()
def reranker() -> FakeReranker:
    return FakeReranker()


@pytest.fixture()
def open_base(monkeypatch, embedder: FakeEmbedder, reranker: FakeReranker):
    """Builds a knowledge base whose embedder and reranker are the fakes."""
    monkeypatch.setattr(
        retrieve, "_load_models", lambda knowledge: (embedder, reranker)
    )
    return retrieve.Retriever


@pytest.fixture()
def bm_requirements() -> Requirements:
    """Bare-metal requirements matching the FAKE@BM catalog."""
    return Requirements(
        duration_hours=2,
        items=[
            Requirement(key="node_type", value="compute_skylake", source="stated"),
            Requirement(key="count", value=1, source="default"),
            Requirement(key="image_name", value="CC-Ubuntu24.04", source="default"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
        ],
    )


@pytest.fixture()
def kvm_requirements() -> Requirements:
    """KVM requirements matching the FAKE@KVM catalog."""
    return Requirements(
        duration_hours=2,
        items=[
            Requirement(key="flavor", value="m1.large", source="stated"),
            Requirement(key="count", value=1, source="default"),
            Requirement(key="image_name", value="CC-Ubuntu24.04", source="default"),
            Requirement(key="os_release", value="Ubuntu 24.04", source="default"),
        ],
    )


@pytest.fixture()
def bm_rubric() -> list[Check]:
    """A rubric the FAKE@BM catalog satisfies."""
    return [
        Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq",
              from_requirement="node_type"),
        Check(id="cpus", kind="cpu_count", expected=24, comparator="gte",
              from_requirement="node_type"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
    ]


@pytest.fixture()
def kvm_rubric() -> list[Check]:
    return [
        Check(id="cpus", kind="cpu_count", expected=4, comparator="gte",
              from_requirement="flavor"),
        Check(id="os", kind="os_release", expected="Ubuntu 24.04", comparator="prefix",
              from_requirement="os_release"),
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
    ]


@pytest.fixture()
def guidance(tmp_path, monkeypatch):
    """Use a copy of the committed bundle whose Chameleon guidance is `stages`."""
    import shutil

    from cloud_conversations import bundle

    def _use(stages: dict[str, str]) -> None:
        root = tmp_path / "bundle"
        shutil.copytree(_REPO_ROOT / "bundle", root)
        shutil.rmtree(root / "testbeds" / "chameleon" / "guidance")
        for stage, text in stages.items():
            path = root / "testbeds" / "chameleon" / "guidance" / f"{stage}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        copied = bundle.Bundle(root, {"chameleon": "Chameleon"})
        monkeypatch.setattr(bundle, "default", lambda: copied)

    return _use


class LoggedOut(FakeAdapter):
    """A FakeAdapter with no login."""

    def sites_with_login(self) -> list[str]:
        return []


def logged_out(*descriptors: SiteDescriptor) -> LoggedOut:
    return LoggedOut(sites={d.site: FakeSite(descriptor=d) for d in descriptors}, config=FAST)
