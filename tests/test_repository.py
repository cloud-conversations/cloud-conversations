"""Reading a repository's setup files from its archive."""

from __future__ import annotations

import pytest

from cloud_conversations.environment import repo
from cloud_conversations.environment.repo import Fact, Repository, RepositoryError
from tests.conftest import NEXT, REPOSITORY

_ARCHIVES = {"github.com": "https://codeload.github.com/{path}/tar.gz/{ref}"}


def _read(ref: str = "HEAD", **over) -> Repository:
    return repo.read(REPOSITORY, ref, _ARCHIVES, **{
        "max_bytes": 1_000_000, "file_chars": 1000, "timeout_s": 1, "user_agent": "test",
        **over,
    })


def test_a_read_keeps_the_setup_files_and_nothing_else(archive):
    archive({
        "requirements.txt": "numpy\n",
        ".github/workflows/ci.yml": "on: push\n",
        "docs/requirements.txt": "sphinx\n",
        "src/train.py": "print('hi')\n",
    })
    assert set(_read().files) == {"requirements.txt", ".github/workflows/ci.yml"}


def test_a_read_reports_the_python_cuda_and_os_its_files_name(archive):
    archive({
        ".python-version": "3.11.4\n",
        "requirements.txt": "torch==2.3.0+cu118\n",
        "Dockerfile": "FROM ubuntu:22.04\n",
    })
    assert _read().facts == {
        "python_version": Fact("3.11", ".python-version"),
        "cuda": Fact("11.8", "requirements.txt"),
        "os_release": Fact("Ubuntu 22.04", "Dockerfile"),
    }


def test_a_read_downloads_the_commit_its_branch_is_at(archive):
    archive({"requirements.txt": "numpy\n"})
    assert _read("next").commit == NEXT


def test_a_read_names_the_hosts_it_can_read():
    with pytest.raises(RepositoryError, match="only repositories at github.com can be read"):
        repo.read("https://gitlab.com/alice/model", "HEAD", _ARCHIVES, max_bytes=1,
                  file_chars=1, timeout_s=1, user_agent="test")


def test_an_archive_over_the_limit_is_not_read(archive):
    archive({"requirements.txt": "numpy\n"})
    with pytest.raises(RepositoryError, match="is larger than 10 bytes"):
        _read(max_bytes=10)
