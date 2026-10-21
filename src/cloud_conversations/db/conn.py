"""Database connection management."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://dev:dev@localhost:5432/cloudconversations",
)


_TIMEOUTS = "-c lock_timeout=10s -c statement_timeout=60s"


@contextmanager
def get_conn() -> Generator[psycopg.Connection[Any], None, None]:
    """Yield an open connection with dict-style row access."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row, options=_TIMEOUTS) as conn:
        yield conn
