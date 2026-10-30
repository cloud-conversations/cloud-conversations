"""Read a repository's setup files from its archive, without running anything in it."""

from __future__ import annotations

import io
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Annotated

from pydantic import AfterValidator

# Files that say what a repository needs, at its top level or in its CI workflows.
_SETUP_FILES = re.compile(
    r"(readme(\.\w+)?|requirements[\w.-]*\.txt|pyproject\.toml|setup\.(py|cfg)|pipfile"
    r"|environment\.ya?ml|\.python-version|package\.json|dockerfile[\w.-]*"
    r"|docker-compose[\w.-]*\.ya?ml|cmakelists\.txt|makefile|cargo\.toml|go\.mod"
    r"|\.github/workflows/[\w.-]+\.ya?ml)",
    re.IGNORECASE,
)
_PYTHON = re.compile(r"\s*(3\.[0-9]+)")
_TORCH_CUDA = re.compile(r"\+cu([0-9]{2,3})\b")
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*(\S+)", re.IGNORECASE | re.MULTILINE)
_CUDA_BASE = re.compile(r"nvidia/cuda:([0-9]+\.[0-9]+)")
_UBUNTU = re.compile(r"ubuntu:?([0-9]{2}\.[0-9]{2})")
_PYTHON_BASE = re.compile(r"^python:(3\.[0-9]+)")
COMMIT = re.compile(r"[0-9a-f]{7,40}")


def _archive_template(value: str) -> str:
    if not value.startswith("https://") or "{path}" not in value or "{ref}" not in value:
        raise ValueError(f"{value!r} is not an https URL with {{path}} and {{ref}} in it")
    return value


# Where a host serves a repository's archive. {path} is the owner and name.
ArchiveUrl = Annotated[str, AfterValidator(_archive_template)]


class RepositoryError(Exception):
    """The repository could not be read."""


class UnreachableError(RepositoryError):
    """The repository's host did not answer."""


@dataclass(frozen=True)
class Fact:
    value: str
    # The file it came from.
    path: str


@dataclass(frozen=True)
class Repository:
    url: str
    ref: str
    commit: str
    # Each setup file's text, cut to file_chars.
    files: dict[str, str]
    # python_version, cuda, and os_release, if a file names them.
    facts: dict[str, Fact]


def read(
    url: str,
    ref: str,
    archive_urls: dict[str, str],
    *,
    max_bytes: int,
    file_chars: int,
    timeout_s: float,
    user_agent: str,
) -> Repository:
    """Download the repository's archive at the commit ref names, and keep its setup files.

    Raises RepositoryError.
    """
    parts = urllib.parse.urlsplit(url)
    template = archive_urls.get(parts.hostname or "")
    if template is None:
        hosts = ", ".join(sorted(archive_urls))
        raise RepositoryError(f"only repositories at {hosts} can be read, not {parts.hostname}")
    known = refs(url, timeout_s=timeout_s, user_agent=user_agent)
    if known is None:
        raise RepositoryError(f"there is no public git repository at {url}")
    commit = ref if COMMIT.fullmatch(ref) else known.get(ref)
    if commit is None:
        raise RepositoryError(f"{url} has no branch or tag {ref}")
    path = parts.path.strip("/").removesuffix(".git")
    archive = template.format(path=urllib.parse.quote(path), ref=commit)
    body = _fetch(archive, timeout_s, user_agent, limit=max_bytes + 1)
    if body is None:
        raise RepositoryError(f"{url} has no public archive at {commit}")
    if len(body) > max_bytes:
        raise RepositoryError(f"{url} is larger than {max_bytes} bytes")
    files = _setup_files(body, file_chars)
    return Repository(url=url, ref=ref, commit=commit, files=files, facts=_facts(files))


def refs(url: str, *, timeout_s: float, user_agent: str) -> dict[str, str] | None:
    """Each branch and tag at url, and HEAD, with its commit.

    Returns None if there is no public repository at url. Raises UnreachableError.
    """
    body = _fetch(
        f"{url.removesuffix('/')}/info/refs?service=git-upload-pack", timeout_s, user_agent
    )
    return None if body is None else _refs(body)


def _refs(body: bytes) -> dict[str, str]:
    found: dict[str, str] = {}
    at = 0
    while at + 4 <= len(body):
        try:
            length = int(body[at : at + 4], 16)
        except ValueError:
            break
        line = body[at + 4 : at + length].split(b"\0")[0].decode().strip()
        at += max(length, 4)
        commit, _, name = line.partition(" ")
        if not COMMIT.fullmatch(commit):
            continue
        if name == "HEAD":
            found["HEAD"] = commit
        for prefix in ("refs/heads/", "refs/tags/"):
            if name.startswith(prefix):
                short = name.removeprefix(prefix)
                if short.endswith("^{}") or short not in found:
                    found[short.removesuffix("^{}")] = commit
    return found


def _fetch(url: str, timeout_s: float, user_agent: str, limit: int = -1) -> bytes | None:
    """The body at url.

    Returns None on an HTTP error. Raises UnreachableError.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as reply:
            return reply.read(limit)
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UnreachableError(f"{url} could not be reached: {exc}") from exc


def _setup_files(archive: bytes, file_chars: int) -> dict[str, str]:
    files: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                # Every path starts with the archive's own top folder.
                _, _, path = member.name.partition("/")
                if not member.isfile() or not _SETUP_FILES.fullmatch(path):
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    files[path] = handle.read(file_chars * 4).decode(errors="replace")[:file_chars]
    except (tarfile.TarError, EOFError) as exc:
        raise RepositoryError(f"the archive is not a gzipped tar: {exc}") from exc
    return files


def _facts(files: dict[str, str]) -> dict[str, Fact]:
    facts: dict[str, Fact] = {}
    if (pinned := _PYTHON.match(files.get(".python-version", ""))) is not None:
        facts["python_version"] = Fact(pinned.group(1), ".python-version")
    for path, text in sorted(files.items()):
        name = path.lower()
        if name.startswith("requirements") and (cu := _TORCH_CUDA.search(text)) is not None:
            facts.setdefault("cuda", Fact(f"{cu.group(1)[:-1]}.{cu.group(1)[-1]}", path))
        if not name.startswith("dockerfile"):
            continue
        for base in _FROM.findall(text):
            if (cuda := _CUDA_BASE.search(base)) is not None:
                facts.setdefault("cuda", Fact(cuda.group(1), path))
            if (ubuntu := _UBUNTU.search(base)) is not None:
                facts.setdefault("os_release", Fact(f"Ubuntu {ubuntu.group(1)}", path))
            if (python := _PYTHON_BASE.match(base)) is not None:
                facts.setdefault("python_version", Fact(python.group(1), path))
    return facts
