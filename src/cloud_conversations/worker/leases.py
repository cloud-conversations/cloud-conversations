"""Lease warnings, and the leases that ended."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import cloud_conversations.db.store as store
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.credentials import CredentialsError, user_scope
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.config import Config
from cloud_conversations.db.store import LeaseDue
from cloud_conversations.runner import sequential

log = logging.getLogger(__name__)

# States of a lease that is over.
_OVER = ("TERMINATED", "DELETED", "ERROR")


def watch(config: Config) -> None:
    service = config.service
    for lease in store.leases_to_warn(service.lease_warning_s):
        _say(
            lease,
            f"The lease at {lease.site} ends at {lease.expires_at:%Y-%m-%d %H:%M} UTC. "
            "Save your work before then.",
        )
    for lease in store.ended_leases_to_check(service.lease_watch_s):
        try:
            _check(lease, config)
        except (CredentialsError, AdapterError) as exc:
            log.warning("could not check lease %s at %s: %s", lease.lease_id, lease.site, exc)
        except Exception:
            log.exception("could not check lease %s at %s", lease.lease_id, lease.site)


def _check(lease: LeaseDue, config: Config) -> None:
    """Check a lease past its end at the testbed."""
    adapter = credentials.build_adapter(
        lease.site, config, user_scope(lease.owner_id, lease.project_id)
    )
    found = next((x for x in adapter.list_leases(lease.site) if x.lease_id == lease.lease_id), None)
    if found is not None and found.state not in _OVER and found.end > datetime.now(timezone.utc):
        store.record_lease_end(lease.run_id, lease.lease_id, found.end)
        return
    sequential.lease_ended(lease.run_id, lease.lease_id, lease.site)
    _say(lease, f"The lease at {lease.site} ended.")


def _say(lease: LeaseDue, text: str) -> None:
    thread = store.thread_for_description(lease.owner_id, lease.description_id)
    if thread is not None:
        store.append_message(
            thread.thread_id, "assistant", "notice", text, {"run_id": str(lease.run_id)},
            owner_id=lease.owner_id,
        )
