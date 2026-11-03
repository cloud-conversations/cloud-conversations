"""Carrying out one job."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations.config import Config, load_config
from cloud_conversations.db.store import Job
from cloud_conversations.steps.context import NotYet
from cloud_conversations.worker import chat, runs

log = logging.getLogger(__name__)

BODIES: dict[str, Callable[[Job, Config], Any]] = {
    "exchange": chat.exchange,
    "build": runs.build_experiment,
    "teardown": runs.release,
}


def carry_out(job: Job) -> None:
    """Run a claimed job's body and record its outcome on the job row."""
    config = load_config()
    if job.attempts > config.service.job_attempts:
        _fail(job, f"the worker stopped {job.attempts - 1} times before this job finished")
        return
    try:
        if store.disabled_reason(job.owner_id) is not None:
            raise PermissionError("an admin disabled this account")
        BODIES[job.kind](job, config)
    except NotYet as wait:
        store.defer_job(job.job_id, wait.until)
        _tell(job, wait.notice)
        return
    except Exception as exc:
        log.exception("job %s (%s) failed", job.job_id, job.kind)
        _fail(job, f"{type(exc).__name__}: {exc}")
        return
    store.finish_job(job.job_id)


def _fail(job: Job, failure: str) -> None:
    """Fail a job, telling its chat why."""
    store.finish_job(job.job_id, failure)
    _tell(job, f"That did not work: {failure}")


def _tell(job: Job, text: str) -> None:
    try:
        runs.say(job, text)
    except Exception:
        log.exception("could not report job %s on its thread", job.job_id)
