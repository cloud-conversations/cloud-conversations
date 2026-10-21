"""Who the caller is, and the settings the routes read."""

from __future__ import annotations

import functools
import hashlib
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request

import cloud_conversations.db.store as store
from cloud_conversations import local_users, oidc
from cloud_conversations.adapter.credentials import CredentialsError, access_token
from cloud_conversations.config import Config, ConfigError, load_config

log = logging.getLogger(__name__)

# The cookie with the caller's session token.
SESSION_COOKIE = "cc_session"

PUBLIC_URL_ENV = "CC_PUBLIC_URL"

STALE_JOB = "the worker stopped before this job finished. Try it again."


def no_project(label: str) -> str:
    return f"your {label} account belongs to no project; join one, then log in again"


@functools.lru_cache(maxsize=1)
def config() -> Config:
    """The config file, read once per process."""
    return load_config()


def public_url() -> str:
    """Where the page is served.

    Raises ConfigError if CC_PUBLIC_URL is unset or not a web address.
    """
    url = os.environ.get(PUBLIC_URL_ENV, "")
    if not url:
        raise ConfigError(f"{PUBLIC_URL_ENV} is unset")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"{PUBLIC_URL_ENV} is not an http or https address")
    return url.rstrip("/")


def cookie_name(name: str) -> str:
    """name, with the __Host- prefix behind an https page."""
    return f"__Host-{name}" if public_url().startswith("https://") else name


def same_origin(request: Request) -> None:
    """Refuse a request that changes state, sent from a page on another origin. Raises 403."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    page = urllib.parse.urlsplit(public_url())
    if request.headers.get("origin") != f"{page.scheme}://{page.netloc}":
        raise HTTPException(status_code=403, detail="this request came from another site")


def session_hash(token: str) -> str:
    """What the database stores in place of a session token."""
    return hashlib.sha256(token.encode()).hexdigest()


def user(request: Request) -> store.User:
    """The logged-in caller. Raises 401 without a live session."""
    token = request.cookies.get(cookie_name(SESSION_COOKIE))
    found = store.session_user(session_hash(token)) if token else None
    if found is None:
        raise HTTPException(status_code=401, detail="log in first")
    if not local_users.is_local(found.user_id) and found.testbed not in config().logins():
        store.end_sessions(found.user_id)
        raise HTTPException(
            status_code=401,
            detail=f"logging in through {config().label(found.testbed)} is turned off",
        )
    service = config().service
    for job in store.expire_stale_jobs(found.user_id, service.job_kill_s, STALE_JOB):
        store.append_message(
            job.thread_id, "assistant", "notice", f"That did not work: {STALE_JOB}",
            {"job_id": str(job.job_id)}, owner_id=found.user_id,
        )
    if (
        found.checked_at is not None
        and datetime.now(timezone.utc) - found.checked_at > timedelta(seconds=service.login_check_s)
        and store.claim_check(found.user_id, service.login_check_s)
    ):
        return _recheck(found)
    return found


def _recheck(found: store.User) -> store.User:
    """Raises 401 if the login provider refuses the login."""
    settings = config()
    claims_read = settings.logins()[found.testbed]
    service = settings.service
    try:
        claims = oidc.userinfo(
            service, oidc.client(found.testbed), access_token(found.user_id, service)
        )
    except CredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except oidc.OidcError as exc:
        log.warning("could not recheck %s with the login provider: %s", found.user_id, exc)
        return found
    projects = claims.get(claims_read.projects_claim)
    if not projects:
        store.end_sessions(found.user_id)
        raise HTTPException(status_code=401, detail=no_project(settings.label(found.testbed)))
    return store.update_login(found.user_id, claims[claims_read.username_claim], projects)


def owner(caller: store.User = Depends(user)) -> str:
    """The identity every store call is scoped to."""
    return caller.user_id


def admin(caller: store.User = Depends(user)) -> store.User:
    """The caller, if config lists them as an admin of their testbed. Raises 403 otherwise."""
    if not config().is_admin(caller.testbed, caller.username):
        raise HTTPException(status_code=403, detail="only an admin may do that")
    return caller
