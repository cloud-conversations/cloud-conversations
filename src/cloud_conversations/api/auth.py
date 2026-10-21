"""Logging in through a testbed's OIDC provider, or as a local test user."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

import cloud_conversations.db.store as store
from cloud_conversations import crypt, local_users, oidc
from cloud_conversations.api.deps import (
    SESSION_COOKIE,
    config,
    cookie_name,
    no_project,
    public_url,
    session_hash,
)
from cloud_conversations.api.schemas import AuthMethods, LocalLogin, LoginOption
from cloud_conversations.config import LoginClaims, account_id
from cloud_conversations.schema.service import Service

# The cookie for a login in progress at the login provider.
LOGIN_COOKIE = "cc_login"
# The cookie with why the last login was refused.
REFUSED_COOKIE = "cc_refused"

WAITING = "an admin must approve this account before you can use it"
DENIED = "an admin denied access to this account"

_T = TypeVar("_T")

router = APIRouter(prefix="/auth")


@router.get("/login")
def login(testbed: str, next: str = "/") -> RedirectResponse:
    """Send the browser to the testbed's login, then back to next."""
    _claims(testbed)
    service = config().service
    login_client = _oidc(lambda: oidc.client(testbed))
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": login_client.client_id,
        "redirect_uri": _callback_url(),
        "scope": service.oidc_scope,
        "state": state,
        "code_challenge": challenge.rstrip(b"=").decode(),
        "code_challenge_method": "S256",
    })
    response = RedirectResponse(
        f"{_published(service, login_client)['authorization_endpoint']}?{query}"
    )
    pending = {"state": state, "verifier": verifier, "next": _local_path(next), "testbed": testbed}
    _set_cookie(response, LOGIN_COOKIE, crypt.encrypt(json.dumps(pending)), service.login_s)
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Finish a login, or send a refused one to the login page with the reason."""
    try:
        return _finish_login(request, code, state, error)
    except HTTPException as exc:
        response = RedirectResponse(public_url() + "/login")
        _set_cookie(
            response, REFUSED_COOKIE, crypt.encrypt(str(exc.detail)), config().service.login_s
        )
        _delete_cookie(response, LOGIN_COOKIE)
        return response


def _finish_login(
    request: Request, code: str | None, state: str | None, error: str | None
) -> RedirectResponse:
    """Start a session. Raises HTTPException if the login is refused."""
    service = config().service
    pending = _pending_login(request.cookies.get(cookie_name(LOGIN_COOKIE)), service.login_s)
    expired = HTTPException(
        status_code=400, detail="this login expired or did not start here; log in again"
    )
    if pending is None or not hmac.compare_digest(state or "", pending["state"]):
        raise expired
    if error:
        raise HTTPException(status_code=400, detail=f"the login was refused: {error}")
    if not code:
        raise expired

    testbed = pending["testbed"]
    claims_read = _claims(testbed)
    login_client = _oidc(lambda: oidc.client(testbed))
    tokens = _oidc(
        lambda: oidc.exchange_code(
            service, login_client, code, pending["verifier"], _callback_url()
        )
    )
    claims = _oidc(lambda: oidc.userinfo(service, login_client, tokens["access_token"]))
    projects = claims.get(claims_read.projects_claim)
    if not projects:
        raise HTTPException(status_code=403, detail=no_project(config().label(testbed)))

    user_id = account_id(testbed, claims["sub"])
    username = claims[claims_read.username_claim]
    email = claims.get(claims_read.email_claim)
    _refuse_if_disabled(user_id)
    if not config().is_admin(testbed, username):
        _refuse_unless_approved(user_id, username, projects, email)

    session = secrets.token_urlsafe(32)
    expires = _session_expiry(service)
    logged_in_until = oidc.refresh_expiry(tokens)
    store.record_login(
        user_id,
        username,
        projects,
        crypt.encrypt(tokens["refresh_token"]),
        session_hash(session),
        min(expires, logged_in_until) if logged_in_until else expires,
        email=email,
    )
    response = RedirectResponse(public_url() + pending["next"])
    _set_cookie(response, SESSION_COOKIE, session, service.session_s)
    _delete_cookie(response, LOGIN_COOKIE)
    return response


@router.get("/methods", response_model=AuthMethods)
def methods(request: Request, response: Response) -> AuthMethods:
    """The ways to log in, and why the last login here was refused."""
    settings = config()
    refused = _refusal(request.cookies.get(cookie_name(REFUSED_COOKIE)), settings.service.login_s)
    if refused is not None:
        _delete_cookie(response, REFUSED_COOKIE)
    return AuthMethods(
        local=settings.service.local_login,
        logins=[LoginOption(testbed=t, label=settings.label(t)) for t in settings.logins()],
        refused=refused,
    )


@router.post("/local", status_code=204)
def local(attempt: LocalLogin) -> Response:
    """Log a local test user in with their password."""
    service = config().service
    if not service.local_login or service.local_users_testbed is None:
        raise HTTPException(status_code=404, detail="Not Found")
    user_id = local_users.check(service.local_users_testbed, attempt.name, attempt.password)
    if user_id is None:
        raise HTTPException(status_code=401, detail="wrong username or password")
    _refuse_if_disabled(user_id)
    session = secrets.token_urlsafe(32)
    store.start_session(user_id, session_hash(session), _session_expiry(service))
    response = Response(status_code=204)
    _set_cookie(response, SESSION_COOKIE, session, service.session_s)
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    """End the session here, and the login at the user's testbed."""
    service = config().service
    token = request.cookies.get(cookie_name(SESSION_COOKIE))
    caller = store.session_user(session_hash(token)) if token else None
    if token:
        store.end_session(session_hash(token))
    if caller is None or local_users.is_local(caller.user_id) or (
        caller.testbed not in config().logins()
    ):
        response = RedirectResponse(public_url() + "/", status_code=303)
        _delete_cookie(response, SESSION_COOKIE)
        return response
    login_client = _oidc(lambda: oidc.client(caller.testbed))
    query = urllib.parse.urlencode({
        "client_id": login_client.client_id,
        "post_logout_redirect_uri": public_url() + "/",
    })
    response = RedirectResponse(
        f"{_published(service, login_client)['end_session_endpoint']}?{query}", status_code=303
    )
    _delete_cookie(response, SESSION_COOKIE)
    return response


def _claims(testbed: str) -> LoginClaims:
    """The testbed's login claims. Raises 404 if users cannot log in through it."""
    found = config().logins().get(testbed)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no login for testbed {testbed!r}")
    return found


def _refuse_if_disabled(user_id: str) -> None:
    reason = store.disabled_reason(user_id)
    if reason is not None:
        raise HTTPException(status_code=403, detail=f"an admin disabled this account: {reason}")


def _refuse_unless_approved(
    user_id: str, username: str, projects: list[dict[str, Any]], email: str | None
) -> None:
    approval = store.approval(user_id)
    if approval == "denied":
        raise HTTPException(status_code=403, detail=DENIED)
    if approval != "approved" and store.require_approval():
        store.request_approval(user_id, username, projects, email=email)
        raise HTTPException(status_code=403, detail=WAITING)


def _callback_url() -> str:
    return public_url() + "/api/auth/callback"


def _local_path(path: str) -> str:
    """path if it is on this site, else /."""
    return path if path.startswith("/") and not path.startswith("//") else "/"


def _pending_login(cookie: str | None, max_age_s: float) -> dict[str, str] | None:
    if not cookie:
        return None
    try:
        pending: dict[str, str] = json.loads(crypt.decrypt(cookie, max_age_s))
    except crypt.CryptError:
        return None
    return pending


def _refusal(cookie: str | None, max_age_s: float) -> str | None:
    if not cookie:
        return None
    try:
        return crypt.decrypt(cookie, max_age_s)
    except crypt.CryptError:
        return None


def _session_expiry(service: Service) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=service.session_days)


def _set_cookie(response: Response, name: str, value: str, max_age_s: float) -> None:
    response.set_cookie(
        cookie_name(name),
        value,
        max_age=int(max_age_s),
        httponly=True,
        secure=public_url().startswith("https://"),
        samesite="lax",
        path="/",
    )


def _delete_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        cookie_name(name),
        httponly=True,
        secure=public_url().startswith("https://"),
        samesite="lax",
        path="/",
    )


def _published(service: Service, login_client: oidc.Client) -> dict[str, Any]:
    return _oidc(lambda: oidc.endpoints(login_client.issuer, service.oidc_s))


def _oidc(call: Callable[[], _T]) -> _T:
    """Raises 502 if the call fails."""
    try:
        return call()
    except oidc.OidcError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
