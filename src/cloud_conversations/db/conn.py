"""Database connection management."""

from __future__ import annotations

import functools
import logging
import os
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cloud_conversations.config import load_config
from cloud_conversations.schema.service import Service

log = logging.getLogger(__name__)

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://dev:dev@localhost:5432/cloudconversations",
)


_TIMEOUTS = "-c lock_timeout=10s -c statement_timeout=60s"


@functools.cache
def _service() -> Service:
    return load_config().service


@contextmanager
def get_conn() -> Generator[psycopg.Connection[Any], None, None]:
    """Yield an open connection with dict-style row access."""
    with _connect() as conn:
        yield conn


def _connect() -> psycopg.Connection[Any]:
    """Raises psycopg.OperationalError if the database stays unreachable for db_connect_s."""
    service = _service()
    deadline = time.monotonic() + service.db_connect_s
    while True:
        try:
            return psycopg.connect(DATABASE_URL, row_factory=dict_row, options=_TIMEOUTS)
        except psycopg.OperationalError as exc:
            if time.monotonic() >= deadline:
                raise
            log.warning("cannot reach the database; trying again: %s", exc)
            time.sleep(service.db_retry_s)
