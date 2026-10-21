"""Celery tasks."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations.config import Config, load_config
from cloud_conversations.db.store import Job
from cloud_conversations.worker import chat, runs
from cloud_conversations.worker.app import app

log = logging.getLogger(__name__)


@app.task(name="cloud_conversations.worker.tasks.run_exchange", max_retries=0)
def run_exchange(job_id: str) -> None:
    """Route one message to its agent and run it."""
    _carry_out(job_id, chat.exchange)


@app.task(name="cloud_conversations.worker.tasks.run_build", max_retries=0)
def run_build(job_id: str) -> None:
    """Provision an approved description, recovering a failed attempt."""
    _carry_out(job_id, runs.build_experiment)


@app.task(name="cloud_conversations.worker.tasks.run_release", max_retries=0)
def run_release(job_id: str) -> None:
    _carry_out(job_id, runs.release)


def _carry_out(job_id: str, body: Callable[[Job, Config], Any]) -> None:
    """Run one job's body and record its outcome on the job row."""
    job = store.start_job(uuid.UUID(job_id))
    if job is None:
        log.warning("job %s is not queued; skipping it", job_id)
        return
    try:
        if store.disabled_reason(job.owner_id) is not None:
            raise PermissionError("an admin disabled this account")
        body(job, load_config())
    except Exception as exc:
        log.exception("job %s (%s) failed", job.job_id, job.kind)
        failure = f"{type(exc).__name__}: {exc}"
        store.finish_job(job.job_id, failure)
        _say_so(job, failure)
        return
    store.finish_job(job.job_id)


def _say_so(job: Job, failure: str) -> None:
    """Put a job's failure on its thread."""
    try:
        store.append_message(
            job.thread_id, "assistant", "notice",
            f"That did not work: {failure}", {"job_id": str(job.job_id)},
            owner_id=job.owner_id,
        )
    except Exception:
        log.exception("could not report job %s on its thread", job.job_id)
