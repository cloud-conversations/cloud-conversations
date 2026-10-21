"""The HTTP API. Reads and writes rows, and queues slow work."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.adapter import credentials, system_key, user_key
from cloud_conversations.adapter.credentials import UserScope
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.api import auth
from cloud_conversations.api.deps import admin, config, owner, same_origin, user
from cloud_conversations.api.schemas import (
    ActiveExperimentOut,
    AdminSettingsOut,
    AdminSettingsPatch,
    AnnouncementOut,
    Approval,
    EventOut,
    FeedbackDetailOut,
    FeedbackIn,
    FeedbackOut,
    FeedbackPage,
    JobOut,
    KeyDeleted,
    KeyImport,
    KeyOut,
    KeypairOut,
    LimitOut,
    Limits,
    ListedFeedbackOut,
    MeOut,
    MessageOut,
    NewAnnouncement,
    NewMessage,
    Posted,
    ProfileOut,
    ProfilePatch,
    QuotaOut,
    Release,
    ResourceOut,
    RunOut,
    RunSummary,
    SiteOutcome,
    SystemKeyOut,
    ThreadListed,
    ThreadOut,
    ThreadPatch,
    ThreadSummary,
    TrashedThread,
    UserDetailOut,
    UserPatch,
    UsersPage,
    UserSummaryOut,
    VersionOut,
)
from cloud_conversations.db.store import Decision, Description, Message, Run, Thread
from cloud_conversations.schema.description import (
    Requirements,
    ReservationKind,
    reservation_kind_of,
)
from cloud_conversations.schema.service import NEW_THREAD_TITLE, QUOTAS
from cloud_conversations.worker import tasks

NEEDS_KEYPAIR = (
    "Before I can build this I need the key you will log in with at {site}. "
    "Name a keypair you already have there, or import a public key."
)

app = FastAPI(
    title="cloud-conversations", description=__doc__, dependencies=[Depends(same_origin)]
)
app.include_router(auth.router)


@app.exception_handler(store.NotFoundError)
def _not_found(request: Request, exc: store.NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(store.ConflictError)
def _conflict(request: Request, exc: store.ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/me", response_model=MeOut)
def me(caller: store.User = Depends(user)) -> MeOut:
    is_admin = config().is_admin(caller.testbed, caller.username)
    return MeOut(
        testbed=config().label(caller.testbed),
        username=caller.username,
        projects=caller.projects,
        admin=is_admin,
        waiting_accounts=(
            store.waiting_users() if is_admin and store.require_approval() else 0
        ),
    )


@app.get("/announcement", response_model=AnnouncementOut | None)
def get_announcement(_: store.User = Depends(user)) -> AnnouncementOut | None:
    current = store.announcement()
    return AnnouncementOut.model_validate(current) if current else None


@app.put("/admin/announcement", response_model=AnnouncementOut)
def post_announcement(posted: NewAnnouncement, _: store.User = Depends(admin)) -> AnnouncementOut:
    store.post_announcement(posted.text)
    return AnnouncementOut.model_validate(store.announcement())


@app.delete("/admin/announcement", status_code=204)
def clear_announcement(_: store.User = Depends(admin)) -> Response:
    store.clear_announcement()
    return Response(status_code=204)


@app.get("/profile", response_model=ProfileOut)
def get_profile(caller: store.User = Depends(user)) -> ProfileOut:
    return _profile(caller)


@app.patch("/profile", response_model=ProfileOut)
def patch_profile(patch: ProfilePatch, caller: store.User = Depends(user)) -> ProfileOut:
    """Set the defaults new chats start with."""
    changes = patch.model_dump(exclude_unset=True)
    if "default_project_id" in changes:
        if changes["default_project_id"] is None:
            raise HTTPException(status_code=422, detail="name a project")
        _require_project(caller, changes["default_project_id"])
    changes |= _one_keypair(
        caller, changes, "default_ssh_key_ref", "default_site_keypairs"
    )
    defaults: list[tuple[str, ReservationKind, str]] = [
        ("default_vm_site", "flavor_instance", "VMs"),
        ("default_baremetal_site", "physical_host", "bare metal"),
    ]
    for field, kind, what in defaults:
        site = changes.get(field)
        if site is not None and site not in _sites_offering(caller.testbed, kind):
            raise HTTPException(status_code=422, detail=f"{site} offers no {what}")
    store.update_user(caller.user_id, **changes)
    return _profile(dataclasses.replace(caller, **changes))


def _profile(caller: store.User) -> ProfileOut:
    return ProfileOut(
        testbed=config().label(caller.testbed),
        username=caller.username,
        projects=caller.projects,
        default_project_id=caller.default_project_id,
        default_ssh_key_ref=caller.default_ssh_key_ref,
        default_site_keypairs=caller.default_site_keypairs,
        keys=[_key_out(k) for k in store.user_keys(caller.user_id)],
        sites=config().sites_at(caller.testbed),
        default_vm_site=caller.default_vm_site,
        default_baremetal_site=caller.default_baremetal_site,
        vm_sites=_sites_offering(caller.testbed, "flavor_instance"),
        baremetal_sites=_sites_offering(caller.testbed, "physical_host"),
        quotas=_quotas(caller),
        active_experiments=[
            ActiveExperimentOut.model_validate(e)
            for e in store.active_experiments(caller.user_id)
        ],
    )


def _sites_offering(testbed: str, kind: ReservationKind) -> list[str]:
    return [
        site
        for site in config().sites_at(testbed)
        if any(k.reservation_kind == kind for k in config().kinds_at(site))
    ]


def _quotas(account: store.User) -> list[QuotaOut]:
    with store.transaction() as conn:
        used = store.usage(account.user_id, conn)
    counts = {
        "max_messages_per_day": used.messages_today,
        "max_builds_per_user": used.builds_running,
        "max_active_experiments_per_user": used.active_experiments,
    }
    defaults = _defaults()
    return [
        QuotaOut(
            name=name,
            used=counts[name],
            limit=getattr(account, name) or defaults[name],
            default=defaults[name],
            override=getattr(account, name),
        )
        for name in QUOTAS
    ]


def _defaults() -> dict[str, int]:
    """Each limit that applies to everyone."""
    service = config().service
    return {q: v or getattr(service, q) for q, v in store.everyone_limits().items()}


def _limit(account: store.User, quota: str) -> int:
    limit: int = getattr(account, quota) or _defaults()[quota]
    return limit


@app.get("/profile/keypairs", response_model=list[KeypairOut])
def list_keypairs(site: str, caller: store.User = Depends(user)) -> list[KeypairOut]:
    """The caller's keypairs at one site, read as them."""
    try:
        adapter = credentials.build_adapter(site, config(), _scope(caller))
        keypairs = adapter.list_keypairs(site)
    except (credentials.CredentialsError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [KeypairOut(name=k.name) for k in keypairs]


@app.get("/admin/users", response_model=UsersPage)
def list_users(
    page: int = Query(default=1, ge=1),
    size: int | None = None,
    _: store.User = Depends(admin),
) -> UsersPage:
    sizes = config().service.user_page_sizes
    size = sizes[0] if size is None else size
    if size not in sizes:
        raise HTTPException(
            status_code=422, detail=f"size must be one of {', '.join(map(str, sizes))}"
        )
    found = store.user_summaries(limit=size + 1, offset=(page - 1) * size)
    return UsersPage(
        users=[_summary_out(u) for u in found[:size]],
        more=len(found) > size,
        size=size,
        sizes=sizes,
    )


def _summary_out(u: store.UserSummary) -> UserSummaryOut:
    return UserSummaryOut(**{
        **dataclasses.asdict(u),
        "testbed": config().label(u.testbed),
        "admin": config().is_admin(u.testbed, u.username),
    })


@app.get("/admin/users/{user_id}", response_model=UserDetailOut)
def get_user(user_id: str, _: store.User = Depends(admin)) -> UserDetailOut:
    return _user_detail(user_id)


@app.patch("/admin/users/{user_id}", response_model=UserDetailOut)
def patch_user(
    user_id: str, patch: UserPatch, caller: store.User = Depends(admin)
) -> UserDetailOut:
    if patch.disabled and not patch.reason:
        raise HTTPException(status_code=422, detail="give a reason for disabling the account")
    if patch.disabled and user_id == caller.user_id:
        raise HTTPException(status_code=422, detail="you cannot disable your own account")
    if patch.disabled and patch.reason:
        store.disable_user(user_id, patch.reason)
    elif patch.disabled is False:
        store.enable_user(user_id)
    if patch.approval == "approved":
        store.approve_user(user_id)
    elif patch.approval == "denied":
        store.deny_user(user_id)
    store.update_user(user_id, **patch.model_dump(include=set(QUOTAS), exclude_unset=True))
    return _user_detail(user_id)


@app.get("/admin/settings", response_model=AdminSettingsOut)
def get_settings(_: store.User = Depends(admin)) -> AdminSettingsOut:
    return _settings_out()


@app.patch("/admin/settings", response_model=AdminSettingsOut)
def patch_settings(
    patch: AdminSettingsPatch, _: store.User = Depends(admin)
) -> AdminSettingsOut:
    if patch.require_approval is not None:
        store.set_require_approval(patch.require_approval)
    store.set_everyone_limits(**patch.model_dump(include=set(QUOTAS), exclude_unset=True))
    return _settings_out()


def _settings_out() -> AdminSettingsOut:
    service = config().service
    return AdminSettingsOut(
        require_approval=store.require_approval(),
        limits=[
            LimitOut(name=q, default=getattr(service, q), override=v)
            for q, v in store.everyone_limits().items()
        ],
    )


@app.delete("/admin/users/{user_id}/sessions", status_code=204)
def end_user_sessions(user_id: str, _: store.User = Depends(admin)) -> Response:
    store.get_user(user_id)
    store.end_sessions(user_id)
    return Response(status_code=204)


def _user_detail(user_id: str) -> UserDetailOut:
    found = store.user_summaries(user_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"No user {user_id!r}")
    return UserDetailOut(
        **_summary_out(found[0]).model_dump(), quotas=_quotas(store.get_user(user_id))
    )


@app.get("/admin/feedback", response_model=FeedbackPage)
def list_feedback(
    page: int = Query(default=1, ge=1),
    size: int | None = None,
    _: store.User = Depends(admin),
) -> FeedbackPage:
    sizes = config().service.feedback_page_sizes
    size = sizes[0] if size is None else size
    if size not in sizes:
        raise HTTPException(
            status_code=422, detail=f"size must be one of {', '.join(map(str, sizes))}"
        )
    found = store.listed_feedback(size + 1, (page - 1) * size)
    return FeedbackPage(
        feedback=[ListedFeedbackOut.model_validate(f) for f in found[:size]],
        more=len(found) > size,
        size=size,
        sizes=sizes,
    )


@app.get("/admin/feedback/{feedback_id}", response_model=FeedbackDetailOut)
def get_feedback(feedback_id: uuid.UUID, _: store.User = Depends(admin)) -> FeedbackDetailOut:
    return FeedbackDetailOut.model_validate(store.feedback_detail(feedback_id))


@app.get("/admin/system-keys", response_model=list[SystemKeyOut])
def list_system_keys(_: store.User = Depends(admin)) -> list[SystemKeyOut]:
    return [_system_key_out(k) for k in store.system_keys()]


@app.post("/admin/system-keys/rotate", response_model=SystemKeyOut, status_code=201)
def rotate_system_key(_: store.User = Depends(admin)) -> SystemKeyOut:
    """Retire the current system key. New instances get its replacement."""
    rotated = store.system_key(system_key.rotate().fingerprint)
    return _system_key_out(rotated)


def _system_key_out(key: store.SystemKey) -> SystemKeyOut:
    return SystemKeyOut(
        fingerprint=key.fingerprint,
        created_at=key.created_at,
        retired_at=key.retired_at,
        authorized_key=system_key.authorized_key(system_key.load(key.private_key)),
    )


@app.get("/threads", response_model=list[ThreadListed])
def list_threads(who: str = Depends(owner)) -> list[ThreadListed]:
    activity = store.thread_activity(who)
    return [
        ThreadListed.model_validate(t).model_copy(update={"activity": activity.get(t.thread_id)})
        for t in store.list_threads(who)
    ]


@app.post("/threads", response_model=ThreadSummary, status_code=201)
def create_thread(caller: store.User = Depends(user)) -> ThreadSummary:
    """Open a chat in the caller's default project, with their default keypair."""
    return ThreadSummary.model_validate(
        store.create_thread(
            caller.user_id,
            NEW_THREAD_TITLE,
            project_id=caller.project(),
            ssh_key_ref=caller.default_ssh_key_ref,
            site_keypairs=caller.default_site_keypairs,
        )
    )


@app.get("/threads/{thread_id}", response_model=ThreadOut)
def get_thread(thread_id: uuid.UUID, who: str = Depends(owner)) -> ThreadOut:
    thread = store.get_thread(who, thread_id)
    messages = store.read_messages(who, thread_id)
    versions: list[Description] = []
    runs: list[Run] = []
    for lineage in _lineages(thread, messages):
        versions += store.description_versions(who, lineage)
        runs += store.runs_for_description(who, lineage)
    build_running = store.any_build_running()
    asked = [uuid.UUID(m.payload["request_id"]) for m in messages if m.kind == "release"]
    return ThreadOut(
        thread=ThreadSummary.model_validate(thread),
        messages=[MessageOut.model_validate(m) for m in messages],
        versions=[VersionOut.model_validate(d) for d in versions],
        jobs=[
            JobOut.model_validate(j).model_copy(update={
                "waiting": build_running and j.state == "queued" and j.kind != "exchange"
            })
            for j in store.unfinished_jobs(who, thread_id)
        ],
        runs=[RunSummary.model_validate(r) for r in runs],
        limits=Limits.model_validate(config().for_site(None)),
        decisions=store.answers(who, asked),
        feedback=[FeedbackOut.model_validate(f) for f in store.thread_feedback(who, thread_id)],
    )


def _lineages(thread: Thread, messages: list[Message]) -> list[uuid.UUID]:
    """Every description this chat has worked on, oldest first."""
    named = [m.description_id for m in messages if m.description_id is not None]
    if thread.description_id is not None:
        named.append(thread.description_id)
    return list(dict.fromkeys(named))


@app.post("/threads/{thread_id}/feedback", response_model=FeedbackOut)
def post_feedback(
    thread_id: uuid.UUID, given: FeedbackIn, who: str = Depends(owner)
) -> FeedbackOut:
    saved = store.save_feedback(
        who, thread_id, given.message_id, given.run_id, given.rating, given.comment
    )
    return FeedbackOut.model_validate(saved)


@app.patch("/threads/{thread_id}", response_model=ThreadSummary)
def patch_thread(
    thread_id: uuid.UUID, patch: ThreadPatch, caller: store.User = Depends(user)
) -> ThreadSummary:
    """Change a chat's keypair, or the project it builds in."""
    if patch.project_id is not None:
        _require_project(caller, patch.project_id)
    changes = patch.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="name a keypair or a project")
    changes |= _one_keypair(caller, changes, "ssh_key_ref", "site_keypairs")
    return ThreadSummary.model_validate(
        store.update_thread(caller.user_id, thread_id, **changes)
    )


@app.delete("/threads/{thread_id}", status_code=204)
def delete_thread(thread_id: uuid.UUID, who: str = Depends(owner)) -> Response:
    """Move a chat to the trash."""
    store.delete_thread(who, thread_id)
    return Response(status_code=204)


@app.get("/trash", response_model=list[TrashedThread])
def list_trash(who: str = Depends(owner)) -> list[TrashedThread]:
    return [
        TrashedThread.model_validate(t) for t in store.trash(who, config().service.restore_days)
    ]


@app.post("/trash/{thread_id}/restore", response_model=ThreadSummary)
def restore_thread(thread_id: uuid.UUID, who: str = Depends(owner)) -> ThreadSummary:
    return ThreadSummary.model_validate(
        store.restore_thread(who, thread_id, config().service.restore_days)
    )


@app.post("/threads/{thread_id}/messages", response_model=Posted, status_code=202)
def post_message(
    thread_id: uuid.UUID, posted: NewMessage, caller: store.User = Depends(user)
) -> Posted:
    """Record the message, queue the exchange it starts."""
    who = caller.user_id
    thread = store.get_thread(who, thread_id)
    text = posted.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="the message is empty")

    with store.transaction() as conn:
        used = store.usage(who, conn)
        most = _limit(caller, "max_messages_per_day")
        if used.messages_today >= most and used.next_message_at is not None:
            after = used.next_message_at.astimezone(timezone.utc)
            raise HTTPException(
                status_code=429,
                detail=f"you have sent {most} messages in the last 24 hours; "
                       f"send another after {after:%Y-%m-%d %H:%M} UTC",
            )
        job = store.enqueue_job(
            who, thread_id, "exchange", {"text": text}, conn=conn
        )
        message = store.append_message(
            thread_id, "user", "user", text, owner_id=who, conn=conn
        )
        if thread.title == NEW_THREAD_TITLE:
            store.update_thread(
                who, thread_id,
                title=text[: config().service.thread_title_chars],
                conn=conn,
            )
    tasks.run_exchange.delay(str(job.job_id))
    return Posted(
        message=MessageOut.model_validate(message), job=JobOut.model_validate(job)
    )


@app.post("/keys", response_model=KeyOut, status_code=201)
def import_key(imported: KeyImport, who: str = Depends(owner)) -> KeyOut:
    """Import a public key. Each site gets its keypair at the first launch there."""
    try:
        key = user_key.import_key(who, imported.name, imported.public_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _key_out(key)


@app.delete("/keys/{name}", response_model=KeyDeleted)
def delete_key(
    name: str, at_sites: bool = False, caller: store.User = Depends(user)
) -> KeyDeleted:
    """Forget an imported key, and with at_sites, delete its keypair at the testbed's sites."""
    key = store.delete_user_key(caller.user_id, name)
    if not at_sites:
        return KeyDeleted(name=key.name, keypair_name=key.keypair_name, sites=[])

    resources = [
        {"kind": "keypair", "provider_id": key.keypair_name, "site": site}
        for site in config().sites_at(caller.testbed)
    ]
    request_id = releases.propose(
        caller.user_id, "user", "delete_keypair", "the user deleted an imported key",
        resources, via="web",
    )
    releases.confirm(caller.user_id, request_id, "web")
    with audit.acting(caller.user_id, request_id):
        sites = [_delete_keypair_at(r["site"], key.keypair_name, caller) for r in resources]
    return KeyDeleted(name=key.name, keypair_name=key.keypair_name, sites=sites)


def _delete_keypair_at(site: str, keypair_name: str, caller: store.User) -> SiteOutcome:
    try:
        adapter = credentials.build_adapter(site, config(), _scope(caller))
        deleted = adapter.delete_keypair(site, keypair_name)
    except (credentials.CredentialsError, AdapterError, ValueError) as exc:
        return SiteOutcome(site=site, outcome="failed", error=str(exc))
    return SiteOutcome(site=site, outcome="deleted" if deleted else "absent")


def _key_out(key: store.UserKey) -> KeyOut:
    return KeyOut(
        name=key.name,
        keypair_name=key.keypair_name,
        fingerprint=user_key.fingerprint(key.public_key),
    )


def _scope(caller: store.User) -> UserScope:
    """The caller, in the project their new chats build in."""
    return UserScope(caller.user_id, caller.testbed, caller.project())


def _latest_start(site: str, requirements: Requirements) -> datetime:
    """The latest lease start a build can wait for and still finish."""
    settings = config().for_site(site, reservation_kind_of(requirements.items))
    after_start = settings.lease_active_s + settings.instance_active_s + settings.ssh_s
    return datetime.now(timezone.utc) + timedelta(
        seconds=config().service.job_timeout_s - after_start
    )


def _one_keypair(
    caller: store.User, changes: dict, imported: str, at_sites: str
) -> dict[str, None]:
    """Clear one keypair field if changes sets the other.

    Raises 422 if the keypairs at sites do not name one at each site of the testbed.
    """
    named = changes.get(at_sites)
    if named is not None:
        sites = config().sites_at(caller.testbed)
        if set(named) != set(sites) or not all(name.strip() for name in named.values()):
            raise HTTPException(
                status_code=422, detail=f"pick one keypair at each of {', '.join(sites)}"
            )
        return {imported: None}
    return {at_sites: None} if changes.get(imported) is not None else {}


def _require_project(caller: store.User, project_id: str) -> None:
    """Raises 422 if the caller is not in the project."""
    if not caller.in_project(project_id):
        raise HTTPException(
            status_code=422,
            detail=f"you are not in {config().label(caller.testbed)} project {project_id}",
        )


@app.post("/threads/{thread_id}/builds", response_model=JobOut, status_code=202)
def post_build(
    thread_id: uuid.UUID, approval: Approval, caller: store.User = Depends(user)
) -> JobOut:
    """Approve a version, and queue the build that spends a lease on it."""
    return _queue_build(caller, thread_id, approval.description_id, approval.version, None)


def _queue_build(
    caller: store.User,
    thread_id: uuid.UUID,
    description_id: uuid.UUID,
    version: int,
    replacing: Decision | None,
) -> JobOut:
    """Queue a build of the version."""
    who = caller.user_id
    thread = store.get_thread(who, thread_id)
    description = store.get_description(who, description_id, version)
    site = description.requirements.site
    if not site:
        raise HTTPException(
            status_code=422,
            detail=f"description {description_id} v{version} "
                   "names no site, so nothing can be built from it",
        )
    start = description.requirements.start_at
    latest = _latest_start(site, description.requirements)
    if start is not None and start > latest:
        hours = max((latest - datetime.now(timezone.utc)).total_seconds(), 0) / 3600
        raise HTTPException(
            status_code=422,
            detail=f"the lease starts {start:%Y-%m-%d %H:%M} UTC, too late for a build to "
                   f"finish; ask for a start within {hours:.1f} hours of now",
        )
    if thread.keypair_at(site) is None:
        store.append_message(
            thread_id, "assistant", "notice", NEEDS_KEYPAIR.format(site=site),
            {"needs": "ssh_key_ref"},
            owner_id=who,
            description_id=description_id,
            description_version=version,
        )
        raise HTTPException(
            status_code=409, detail=f"pick the keypair to log in to {site} with, then approve"
        )

    payload: dict[str, object] = {"description_id": str(description_id), "version": version}
    said = f"Approved version {version}; building it at {site}."
    with store.transaction() as conn:
        if replacing is None:
            payload["request_id"] = str(audit.approved(
                who, "web", description_id=description_id, version=version,
                thread_id=thread_id, conn=conn,
            ))
        else:
            releases.confirm(who, replacing.request_id, "web", conn)
            payload |= {
                "replaces_run_id": str(replacing.run_id),
                "request_id": str(replacing.request_id),
                "change": replacing.detail["change"],
            }
            said = (
                f"Confirmed releasing {releases.named(replacing.resources)}; building "
                f"version {version} at {site}."
            )
        used = store.usage(who, conn)
        if used.builds_running >= _limit(caller, "max_builds_per_user"):
            raise HTTPException(
                status_code=409,
                detail=f"you have {used.builds_running} builds running, the most allowed; "
                       "wait for one to finish",
            )
        active = used.active_experiments
        if replacing is not None and replacing.run_id and store.live_resources(replacing.run_id):
            active -= 1
        if active >= _limit(caller, "max_active_experiments_per_user"):
            raise HTTPException(
                status_code=409,
                detail=f"you have {active} active experiments, the most "
                       "allowed; release one from your profile",
            )
        job = store.enqueue_job(who, thread_id, "build", payload, conn=conn)
        store.append_message(
            thread_id, "user", "run", said,
            owner_id=who,
            description_id=description_id,
            description_version=version,
            conn=conn,
        )
    tasks.run_build.delay(str(job.job_id))
    return JobOut.model_validate(job)


@app.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: uuid.UUID, who: str = Depends(owner)) -> RunOut:
    run = store.get_run(who, run_id)
    return RunOut(
        description_version=run.description_version,
        state=run.state,
        resume_count=run.resume_count,
        results=run.results,
        connection=run.connection,
        failure=run.failure,
        live_resources=[
            ResourceOut.model_validate(r) for r in store.live_resources(run_id)
        ],
    )


@app.get("/runs/{run_id}/events", response_model=list[EventOut])
def get_run_events(run_id: uuid.UUID, who: str = Depends(owner)) -> list[EventOut]:
    return [EventOut.model_validate(e) for e in store.read_events(who, run_id)]


@app.post("/runs/{run_id}/teardown", response_model=JobOut, status_code=202)
def post_teardown(run_id: uuid.UUID, release: Release, who: str = Depends(owner)) -> JobOut:
    """Queue the release the user confirmed."""
    run = store.get_run(who, run_id)
    thread = store.thread_for_description(who, run.description_id)
    if thread is None:
        raise HTTPException(status_code=404, detail=f"No experiment holds run {run_id}")

    held = releases.held(run_id)
    if not held:
        raise HTTPException(status_code=409, detail=f"run {run_id} holds nothing to release")
    seen = {(r.kind, r.provider_id, r.site) for r in release.resources}
    if seen != {(r["kind"], r["provider_id"], r["site"]) for r in held}:
        raise HTTPException(
            status_code=409,
            detail=f"the run holds {releases.named(held)}. Review the run and release it again",
        )

    with store.transaction() as conn:
        request_id = releases.propose(
            who, "user", "release", "the user asked to release the run", held,
            via="web", run_id=run_id, thread_id=thread.thread_id, conn=conn,
        )
        releases.confirm(who, request_id, "web", conn)
        job = store.enqueue_job(
            who, thread.thread_id, "teardown",
            {"run_id": str(run_id), "request_id": str(request_id)}, conn=conn,
        )
    tasks.run_release.delay(str(job.job_id))
    return JobOut.model_validate(job)


@app.post("/releases/{request_id}/confirm", response_model=JobOut, status_code=202)
def confirm_release(request_id: uuid.UUID, caller: store.User = Depends(user)) -> JobOut:
    who = caller.user_id
    proposal, run, thread_id = _proposal(who, request_id)
    if proposal.version is not None:
        return _queue_build(caller, thread_id, run.description_id, proposal.version, proposal)

    with store.transaction() as conn:
        releases.confirm(who, request_id, "web", conn)
        job = store.enqueue_job(
            who, thread_id, "teardown",
            {"run_id": str(run.run_id), "request_id": str(request_id)}, conn=conn,
        )
    tasks.run_release.delay(str(job.job_id))
    return JobOut.model_validate(job)


@app.post("/releases/{request_id}/decline", status_code=204)
def decline_release(request_id: uuid.UUID, who: str = Depends(owner)) -> Response:
    """Refuse the agent's proposal, and post the version it wrote."""
    proposal, run, thread_id = _proposal(who, request_id)
    releases.decline(who, request_id, "web")
    if proposal.version is not None:
        store.append_message(
            thread_id, "assistant", "description",
            f"Version {proposal.version} is saved. Approve it to build it.",
            owner_id=who,
            description_id=run.description_id,
            description_version=proposal.version,
        )
    return Response(status_code=204)


def _proposal(who: str, request_id: uuid.UUID) -> tuple[Decision, Run, uuid.UUID]:
    """The agent's proposal, its run, and its chat.

    Raises 404 unless the agent proposed the release in one of the caller's chats.
    """
    proposal = store.decision_request(who, request_id)[0]
    if proposal.actor != "agent" or proposal.run_id is None or proposal.thread_id is None:
        raise HTTPException(status_code=404, detail=f"No proposed release {request_id}")
    return proposal, store.get_run(who, proposal.run_id), proposal.thread_id
