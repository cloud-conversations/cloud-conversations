"""Connections to the database."""

from __future__ import annotations

import psycopg
import pytest

from cloud_conversations.db import conn
from cloud_conversations.schema.service import Service


def test_a_connection_is_tried_again_until_the_database_answers(monkeypatch):
    connect = psycopg.connect
    refusals = [psycopg.OperationalError("connection refused")] * 2

    def flaky(*args, **kwargs):
        if refusals:
            raise refusals.pop()
        return connect(*args, **kwargs)

    monkeypatch.setattr(psycopg, "connect", flaky)
    monkeypatch.setattr(conn, "_service", lambda: Service(db_retry_s=0.01))

    with conn.get_conn() as c:
        assert c.execute("SELECT 41 + 1 AS answer").fetchone() == {"answer": 42}
    assert refusals == []


def test_a_connection_gives_up_after_db_connect_s(monkeypatch):
    tries: list[None] = []

    def down(*args, **kwargs):
        tries.append(None)
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", down)
    monkeypatch.setattr(conn, "_service", lambda: Service(db_connect_s=0.05, db_retry_s=0.01))

    with pytest.raises(psycopg.OperationalError, match="connection refused"), conn.get_conn():
        pass
    assert len(tries) > 1
