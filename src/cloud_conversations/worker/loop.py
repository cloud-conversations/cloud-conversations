"""The worker's loop."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

import cloud_conversations.db.store as store
from cloud_conversations import oidc
from cloud_conversations.config import Config
from cloud_conversations.db.store import Job
from cloud_conversations.schema.service import TESTBED_KINDS, Service
from cloud_conversations.worker import leases, tasks

log = logging.getLogger(__name__)


def require_login_settings(config: Config) -> None:
    """Raises SystemExit if the worker cannot refresh a user's login."""
    try:
        for testbed in config.logins():
            oidc.client(testbed)
    except oidc.OidcError as exc:
        raise SystemExit(f"ERROR: the worker needs the login settings: {exc}") from exc


def run(kinds: tuple[str, ...], config: Config) -> None:
    """Carry out jobs of these kinds, one at a time, until SIGTERM.

    Watches the leases between jobs.
    """
    service = config.service
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    watched = time.monotonic() - service.lease_watch_s
    while not stopping.is_set():
        try:
            worked = work_once(kinds, service)
            if not worked and time.monotonic() - watched >= service.lease_watch_s:
                watched = time.monotonic()
                leases.watch(config)
        except psycopg.OperationalError as exc:
            log.warning("cannot reach the database: %s", exc)
            worked = False
        if not worked:
            stopping.wait(service.job_poll_s)


def work_once(kinds: tuple[str, ...], service: Service) -> bool:
    """Carry out one job. Returns False if there was none to claim."""
    if store.paused():
        kinds = tuple(k for k in kinds if k not in TESTBED_KINDS)
    job = store.claim_job(list(kinds), service.job_claim_s)
    if job is None:
        return False
    with _holding(job, service):
        tasks.carry_out(job)
    return True


@contextmanager
def _holding(job: Job, service: Service) -> Iterator[None]:
    """Hold the claim on a job while it runs.

    Stops the process on SIGTERM, on losing the claim, or past job_timeout_s.
    """
    claim = job.claim
    if claim is None:
        raise ValueError(f"job {job.job_id} is not claimed")
    done = threading.Event()

    def renew() -> None:
        stop_at = time.monotonic() + service.job_kill_s
        while not done.wait(service.job_claim_s / 3):
            if time.monotonic() > stop_at:
                log.error("job %s ran past job_timeout_s; stopping the worker", job.job_id)
                os._exit(1)
            try:
                held = store.renew_claim(job.job_id, claim, service.job_claim_s)
            except psycopg.Error as exc:
                log.warning("could not renew the claim on job %s: %s", job.job_id, exc)
                continue
            if not held:
                log.error("another worker claimed job %s; stopping this one", job.job_id)
                os._exit(1)

    def hand_back(*_: object) -> None:
        log.info("stopping; handing job %s back", job.job_id)
        try:
            store.give_up_claim(job.job_id, claim)
        except psycopg.Error as exc:
            log.warning("could not hand job %s back: %s", job.job_id, exc)
        os._exit(0)

    before = signal.signal(signal.SIGTERM, hand_back)
    renewer = threading.Thread(target=renew, daemon=True)
    renewer.start()
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, before)
        done.set()
        renewer.join()
