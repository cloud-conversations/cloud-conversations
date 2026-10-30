"""Looking up a description's packages, images and git refs before it is stored."""

from __future__ import annotations

import urllib.error
import urllib.request
from email.message import Message

import pytest

from cloud_conversations.environment.registries import Lookups
from tests.conftest import COMMIT, refs_reply


def _lookups() -> Lookups:
    return Lookups(
        pypi_url="https://pypi.org/pypi",
        npm_registry_url="https://registry.npmjs.org",
        docker_registry_url="https://registry-1.docker.io",
        timeout_s=1,
        user_agent="test",
    )


class _Reply:
    def __init__(self, body: bytes) -> None:
        self.status, self._body, self.headers = 200, body, Message()

    def read(self, size: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


@pytest.fixture()
def published(monkeypatch):
    """Answer each URL from a table of bodies, and 404 for the rest."""
    bodies: dict[str, bytes] = {}

    def urlopen(request, timeout):
        url = request.full_url
        if url not in bodies:
            raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)
        return _Reply(bodies[url])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return bodies


def test_a_package_pypi_does_not_have_is_named(published):
    published["https://pypi.org/pypi/numpy/json"] = b"{}"
    items = [
        ("pip_package", "numpy"),
        ("pip_package", "nosuch"),
    ]
    assert _lookups().missing(items) == ["PyPI has no package nosuch"]


def test_a_repository_on_a_branch_is_told_the_branch_s_commit(published):
    url = "https://github.com/octocat/Hello-World"
    published[f"{url}/info/refs?service=git-upload-pack"] = refs_reply(
        {"HEAD": COMMIT, "refs/heads/master": COMMIT}
    )
    item = ("repository", {"url": url, "ref": "master", "path": "hw"})
    assert _lookups().missing([item]) == [
        f"the repository ref master is a branch or tag; set the ref, and the expected value "
        f"of its git_head check, to its commit {COMMIT}"
    ]


def test_a_registry_that_does_not_answer_gives_no_reason(monkeypatch):
    def down(request, timeout):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    item = ("pip_package", "numpy")
    assert _lookups().missing([item]) == []


def test_an_image_is_looked_up_with_the_token_its_registry_asks_for(monkeypatch):
    manifest = "https://registry-1.docker.io/v2/library/nginx/manifests/1.27"
    token = "https://auth.example.org/token?service=registry&scope=repository%3Alibrary%2Fnginx%3Apull"
    challenge = Message()
    challenge["WWW-Authenticate"] = (
        'Bearer realm="https://auth.example.org/token",service="registry",'
        'scope="repository:library/nginx:pull"'
    )

    def urlopen(request, timeout):
        if request.full_url == token:
            return _Reply(b'{"token": "t"}')
        if request.full_url == manifest and request.get_header("Authorization") == "Bearer t":
            raise urllib.error.HTTPError(manifest, 404, "Not Found", Message(), None)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", challenge, None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    item = ("container", {"image": "nginx:1.27"})
    assert _lookups().missing([item]) == ["nginx:1.27 is not published"]


def test_an_image_on_another_registry_is_looked_up_there(published):
    published["https://ghcr.io/v2/alice/model/manifests/1.0"] = b""
    assert _lookups().missing([("container", {"image": "ghcr.io/alice/model:1.0"})]) == []


def test_an_answer_is_kept_for_the_next_round(monkeypatch):
    asked: list[str] = []

    def urlopen(request, timeout):
        asked.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    lookups = _lookups()
    for _ in range(2):
        assert lookups.missing([("pip_package", "nosuch")]) == ["PyPI has no package nosuch"]
    assert asked == ["https://pypi.org/pypi/nosuch/json"]

