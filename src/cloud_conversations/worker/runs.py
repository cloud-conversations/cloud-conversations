"""Building an approved description, and releasing a run's resources."""

from __future__ import annotations

import logging
import time
import uuid

import cloud_conversations.db.store as store
from cloud_conversations import audit, releases
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.credentials import user_scope
from cloud_conversations.agent.recover import recover
from cloud_conversations.agent.revision import build_replacement, keypair_for
from cloud_conversations.config import Config
from cloud_conversations.db.store import Job
from cloud_conversations.knowledge.retrieve import usable_knowledge
from cloud_conversations.llm.client import LLMError, build_client
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import reservation_kind_of
from cloud_conversations.schema.runs import RunState
from cloud_conversations.steps.context import finishing_by

log = logging.getLogger(__name__)


def build_experiment(job: Job, config: Config) -> None:
    """Provision the approved version, and recover it if the attempt fails.

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
        if old is not None:
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
                f"the build ran past job_timeout_s ({config.service.job_timeout_s:g}s); "
                "resume or release its run from the chat"
            )

        try:
            client = build_client(settings)
        except LLMError as exc:
            log.warning("run %s failed and cannot be recovered: %s", run_id, exc)
            return
        recover(
            client, owner, run_id, adapter, config, settings, usable_knowledge(config.knowledge)
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
    live = store.live_resources(run_id)
    if not live:
        return
    site = run.site or live[0].site
    project = run.project_id or store.get_thread(job.owner_id, job.thread_id).project_id
    releases.carry_out(
        job.owner_id, request_id,
        credentials.build_adapter(site, config, user_scope(job.owner_id, project)),
    )

    remaining = store.live_resources(run_id)
    if remaining:
        named = ", ".join(f"{r.kind} {r.provider_id} at {r.site}" for r in remaining)
        raise ValueError(f"{len(remaining)} resource(s) still live: {named}")
