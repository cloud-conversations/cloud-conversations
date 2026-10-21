"""The workers' Celery app."""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import worker_init

from cloud_conversations import oidc
from cloud_conversations.config import load_config
from cloud_conversations.schema.service import BROKER_URL_ENV, CHAT_QUEUE, RUNS_QUEUE

_config = load_config()
_service = _config.service

app = Celery(
    "cloud_conversations",
    broker=os.environ.get(BROKER_URL_ENV) or _service.broker_url,
    include=["cloud_conversations.worker.tasks"],
)

app.conf.update(
    task_ignore_result=True,
    task_soft_time_limit=_service.job_timeout_s,
    task_time_limit=_service.job_kill_s,
    task_acks_late=True,
    # Seconds before an unacknowledged task is redelivered.
    broker_transport_options={"visibility_timeout": _service.job_kill_s},
    worker_prefetch_multiplier=1,
    task_default_queue=CHAT_QUEUE,
    task_routes={
        "cloud_conversations.worker.tasks.run_exchange": {"queue": CHAT_QUEUE},
        "cloud_conversations.worker.tasks.run_build": {"queue": RUNS_QUEUE},
        "cloud_conversations.worker.tasks.run_release": {"queue": RUNS_QUEUE},
    },
)


@worker_init.connect
def _require_login_settings(**_: object) -> None:
    """Refuse to start a worker that cannot refresh a user's login."""
    try:
        for testbed in _config.logins():
            oidc.client(testbed)
    except oidc.OidcError as exc:
        raise SystemExit(f"ERROR: the worker needs the login settings: {exc}") from exc
