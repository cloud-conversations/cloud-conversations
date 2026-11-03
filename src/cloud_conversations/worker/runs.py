"""Building an approved description, and releasing a run's resources."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.credentials import user_scope
from cloud_conversations.agent.recover import recover
from cloud_conversations.agent.revision import build_replacement, keypair_for
from cloud_conversations.config import Config
from cloud_conversations.db.store import Job, Run
from cloud_conversations.knowledge.retrieve import usable_knowledge
from cloud_conversations.llm.client import LLMError, build_client
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import Requirements, reservation_kind_of
from cloud_conversations.schema.runs import RunState
from cloud_conversations.steps.context import finishing_by

log = logging.getLogger(__name__)

# States of a run that is being built.
_UNDERWAY = frozenset(
    {RunState.planning, RunState.provisioning, RunState.configuring, RunState.validating}
)

LATE = (
    "This build was due at {due:%Y-%m-%d %H:%M} UTC and did not start in time. "
    "Nothing was reserved. Approve the version again to build it."
)

LEASE_ENDED = (
    "This build's lease ended while the build was stopped. "
    "Release its run, or approve the version again."
)


def build_experiment(job: Job, config: Config) -> None:
    """Provision the approved version, and recover it if the attempt fails.

    A job claimed again resumes the run it started.
    Raises ValueError if the run it replaces still holds anything.
    """
    owner = job.owner_id
    description_id = uuid.UUID(str(job.payload["description_id"]))
    version = int(job.payload["version"])
    replaced = job.payload.get("replaces_run_id")
    old = store.get_run(owner, uuid.UUID(str(replaced))) if replaced is not None else None
    desc = store.get_description(owner, description_id, version)
    site = desc.requirements.site
    if not site:
        raise ValueError(f"description {description_id} v{version} names no site")

    started = time.monotonic()
    current = _current_run(job, description_id)
    if current is not None and current.state == RunState.abandoned:
        return
    notice = _late_notice(job, current, desc.requirements, config.service.build_grace_s)
    if notice is not None:
        if current is not None and current.state in _UNDERWAY:
            sequential.stop(current.run_id, notice)
        say(job, notice)
        return

    thread = store.get_thread(owner, job.thread_id)
    keypair = keypair_for(owner, old, site) if old is not None else thread.keypair_at(site)
    if keypair is None:
        raise ValueError(f"the chat has no keypair at {site}")
    user = user_scope(owner, old.project_id if old is not None else thread.project_id)
    adapter = credentials.build_adapter(site, config, user)
    settings = config.for_site(site, reservation_kind_of(desc.requirements.items))
    # The job's time limit, less one SSH call.
    stop_by = started + config.service.job_timeout_s - settings.install_call_s
    request_id = uuid.UUID(str(job.payload["request_id"]))
    with audit.acting(owner, request_id), finishing_by(stop_by):
        if current is not None:
            run_id = current.run_id
            if current.state in _UNDERWAY:
                if job.attempts > 1:
                    say(job, "The build was interrupted, and is resuming.")
                sequential.resume(run_id, adapter, settings)
        elif old is not None:
            _release(job, config, old.run_id, request_id)
            run_id = build_replacement(
                owner, old, version, str(job.payload["change"]), keypair, adapter, config
            )
        else:
            run_id = sequential.provision(
                owner_id=owner,
                description_id=description_id,
                version=version,
                project_id=user.project,
                keypair=keypair,
                adapter=adapter,
                site=site,
                settings=settings,
            )
        if store.get_run(owner, run_id).state == RunState.passed:
            return
        if time.monotonic() >= stop_by:
            raise TimeoutError(
                f"the build ran past job_timeout_s ({config.service.job_timeout_s:g}s). "
                "Release its run, or approve the version again"
            )

        try:
            client = build_client(settings)
        except LLMError as exc:
            log.warning("run %s failed and cannot be recovered: %s", run_id, exc)
            return
        recover(
            client, owner, run_id, adapter, config, settings, usable_knowledge(config.knowledge)
        )


def _current_run(job: Job, description_id: uuid.UUID) -> Run | None:
    """The run this job started.

    Abandons a run the job made but did not plan.
    """
    current = store.newest_run_since(
        job.owner_id, description_id, job.started_at or job.created_at
    )
    if current is not None and current.plan is None:
        sequential.abandon(current.run_id)
        return None
    return current


def _late_notice(
    job: Job, current: Run | None, requirements: Requirements, grace_s: float
) -> str | None:
    """What to tell the user if the build may not continue."""
    if current is not None and current.state in _UNDERWAY and _lease_ended(current.run_id):
        return LEASE_ENDED
    due = max(job.created_at, requirements.start_at or job.created_at)
    if datetime.now(timezone.utc) - due <= timedelta(seconds=grace_s):
        return None
    if current is not None and (current.state == RunState.passed or _holds(current.run_id)):
        return None
    return LATE.format(due=due)


def _holds(run_id: uuid.UUID) -> bool:
    return bool(store.live_resources(run_id) or store.planned_resources(run_id))


def _lease_ended(run_id: uuid.UUID) -> bool:
    now = datetime.now(timezone.utc)
    return any(
        r.kind == "lease" and r.expires_at is not None and r.expires_at <= now
        for r in store.live_resources(run_id)
    )


def say(job: Job, text: str) -> None:
    """Put a notice on the job's chat."""
    store.append_message(
        job.thread_id, "assistant", "notice", text, {"job_id": str(job.job_id)},
        owner_id=job.owner_id,
    )


def release(job: Job, config: Config) -> None:
    """Raises ValueError naming whatever is still live afterwards."""
    _release(
        job, config, uuid.UUID(str(job.payload["run_id"])),
        uuid.UUID(str(job.payload["request_id"])),
    )


def _release(job: Job, config: Config, run_id: uuid.UUID, request_id: uuid.UUID) -> None:
    """Release the run the user confirmed releasing.

    Raises ValueError naming whatever is still live afterwards.
    """
    run = store.get_run(job.owner_id, run_id)  # authorises
    sites = [r.site for r in store.live_resources(run_id)]
    sites += [p.site for p in store.planned_resources(run_id)]
    if not sites:
        return
    site = run.site or sites[0]
    project = run.project_id or store.get_thread(job.owner_id, job.thread_id).project_id
    releases.carry_out(
        job.owner_id, request_id,
        credentials.build_adapter(site, config, user_scope(job.owner_id, project)),
    )

    remaining = store.live_resources(run_id)
    if remaining:
        named = ", ".join(f"{r.kind} {r.provider_id} at {r.site}" for r in remaining)
        raise ValueError(f"{len(remaining)} resource(s) still live: {named}")
