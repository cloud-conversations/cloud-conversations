"""Data-access layer."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from cloud_conversations.config import testbed_of
from cloud_conversations.db.conn import get_conn
from cloud_conversations.schema.description import Check, KeypairChoice, Requirements, keypair_at
from cloud_conversations.schema.events import Actor, EventKind
from cloud_conversations.schema.knowledge import Document, Index, Passage, SourceState
from cloud_conversations.schema.runs import (
    CheckResult,
    Connection,
    Failure,
    Plan,
    RunState,
    check_transition,
)
from cloud_conversations.schema.service import QUOTAS


class NotFoundError(Exception):
    """No row matches the ID and owner."""


class AccessError(Exception):
    """The caller tried to change a read-only field."""


class ConcurrencyError(Exception):
    """Database lock contention."""


RecordOutcome = Literal["inserted", "replay", "duplicate_key"]


class DuplicateKeyError(Exception):
    """One idempotency key was used for two provider resources.

    The resource is still recorded, untagged.
    """


class ConflictError(Exception):
    """A change conflicts with the current state."""


@dataclass
class Description:
    description_id: uuid.UUID
    version: int
    parent_version: int | None
    revision_reason: str | None
    owner_id: str
    conversation_id: str
    requirements: Requirements
    rubric: list[Check]
    unsupported: list[str]
    open_questions: list[str]
    created_at: datetime


@dataclass
class Conversation:
    conversation_id: uuid.UUID
    owner_id: str
    prompt: str
    transcript: list[dict[str, Any]]
    description_id: uuid.UUID | None
    description_version: int | None
    created_at: datetime


JobKind = Literal["exchange", "build", "teardown"]
MessageRole = Literal["user", "assistant"]
MessageKind = Literal[
    "user", "answer", "description", "run", "notice", "holdings", "release"
]


@dataclass
class Thread:
    thread_id: uuid.UUID
    owner_id: str
    title: str
    ssh_key_ref: str | None
    site_keypairs: dict[str, str] | None
    project_id: str | None
    description_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    def keypair(self) -> KeypairChoice | None:
        return self.site_keypairs or self.ssh_key_ref

    def keypair_at(self, site: str) -> str | None:
        choice = self.keypair()
        return keypair_at(choice, site) if choice is not None else None


@dataclass
class Message:
    message_id: uuid.UUID
    thread_id: uuid.UUID
    seq: int
    role: str
    kind: str
    text: str
    payload: dict[str, Any]
    conversation_id: uuid.UUID | None
    description_id: uuid.UUID | None
    description_version: int | None
    created_at: datetime


@dataclass
class Feedback:
    feedback_id: uuid.UUID
    thread_id: uuid.UUID
    message_id: uuid.UUID | None
    run_id: uuid.UUID | None
    rating: str | None
    comment: str | None
    created_at: datetime
    updated_at: datetime


_FEEDBACK_COLUMNS = (
    "feedback_id, thread_id, message_id, run_id, rating, comment, created_at, updated_at"
)


@dataclass
class ListedFeedback:
    feedback_id: uuid.UUID
    updated_at: datetime
    owner_id: str
    username: str
    email: str | None
    thread_id: uuid.UUID
    thread_title: str
    rating: str | None
    comment: str | None
    message_kind: str | None
    message_text: str | None
    run_id: uuid.UUID | None
    run_state: str | None
    run_failure: dict[str, Any] | None


@dataclass
class FeedbackDetail(ListedFeedback):
    created_at: datetime
    project_id: str | None
    message_id: uuid.UUID | None
    message_payload: dict[str, Any] | None
    conversation_id: uuid.UUID | None
    prompt: str | None
    transcript: list[dict[str, Any]] | None
    routing_conversation_id: uuid.UUID | None
    routing_transcript: list[dict[str, Any]] | None
    job_id: uuid.UUID | None
    job_error: str | None
    description_id: uuid.UUID | None
    description_version: int | None
    requirements: dict[str, Any] | None
    rubric: list[dict[str, Any]] | None
    # The run's build-agent turns, each with what it was sent.
    build_turns: list[dict[str, Any]]


@dataclass
class Job:
    job_id: uuid.UUID
    thread_id: uuid.UUID
    owner_id: str
    kind: str
    payload: dict[str, Any]
    state: str
    route: str | None
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass
class Run:
    run_id: uuid.UUID
    description_id: uuid.UUID
    description_version: int
    owner_id: str
    project_id: str | None
    state: RunState
    resume_count: int
    plan: Plan | None
    results: list[CheckResult] | None
    connection: Connection | None
    failure: Failure | None
    created_at: datetime
    updated_at: datetime
    heartbeat_at: datetime

    @property
    def site(self) -> str | None:
        """The site the run was planned at."""
        return self.plan.site if self.plan is not None else None


@dataclass
class Event:
    run_id: uuid.UUID
    seq: int
    at: datetime
    actor: str
    kind: str
    payload: dict


@dataclass
class Resource:
    provider_id: str
    kind: str
    run_id: uuid.UUID
    site: str
    expires_at: datetime | None
    created_at: datetime
    released_at: datetime | None
    idempotency_key: str | None


@dataclass
class User:
    user_id: str
    username: str
    projects: list[dict[str, Any]]
    default_project_id: str | None
    default_ssh_key_ref: str | None
    default_site_keypairs: dict[str, str] | None
    default_vm_site: str | None
    default_baremetal_site: str | None
    created_at: datetime
    checked_at: datetime | None
    max_messages_per_day: int | None
    max_builds_per_user: int | None
    max_active_experiments_per_user: int | None

    @property
    def testbed(self) -> str:
        return testbed_of(self.user_id)

    def in_project(self, project_id: str) -> bool:
        return project_id in {str(p["id"]) for p in self.projects}

    def project(self) -> str | None:
        """The project the user's new chats build in."""
        if self.default_project_id and self.in_project(self.default_project_id):
            return self.default_project_id
        return str(self.projects[0]["id"]) if self.projects else None


_USER_COLUMNS = (
    "user_id, username, projects, default_project_id, default_ssh_key_ref, "
    "default_site_keypairs, default_vm_site, "
    "default_baremetal_site, created_at, checked_at, max_messages_per_day, max_builds_per_user, "
    "max_active_experiments_per_user"
)


@dataclass
class UserSummary:
    user_id: str
    testbed: str
    username: str
    email: str | None
    projects: list[dict[str, Any]]
    local: bool
    first_login_at: datetime | None
    last_login_at: datetime | None
    checked_at: datetime | None
    disabled_at: datetime | None
    disabled_reason: str | None
    open_sessions: int
    chats: int
    latest_chat_at: datetime | None
    overridden: bool
    approval: str
    approval_requested_at: datetime | None
    approval_decided_at: datetime | None


@dataclass
class Usage:
    messages_today: int
    # When the oldest of messages_today is a day old.
    next_message_at: datetime | None
    builds_running: int
    active_experiments: int


@dataclass
class ActiveExperiment:
    run_id: uuid.UUID
    state: str
    site: str
    ends_at: str | None
    thread_id: uuid.UUID | None
    title: str | None
    resources: list[dict[str, str]]


@dataclass
class UserKey:
    owner_id: str
    name: str
    keypair_name: str
    public_key: str
    created_at: datetime
    updated_at: datetime


@dataclass
class Trashed:
    thread_id: uuid.UUID
    title: str
    deleted_at: datetime
    restorable_until: datetime


@dataclass
class Announcement:
    text: str
    posted_at: datetime


@dataclass
class SystemKey:
    fingerprint: str
    private_key: str
    created_at: datetime
    retired_at: datetime | None


_JSONB_RUN_FIELDS: frozenset[str] = frozenset({"plan", "results", "connection", "failure"})
_ALLOWED_RUN_UPDATE_FIELDS: frozenset[str] = frozenset(
    {"state", "resume_count"} | _JSONB_RUN_FIELDS
)

_VALID_EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))
_VALID_ACTORS: frozenset[str] = frozenset(get_args(Actor))

_SENSITIVE_PATTERNS: frozenset[str] = frozenset(
    {"password", "passwd", "secret", "token", "private_key", "api_key", "credential", "auth_key"}
)


@contextmanager
def _use_conn(
    conn: psycopg.Connection[Any] | None,
) -> Generator[psycopg.Connection[Any], None, None]:
    """Yield the supplied connection, or a new one inside a transaction."""
    if conn is not None:
        yield conn
    else:
        with get_conn() as c, c.transaction():
            yield c


@contextmanager
def _translate() -> Generator[None, None, None]:
    """Map lock timeouts and deadlocks into ConcurrencyError."""
    try:
        yield
    except (psycopg.errors.LockNotAvailable, psycopg.errors.DeadlockDetected) as exc:
        raise ConcurrencyError(f"Contention on the database: {exc}") from exc


def _lock_run(c: psycopg.Connection, run_id: uuid.UUID) -> str:
    """Take the run row's write lock and return its owner.

    Every transaction touching a run must take this lock first.
    """
    row = c.execute(
        "SELECT owner_id FROM experiments.runs WHERE run_id = %s FOR UPDATE",
        [run_id],
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Run {run_id} not found")
    return str(row["owner_id"])  # type: ignore[call-overload]


_REDACTED = "[redacted]"

_REDACTION_EXEMPT: frozenset[str] = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "token_count",
    }
)


def _redact(value: Any, _key: str = "") -> Any:
    """Redact credential-shaped values by key name."""
    lowered = _key.lower()
    if lowered not in _REDACTION_EXEMPT and any(p in lowered for p in _SENSITIVE_PATTERNS):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, _key) for v in value]
    return value


def _vector(values: list[float]) -> str:
    """A pgvector literal."""
    return f"[{','.join(str(float(v)) for v in values)}]"


def _as_jsonb(v: Any) -> Any:
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        return Jsonb(v.model_dump())
    if isinstance(v, list):
        return Jsonb([item.model_dump() if hasattr(item, "model_dump") else item for item in v])
    if isinstance(v, dict):
        return Jsonb(v)
    return v


def _row_to_description(row: dict) -> Description:
    return Description(
        description_id=row["description_id"],
        version=row["version"],
        parent_version=row["parent_version"],
        revision_reason=row["revision_reason"],
        owner_id=row["owner_id"],
        conversation_id=row["conversation_id"],
        requirements=Requirements.model_validate(row["requirements"]),
        rubric=[Check.model_validate(c) for c in row["rubric"]],
        unsupported=list(row["unsupported"]),
        open_questions=list(row["open_questions"]),
        created_at=row["created_at"],
    )


def _row_to_conversation(row: dict) -> Conversation:
    return Conversation(
        conversation_id=row["conversation_id"],
        owner_id=row["owner_id"],
        prompt=row["prompt"],
        transcript=list(row["transcript"]),
        description_id=row["description_id"],
        description_version=row["description_version"],
        created_at=row["created_at"],
    )


def _row_to_run(row: dict) -> Run:
    return Run(
        run_id=row["run_id"],
        description_id=row["description_id"],
        description_version=row["description_version"],
        owner_id=row["owner_id"],
        project_id=row["project_id"],
        state=RunState(row["state"]),
        resume_count=row["resume_count"],
        plan=Plan.model_validate(row["plan"]) if row["plan"] is not None else None,
        results=(
            [CheckResult.model_validate(r) for r in row["results"]]
            if row["results"] is not None
            else None
        ),
        connection=(
            Connection.model_validate(row["connection"]) if row["connection"] is not None else None
        ),
        failure=Failure.model_validate(row["failure"]) if row["failure"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        heartbeat_at=row["heartbeat_at"],
    )


def _row_to_event(row: dict) -> Event:
    return Event(
        run_id=row["run_id"],
        seq=row["seq"],
        at=row["at"],
        actor=row["actor"],
        kind=row["kind"],
        payload=dict(row["payload"]),
    )


def _row_to_resource(row: dict) -> Resource:
    return Resource(
        provider_id=row["provider_id"],
        kind=row["kind"],
        run_id=row["run_id"],
        site=row["site"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        released_at=row["released_at"],
        idempotency_key=row["idempotency_key"],
    )


def create_description(
    owner_id: str,
    conversation_id: str,
    requirements: Requirements,
    rubric: list[Check],
    unsupported: list[str] | None = None,
    open_questions: list[str] | None = None,
    description_id: uuid.UUID | None = None,
    parent_version: int | None = None,
    revision_reason: str | None = None,
) -> tuple[uuid.UUID, int]:
    """Insert a description snapshot and return (description_id, version).

    description_id=None starts a new description.
    """
    unsupported = list(unsupported or [])
    open_questions = list(open_questions or [])
    new_lineage = description_id is None
    if new_lineage:
        description_id = uuid.uuid4()
    assert description_id is not None

    try:
        with _translate(), transaction() as c:
            if new_lineage:
                version = 1
            else:
                c.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    [str(description_id)],
                )
                row = c.execute(
                    """
                    SELECT max(version) AS v
                      FROM experiments.experiment_descriptions
                     WHERE description_id = %s AND owner_id = %s
                    """,
                    [description_id, owner_id],
                ).fetchone()
                current: int | None = row["v"] if row else None  # type: ignore[call-overload]
                if current is None:
                    raise NotFoundError(
                        f"Description {description_id} not found for owner {owner_id!r}; "
                        "pass description_id=None to start a new lineage"
                    )
                version = current + 1

            c.execute(
                """
                INSERT INTO experiments.experiment_descriptions
                       (description_id, version, parent_version,
                        revision_reason, owner_id, conversation_id,
                        requirements, rubric, unsupported, open_questions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    description_id,
                    version,
                    parent_version,
                    revision_reason,
                    owner_id,
                    conversation_id,
                    Jsonb(requirements.model_dump(mode="json")),
                    Jsonb([c_item.model_dump() for c_item in rubric]),
                    Jsonb(unsupported),
                    Jsonb(open_questions),
                ],
            )
            return description_id, version

    except psycopg.errors.UniqueViolation as exc:
        raise ConcurrencyError(
            f"Could not allocate a version for description {description_id}"
        ) from exc


def get_description(owner_id: str, description_id: uuid.UUID, version: int) -> Description:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
              FROM experiments.experiment_descriptions
             WHERE description_id = %s AND version = %s AND owner_id = %s AND deleted_at IS NULL
            """,
            [description_id, version, owner_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Description {description_id} v{version} not found for owner {owner_id!r}"
        )
    return _row_to_description(row)  # type: ignore[arg-type]


def latest_description(owner_id: str, description_id: uuid.UUID) -> Description:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT *
              FROM experiments.experiment_descriptions
             WHERE description_id = %s AND owner_id = %s
             ORDER BY version DESC
             LIMIT 1
            """,
            [description_id, owner_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Description {description_id} not found for owner {owner_id!r}")
    return _row_to_description(row)  # type: ignore[arg-type]


def create_conversation(
    owner_id: str,
    prompt: str,
    transcript: list[dict[str, Any]],
    description_id: uuid.UUID | None = None,
    description_version: int | None = None,
    *,
    conversation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    conversation_id = conversation_id or uuid.uuid4()
    with transaction() as c:
        c.execute(
            """
            INSERT INTO chat.conversations
                   (conversation_id, owner_id, prompt, transcript,
                    description_id, description_version)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [conversation_id, owner_id, prompt, Jsonb(transcript),
             description_id, description_version],
        )
    return conversation_id


def get_conversation(owner_id: str, conversation_id: uuid.UUID) -> Conversation:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT * FROM chat.conversations
             WHERE conversation_id = %s AND owner_id = %s
            """,
            [conversation_id, owner_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(
            f"Conversation {conversation_id} not found for owner {owner_id!r}"
        )
    return _row_to_conversation(row)  # type: ignore[arg-type]


def conversations_for_description(
    owner_id: str, description_id: uuid.UUID
) -> list[Conversation]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM chat.conversations
             WHERE description_id = %s AND owner_id = %s
             ORDER BY description_version, created_at
            """,
            [description_id, owner_id],
        ).fetchall()
    return [_row_to_conversation(r) for r in rows]


def create_run(
    owner_id: str,
    description_id: uuid.UUID,
    description_version: int,
    project_id: str | None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    with transaction() as c:
        c.execute(
            """
            INSERT INTO experiments.runs
                   (run_id, description_id, description_version,
                    owner_id, project_id, state)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [run_id, description_id, description_version, owner_id,
             project_id, RunState.planning.value],
        )
    return run_id


def get_run(owner_id: str, run_id: uuid.UUID) -> Run:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM experiments.runs "
            "WHERE run_id = %s AND owner_id = %s AND deleted_at IS NULL",
            [run_id, owner_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Run {run_id} not found for owner {owner_id!r}")
    return _row_to_run(row)  # type: ignore[arg-type]


def get_run_unscoped(run_id: uuid.UUID) -> Run:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM experiments.runs WHERE run_id = %s",
            [run_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Run {run_id} not found")
    return _row_to_run(row)  # type: ignore[arg-type]


def update_run(
    owner_id: str,
    run_id: uuid.UUID,
    **fields: Any,
) -> None:
    """Raises IllegalTransitionError if the state change is not permitted."""
    invalid = set(fields) - _ALLOWED_RUN_UPDATE_FIELDS
    if invalid:
        raise AccessError(f"Cannot update run field(s): {sorted(invalid)!r}")
    if not fields:
        return

    set_parts: list[str] = []
    params: list[Any] = []

    for col in ("state", "resume_count", "plan", "results", "connection", "failure"):
        if col not in fields:
            continue
        val = fields[col]
        set_parts.append(f"{col} = %s")
        if col in _JSONB_RUN_FIELDS:
            params.append(_as_jsonb(val))
        elif col == "state":
            params.append(RunState(val).value)
        else:
            params.append(val)

    set_parts.append("updated_at = now()")
    set_parts.append("heartbeat_at = now()")
    params.extend([owner_id, run_id])

    with _translate(), transaction() as c:
        if "state" in fields:
            current = c.execute(
                """
                SELECT state FROM experiments.runs
                 WHERE run_id = %s AND owner_id = %s
                   FOR UPDATE
                """,
                [run_id, owner_id],
            ).fetchone()
            if current is None:
                raise NotFoundError(f"Run {run_id} not found for owner {owner_id!r}")
            check_transition(RunState(current["state"]), RunState(fields["state"]))  # type: ignore[call-overload]

        result = c.execute(
            f"UPDATE experiments.runs SET {', '.join(set_parts)} "
            "WHERE owner_id = %s AND run_id = %s",
            params,
        )
        if result.rowcount == 0:
            raise NotFoundError(f"Run {run_id} not found for owner {owner_id!r}")


def touch_heartbeat(run_id: uuid.UUID) -> None:
    """Mark a run as still alive. Not owner-scoped."""
    with _translate(), transaction() as c:
        c.execute(
            "UPDATE experiments.runs SET heartbeat_at = now() WHERE run_id = %s",
            [run_id],
        )


def runs_for_description(owner_id: str, description_id: uuid.UUID) -> list[Run]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM experiments.runs
             WHERE description_id = %s AND owner_id = %s AND deleted_at IS NULL
             ORDER BY created_at
            """,
            [description_id, owner_id],
        ).fetchall()
    return [_row_to_run(r) for r in rows]


def _append_event_impl(
    c: psycopg.Connection,
    run_id: uuid.UUID,
    actor: str,
    kind: str,
    payload: dict,
    owner_id: str | None,
    model_input: list[dict[str, str]] | None,
) -> int:
    actual_owner = _lock_run(c, run_id)
    if owner_id is not None and actual_owner != owner_id:
        raise NotFoundError(f"Run {run_id} not found for owner {owner_id!r}")

    seq_row = c.execute(
        "SELECT COALESCE(max(seq), 0) + 1 AS next_seq "
        "FROM experiments.run_events WHERE run_id = %s",
        [run_id],
    ).fetchone()
    seq: int = seq_row["next_seq"]  # type: ignore[index,call-overload]

    c.execute(
        """
        INSERT INTO experiments.run_events (run_id, seq, actor, kind, payload, model_input)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [run_id, seq, actor, kind, Jsonb(payload), _as_jsonb(model_input)],
    )
    return seq


def append_event(
    run_id: uuid.UUID,
    actor: str,
    kind: str,
    payload: dict,
    *,
    owner_id: str | None = None,
    conn: psycopg.Connection | None = None,
    model_input: list[dict[str, str]] | None = None,
) -> int:
    """Append an event to a run log, allocating seq atomically.

    owner_id, when given, is verified against the run.
    """
    if kind not in _VALID_EVENT_KINDS:
        raise ValueError(f"Unknown event kind {kind!r}")
    if actor not in _VALID_ACTORS:
        raise ValueError(f"Unknown actor {actor!r}")

    payload = dict(payload)
    if "v" not in payload:
        payload["v"] = 1

    payload = _redact(payload)

    for attempt in range(2):
        try:
            with _translate(), _use_conn(conn) as c:
                return _append_event_impl(c, run_id, actor, kind, payload, owner_id, model_input)
        except psycopg.errors.UniqueViolation:
            if attempt == 0 and conn is None:
                continue
            raise

    raise RuntimeError("unreachable")  # pragma: no cover


def read_events(owner_id: str, run_id: uuid.UUID) -> list[Event]:
    with get_conn() as conn:
        owner_row = conn.execute(
            "SELECT 1 FROM experiments.runs "
            "WHERE run_id = %s AND owner_id = %s AND deleted_at IS NULL",
            [run_id, owner_id],
        ).fetchone()
        if owner_row is None:
            raise NotFoundError(f"Run {run_id} not found for owner {owner_id!r}")

        rows = conn.execute(
            "SELECT run_id, seq, at, actor, kind, payload FROM experiments.run_events "
            "WHERE run_id = %s ORDER BY seq",
            [run_id],
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def record_resource(
    run_id: uuid.UUID,
    provider_id: str,
    kind: str,
    site: str,
    expires_at: datetime | None,
    *,
    idempotency_key: str | None = None,
    conn: psycopg.Connection | None = None,
) -> RecordOutcome:
    """Record a cloud resource and return what happened.

    If another live provider_id already has the key, record the resource untagged
    and return "duplicate_key".
    """
    with _translate(), _use_conn(conn) as c:
        _lock_run(c, run_id)
        row = c.execute(
            """
            INSERT INTO experiments.run_resources
                   (provider_id, run_id, kind, site, expires_at, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING provider_id
            """,
            [provider_id, run_id, kind, site, expires_at, idempotency_key],
        ).fetchone()
        if row is not None:
            return "inserted"

        existing = c.execute(
            """
            SELECT provider_id FROM experiments.run_resources
             WHERE kind = %s AND provider_id = %s
            """,
            [kind, provider_id],
        ).fetchone()
        if existing is not None:
            return "replay"

        c.execute(
            """
            INSERT INTO experiments.run_resources
                   (provider_id, run_id, kind, site, expires_at, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, NULL)
            ON CONFLICT DO NOTHING
            """,
            [provider_id, run_id, kind, site, expires_at],
        )
        return "duplicate_key"


def create_resource_with_event(
    run_id: uuid.UUID,
    provider_id: str,
    kind: str,
    site: str,
    expires_at: datetime | None,
    *,
    idempotency_key: str | None = None,
) -> RecordOutcome:
    """Record a cloud resource and append its resource_created event atomically.

    Raises DuplicateKeyError after the transaction closes on a reused key.
    """
    with transaction() as c:
        outcome = record_resource(run_id, provider_id, kind, site, expires_at,
                                  idempotency_key=idempotency_key, conn=c)
        if outcome == "inserted":
            append_event(run_id, "adapter", "resource_created",
                         {"kind": kind, "provider_id": provider_id, "site": site}, conn=c)

    if outcome == "duplicate_key":
        raise DuplicateKeyError(
            f"Idempotency key {idempotency_key!r} already belongs to another live "
            f"resource; recorded {kind} {provider_id!r} untagged"
        )
    return outcome


def resource_by_idempotency_key(key: str) -> Resource | None:
    """Return the live resource recorded under this key, or None."""
    with transaction() as c:
        row = c.execute(
            """
            SELECT * FROM experiments.run_resources
             WHERE idempotency_key = %s AND released_at IS NULL
            """,
            [key],
        ).fetchone()
    return _row_to_resource(row) if row else None  # type: ignore[arg-type]


def release_resource_with_event(
    run_id: uuid.UUID,
    kind: str,
    provider_id: str,
    site: str,
) -> None:
    """Mark a run's resource released and append its resource_released event atomically."""
    with _translate(), transaction() as c:
        c.execute(
            """
            UPDATE experiments.run_resources
               SET released_at = now()
             WHERE run_id = %s AND kind = %s AND provider_id = %s AND released_at IS NULL
            """,
            [run_id, kind, provider_id],
        )
        append_event(run_id, "adapter", "resource_released",
                     {"kind": kind, "provider_id": provider_id, "site": site}, conn=c)


def replace_knowledge(documents: list[Document], embedding_model: str) -> Index:
    """Replace the whole knowledge index in one transaction, and record the build.

    Raises ValueError on no documents, on a chunk with no embedding, or on
    embeddings of differing widths.
    """
    docs: list[tuple[Any, ...]] = []
    parents: list[tuple[Any, ...]] = []
    chunks: list[tuple[Any, ...]] = []
    widths: set[int] = set()

    for document in documents:
        doc_id = uuid.uuid4()
        docs.append((doc_id, document.source, document.url, document.title,
                     document.content, document.fetched_at))
        for ordinal, parent in enumerate(document.parents):
            parent_id = uuid.uuid4()
            parents.append((parent_id, doc_id, ordinal, parent.content))
            for position, chunk in enumerate(parent.chunks):
                if not chunk.embedding:
                    raise ValueError(f"a chunk of {document.url} has no embedding")
                widths.add(len(chunk.embedding))
                chunks.append((uuid.uuid4(), parent_id, position, chunk.content,
                               _vector(chunk.embedding)))

    if not docs:
        raise ValueError("refusing to replace the knowledge index with no documents")
    if len(widths) != 1:
        raise ValueError(f"embeddings are {sorted(widths)} wide; one model built these")

    build_id = uuid.uuid4()
    dimensions = widths.pop()

    with _translate(), transaction() as c, c.cursor() as cur:
        cur.execute("DELETE FROM knowledge.documents")
        cur.executemany(
            """
            INSERT INTO knowledge.documents
                   (doc_id, source, url, title, content, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            docs,
        )
        cur.executemany(
            "INSERT INTO knowledge.parents (parent_id, doc_id, ordinal, content) "
            "VALUES (%s, %s, %s, %s)",
            parents,
        )
        cur.executemany(
            """
            INSERT INTO knowledge.chunks (chunk_id, parent_id, ordinal, content, embedding)
            VALUES (%s, %s, %s, %s, %s::vector)
            """,
            chunks,
        )
        row = cur.execute(
            """
            INSERT INTO knowledge.builds
                   (build_id, embedding_model, dimensions, documents, parents, chunks)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            [build_id, embedding_model, dimensions,
             len(docs), len(parents), len(chunks)],
        ).fetchone()
    return Index.model_validate(dict(row))  # type: ignore[arg-type]


def latest_index() -> Index | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM knowledge.builds ORDER BY built_at DESC LIMIT 1"
        ).fetchone()
    return Index.model_validate(dict(row)) if row else None


def knowledge_sources() -> list[SourceState]:
    """Document and chunk counts per source, by name."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT d.source,
                   count(DISTINCT d.doc_id) AS documents,
                   count(c.chunk_id)        AS chunks,
                   max(d.fetched_at)        AS fetched_at
              FROM knowledge.documents d
              LEFT JOIN knowledge.parents p ON p.doc_id = d.doc_id
              LEFT JOIN knowledge.chunks c ON c.parent_id = p.parent_id
             GROUP BY d.source
             ORDER BY d.source
            """
        ).fetchall()
    return [SourceState.model_validate(dict(r)) for r in rows]


def search_chunks(embedding: list[float], limit: int) -> list[Passage]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.parent_id, d.source, d.url, c.content
              FROM knowledge.chunks c
              JOIN knowledge.parents p ON p.parent_id = c.parent_id
              JOIN knowledge.documents d ON d.doc_id = p.doc_id
             ORDER BY c.embedding <=> %s::vector
             LIMIT %s
            """,
            [_vector(embedding), limit],
        ).fetchall()
    return [Passage.model_validate(dict(r)) for r in rows]


def parents_of(parent_ids: list[uuid.UUID]) -> dict[uuid.UUID, Passage]:
    if not parent_ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.parent_id, d.source, d.url, p.content
              FROM knowledge.parents p
              JOIN knowledge.documents d ON d.doc_id = p.doc_id
             WHERE p.parent_id = ANY(%s)
            """,
            [parent_ids],
        ).fetchall()
    found = [Passage.model_validate(dict(r)) for r in rows]
    return {p.parent_id: p for p in found}


def live_resources(run_id: uuid.UUID) -> list[Resource]:
    """A caller serving a user must authorise the run first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM experiments.run_resources WHERE released_at IS NULL AND run_id = %s",
            [run_id],
        ).fetchall()
    return [_row_to_resource(r) for r in rows]


DecisionStep = Literal["proposed", "confirmed", "declined"]
DecisionActor = Literal["user", "agent"]
DecisionAction = Literal["build", "resume", "release", "release_and_build", "delete_keypair"]
Via = Literal["web", "cli"]
CallOutcome = Literal["started", "ok", "failed"]


@dataclass
class Decision:
    """One step of a request in audit.decisions."""

    request_id: uuid.UUID
    at: datetime
    owner_id: str
    step: DecisionStep
    actor: DecisionActor
    via: Via | None
    action: DecisionAction
    run_id: uuid.UUID | None
    thread_id: uuid.UUID | None
    description_id: uuid.UUID | None
    version: int | None
    resources: list[dict[str, str]]
    shown: str | None
    reason: str | None
    detail: dict[str, Any]


def _row_to_decision(row: dict) -> Decision:
    return Decision(**{f: row[f] for f in Decision.__dataclass_fields__})


def record_decision(
    request_id: uuid.UUID,
    owner_id: str,
    step: DecisionStep,
    actor: DecisionActor,
    action: DecisionAction,
    *,
    via: Via | None = None,
    run_id: uuid.UUID | None = None,
    thread_id: uuid.UUID | None = None,
    description_id: uuid.UUID | None = None,
    version: int | None = None,
    resources: list[dict[str, str]] | None = None,
    shown: str | None = None,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
    conn: psycopg.Connection | None = None,
) -> None:
    with _translate(), _use_conn(conn) as c:
        c.execute(
            """
            INSERT INTO audit.decisions
                   (request_id, owner_id, step, actor, via, action, run_id, thread_id,
                    description_id, version, resources, shown, reason, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                request_id, owner_id, step, actor, via, action, run_id, thread_id,
                description_id, version, Jsonb(resources or []), shown, reason,
                Jsonb(_redact(detail or {})),
            ],
        )


def decision_request(
    owner_id: str, request_id: uuid.UUID, conn: psycopg.Connection | None = None
) -> list[Decision]:
    """Every step of the request, oldest first.

    Locks the first step until conn's transaction ends. Raises NotFoundError if the owner
    made no such request.
    """
    with _translate(), _use_conn(conn) as c:
        first = c.execute(
            "SELECT seq FROM audit.decisions WHERE request_id = %s AND owner_id = %s "
            "ORDER BY seq LIMIT 1 FOR UPDATE",
            [request_id, owner_id],
        ).fetchone()
        if first is None:
            raise NotFoundError(f"No request {request_id} for owner {owner_id!r}")
        rows = c.execute(
            "SELECT * FROM audit.decisions WHERE request_id = %s ORDER BY seq",
            [request_id],
        ).fetchall()
    return [_row_to_decision(r) for r in rows]


def pending_release(owner_id: str, run_id: uuid.UUID) -> Decision | None:
    """The agent's newest unanswered proposal for the run."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT p.* FROM audit.decisions p
             WHERE p.owner_id = %s AND p.run_id = %s AND p.step = 'proposed'
               AND p.actor = 'agent'
               AND NOT EXISTS (SELECT 1 FROM audit.decisions d
                                WHERE d.request_id = p.request_id AND d.step <> 'proposed')
             ORDER BY p.seq DESC
             LIMIT 1
            """,
            [owner_id, run_id],
        ).fetchone()
    return _row_to_decision(row) if row else None  # type: ignore[arg-type]


def answers(owner_id: str, request_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """The user's answer to each request."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT request_id, step FROM audit.decisions
             WHERE owner_id = %s AND request_id = ANY(%s)
               AND step IN ('confirmed', 'declined')
            """,
            [owner_id, request_ids],
        ).fetchall()
    return {r["request_id"]: r["step"] for r in rows}  # type: ignore[call-overload]


def record_testbed_call(
    call_id: uuid.UUID,
    owner_id: str,
    site: str,
    operation: str,
    kind: str,
    provider_id: str | None,
    outcome: CallOutcome,
    error: str | None,
    request_id: uuid.UUID,
    run_id: uuid.UUID | None,
    reason: str | None,
    arguments: dict[str, Any],
) -> None:
    with _translate(), get_conn() as conn:
        conn.execute(
            """
            INSERT INTO audit.testbed_calls
                   (call_id, owner_id, site, operation, kind, provider_id, outcome, error,
                    request_id, run_id, reason, arguments)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                call_id, owner_id, site, operation, kind, provider_id, outcome, error,
                request_id, run_id, reason, Jsonb(_redact(arguments)),
            ],
        )


_ALLOWED_THREAD_UPDATE_FIELDS: frozenset[str] = frozenset(
    {"title", "ssh_key_ref", "site_keypairs", "project_id", "description_id"}
)

_UNFINISHED_JOB_STATES: list[str] = ["queued", "running"]

# The tables with deleted_at.
_SOFT_DELETED = (
    "chat.threads",
    "chat.conversations",
    "experiments.experiment_descriptions",
    "experiments.runs",
)


def _row_to_thread(row: dict) -> Thread:
    return Thread(
        thread_id=row["thread_id"],
        owner_id=row["owner_id"],
        title=row["title"],
        ssh_key_ref=row["ssh_key_ref"],
        site_keypairs=row["site_keypairs"],
        project_id=row["project_id"],
        description_id=row["description_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_message(row: dict) -> Message:
    return Message(
        message_id=row["message_id"],
        thread_id=row["thread_id"],
        seq=row["seq"],
        role=row["role"],
        kind=row["kind"],
        text=row["text"],
        payload=dict(row["payload"]),
        conversation_id=row["conversation_id"],
        description_id=row["description_id"],
        description_version=row["description_version"],
        created_at=row["created_at"],
    )


def _row_to_job(row: dict) -> Job:
    return Job(
        job_id=row["job_id"],
        thread_id=row["thread_id"],
        owner_id=row["owner_id"],
        kind=row["kind"],
        payload=dict(row["payload"]),
        state=row["state"],
        route=row["route"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_at=row["created_at"],
    )


def _lock_thread(c: psycopg.Connection, thread_id: uuid.UUID) -> str:
    """Take the thread row's write lock and return its owner.

    Every transaction appending to a thread must take this lock first.
    """
    row = c.execute(
        "SELECT owner_id FROM chat.threads WHERE thread_id = %s FOR UPDATE",
        [thread_id],
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Thread {thread_id} not found")
    return str(row["owner_id"])  # type: ignore[call-overload]


def create_thread(
    owner_id: str,
    title: str,
    *,
    project_id: str | None = None,
    ssh_key_ref: str | None = None,
    site_keypairs: dict[str, str] | None = None,
) -> Thread:
    with transaction() as c:
        row = c.execute(
            """
            INSERT INTO chat.threads
                   (thread_id, owner_id, title, project_id, ssh_key_ref, site_keypairs)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            [uuid.uuid4(), owner_id, title, project_id, ssh_key_ref, _as_jsonb(site_keypairs)],
        ).fetchone()
    return _row_to_thread(row)  # type: ignore[arg-type]


def get_thread(owner_id: str, thread_id: uuid.UUID) -> Thread:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat.threads "
            "WHERE thread_id = %s AND owner_id = %s AND deleted_at IS NULL",
            [thread_id, owner_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Thread {thread_id} not found for owner {owner_id!r}")
    return _row_to_thread(row)  # type: ignore[arg-type]


def list_threads(owner_id: str) -> list[Thread]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat.threads WHERE owner_id = %s AND deleted_at IS NULL "
            "ORDER BY updated_at DESC",
            [owner_id],
        ).fetchall()
    return [_row_to_thread(r) for r in rows]


def thread_activity(owner_id: str) -> dict[uuid.UUID, str]:
    """The owner's busy chats, each 'running' or 'reserved'."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT t.thread_id, CASE
              WHEN EXISTS (SELECT 1 FROM chat.jobs j
                            WHERE j.thread_id = t.thread_id AND j.kind IN ('build', 'teardown')
                              AND j.state = ANY(%s)) THEN 'running'
              WHEN EXISTS (SELECT 1 FROM experiments.runs r
                             JOIN experiments.run_resources res USING (run_id)
                            WHERE r.owner_id = t.owner_id AND res.released_at IS NULL
                              AND r.description_id IN (SELECT description_id FROM chat.messages m
                                                        WHERE m.thread_id = t.thread_id))
                THEN 'reserved'
              END AS activity
              FROM chat.threads t
             WHERE t.owner_id = %s AND t.deleted_at IS NULL
            """,
            [_UNFINISHED_JOB_STATES, owner_id],
        ).fetchall()
    return {r["thread_id"]: r["activity"] for r in rows if r["activity"]}


def delete_thread(owner_id: str, thread_id: uuid.UUID) -> None:
    """Stamp a chat, and the descriptions, runs and conversations it made, with one deleted_at.

    Raises ConflictError while the chat has an unfinished job or a run with hardware.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT 1 FROM chat.threads "
            "WHERE thread_id = %s AND owner_id = %s AND deleted_at IS NULL FOR UPDATE",
            [thread_id, owner_id],
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Thread {thread_id} not found for owner {owner_id!r}")
        working = conn.execute(
            """
            SELECT kind FROM chat.jobs
             WHERE thread_id = %s AND state = ANY(%s)
             LIMIT 1
            """,
            [thread_id, _UNFINISHED_JOB_STATES],
        ).fetchone()
        if working is not None:
            raise ConflictError(
                f"Thread {thread_id} is running a {working['kind']}; "
                "it cannot be deleted until that finishes"
            )
        lineages = [
            r["description_id"]
            for r in conn.execute(
                "SELECT DISTINCT description_id FROM chat.messages "
                "WHERE thread_id = %s AND description_id IS NOT NULL",
                [thread_id],
            ).fetchall()
        ]
        holding = conn.execute(
            """
            SELECT runs.run_id FROM experiments.runs
              JOIN experiments.run_resources r USING (run_id)
             WHERE runs.owner_id = %s AND runs.description_id = ANY(%s)
               AND r.released_at IS NULL
             LIMIT 1
            """,
            [owner_id, lineages],
        ).fetchone()
        if holding is not None:
            raise ConflictError(
                f"run {holding['run_id']} still holds hardware; release it before deleting the chat"
            )
        conn.execute("UPDATE chat.threads SET deleted_at = now() WHERE thread_id = %s", [thread_id])
        for table in ("experiments.experiment_descriptions", "experiments.runs"):
            conn.execute(
                f"UPDATE {table} SET deleted_at = now() "
                "WHERE owner_id = %s AND description_id = ANY(%s) AND deleted_at IS NULL",
                [owner_id, lineages],
            )
        conn.execute(
            """
            UPDATE chat.conversations SET deleted_at = now()
             WHERE owner_id = %s AND deleted_at IS NULL
               AND (description_id = ANY(%s) OR conversation_id IN (
                     SELECT conversation_id FROM chat.messages WHERE thread_id = %s
                     UNION
                     SELECT (payload->>'routing_conversation_id')::uuid FROM chat.messages
                      WHERE thread_id = %s))
            """,
            [owner_id, lineages, thread_id, thread_id],
        )


def trash(owner_id: str, restore_days: int) -> list[Trashed]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT thread_id, title, deleted_at,
                   deleted_at + make_interval(days => %(days)s) AS restorable_until
              FROM chat.threads
             WHERE owner_id = %(o)s AND deleted_at > now() - make_interval(days => %(days)s)
             ORDER BY deleted_at DESC
            """,
            {"o": owner_id, "days": restore_days},
        ).fetchall()
    return [Trashed(**r) for r in rows]


def restore_thread(owner_id: str, thread_id: uuid.UUID, restore_days: int) -> Thread:
    """Raises NotFoundError if the chat is not in the owner's trash."""
    with transaction() as conn:
        row = conn.execute(
            """
            SELECT deleted_at FROM chat.threads
             WHERE thread_id = %s AND owner_id = %s
               AND deleted_at > now() - make_interval(days => %s)
             FOR UPDATE
            """,
            [thread_id, owner_id, restore_days],
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Thread {thread_id} is not in the trash of {owner_id!r}")
        for table in _SOFT_DELETED:
            conn.execute(
                f"UPDATE {table} SET deleted_at = NULL WHERE owner_id = %s AND deleted_at = %s",
                [owner_id, row["deleted_at"]],
            )
    return get_thread(owner_id, thread_id)


def update_thread(
    owner_id: str,
    thread_id: uuid.UUID,
    *,
    conn: psycopg.Connection | None = None,
    **fields: Any,
) -> Thread:
    invalid = set(fields) - _ALLOWED_THREAD_UPDATE_FIELDS
    if invalid:
        raise AccessError(f"Cannot update thread field(s): {sorted(invalid)!r}")

    set_parts = [f"{col} = %s" for col in fields]
    params: list[Any] = [_as_jsonb(v) for v in fields.values()]
    set_parts.append("updated_at = now()")
    params.extend([thread_id, owner_id])

    with _translate(), _use_conn(conn) as c:
        row = c.execute(
            f"UPDATE chat.threads SET {', '.join(set_parts)} "
            "WHERE thread_id = %s AND owner_id = %s AND deleted_at IS NULL RETURNING *",
            params,
        ).fetchone()
    if row is None:
        raise NotFoundError(f"Thread {thread_id} not found for owner {owner_id!r}")
    return _row_to_thread(row)  # type: ignore[arg-type]


def append_message(
    thread_id: uuid.UUID,
    role: MessageRole,
    kind: MessageKind,
    text: str,
    payload: dict[str, Any] | None = None,
    *,
    owner_id: str | None = None,
    conversation_id: uuid.UUID | None = None,
    description_id: uuid.UUID | None = None,
    description_version: int | None = None,
    conn: psycopg.Connection | None = None,
) -> Message:
    """Append a message to a thread, allocating seq atomically.

    owner_id, when given, is verified against the thread.
    """
    with _translate(), _use_conn(conn) as c:
        actual_owner = _lock_thread(c, thread_id)
        if owner_id is not None and actual_owner != owner_id:
            raise NotFoundError(f"Thread {thread_id} not found for owner {owner_id!r}")

        seq_row = c.execute(
            "SELECT COALESCE(max(seq), 0) + 1 AS next_seq FROM chat.messages "
            "WHERE thread_id = %s",
            [thread_id],
        ).fetchone()
        row = c.execute(
            """
            INSERT INTO chat.messages
                   (message_id, thread_id, seq, role, kind, text, payload,
                    conversation_id, description_id, description_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            [
                uuid.uuid4(),
                thread_id,
                seq_row["next_seq"],  # type: ignore[index,call-overload]
                role,
                kind,
                text,
                Jsonb(_redact(payload or {})),
                conversation_id,
                description_id,
                description_version,
            ],
        ).fetchone()
        c.execute(
            "UPDATE chat.threads SET updated_at = now() WHERE thread_id = %s",
            [thread_id],
        )
    return _row_to_message(row)  # type: ignore[arg-type]


def read_messages(owner_id: str, thread_id: uuid.UUID) -> list[Message]:
    with get_conn() as conn:
        owner_row = conn.execute(
            "SELECT 1 FROM chat.threads WHERE thread_id = %s AND owner_id = %s",
            [thread_id, owner_id],
        ).fetchone()
        if owner_row is None:
            raise NotFoundError(f"Thread {thread_id} not found for owner {owner_id!r}")

        rows = conn.execute(
            "SELECT * FROM chat.messages WHERE thread_id = %s ORDER BY seq",
            [thread_id],
        ).fetchall()
    return [_row_to_message(r) for r in rows]


def save_feedback(
    owner_id: str,
    thread_id: uuid.UUID,
    message_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    rating: str | None,
    comment: str | None,
) -> Feedback:
    """Rate a message or a run, replacing its last rating, or comment on the chat.

    Raises NotFoundError unless the message or run is in the owner's chat.
    """
    with transaction() as c:
        found = c.execute(
            """
            SELECT 1 FROM chat.threads t
             WHERE t.thread_id = %(t)s AND t.owner_id = %(o)s AND t.deleted_at IS NULL
               AND (%(m)s::uuid IS NULL OR EXISTS (
                     SELECT 1 FROM chat.messages m
                      WHERE m.message_id = %(m)s AND m.thread_id = t.thread_id
                        AND m.role = 'assistant'))
               AND (%(r)s::uuid IS NULL OR EXISTS (
                     SELECT 1 FROM experiments.runs r
                      WHERE r.run_id = %(r)s AND r.owner_id = t.owner_id
                        AND r.deleted_at IS NULL
                        AND (r.description_id = t.description_id OR r.description_id IN (
                              SELECT m.description_id FROM chat.messages m
                               WHERE m.thread_id = t.thread_id))))
            """,
            {"t": thread_id, "o": owner_id, "m": message_id, "r": run_id},
        ).fetchone()
        if found is None:
            raise NotFoundError(f"No such message or run in thread {thread_id}")
        row = c.execute(
            sql.SQL("""
            INSERT INTO chat.feedback
                   (feedback_id, owner_id, thread_id, message_id, run_id, rating, comment)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ({}) DO UPDATE SET
              rating = EXCLUDED.rating, comment = EXCLUDED.comment, updated_at = now()
            RETURNING {}
            """).format(
                sql.Identifier("message_id" if message_id else "run_id"),
                sql.SQL(_FEEDBACK_COLUMNS),
            ),
            [uuid.uuid4(), owner_id, thread_id, message_id, run_id, rating, comment],
        ).fetchone()
    return Feedback(**row)  # type: ignore[arg-type]


def thread_feedback(owner_id: str, thread_id: uuid.UUID) -> list[Feedback]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_FEEDBACK_COLUMNS} FROM chat.feedback "
            "WHERE thread_id = %s AND owner_id = %s ORDER BY created_at",
            [thread_id, owner_id],
        ).fetchall()
    return [Feedback(**r) for r in rows]


def listed_feedback(limit: int, offset: int) -> list[ListedFeedback]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT feedback_id, updated_at, owner_id, username, email, thread_id,
                   thread_title, rating, comment, message_kind, message_text,
                   run_id, run_state, run_failure
              FROM chat.feedback_context
             ORDER BY updated_at DESC, feedback_id
             LIMIT %s OFFSET %s
            """,
            [limit, offset],
        ).fetchall()
    return [ListedFeedback(**r) for r in rows]


def feedback_detail(feedback_id: uuid.UUID) -> FeedbackDetail:
    """Raises NotFoundError if there is no such feedback."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat.feedback_context WHERE feedback_id = %s", [feedback_id]
        ).fetchone()
        if row is None:
            raise NotFoundError(f"No feedback {feedback_id}")
        turns = conn.execute(
            "SELECT payload || jsonb_build_object('sent', model_input) AS turn "
            "FROM experiments.run_events WHERE run_id = %s AND kind = 'model_turn' "
            "ORDER BY seq",
            [row["run_id"]],
        ).fetchall()
    return FeedbackDetail(**row, build_turns=[t["turn"] for t in turns])


def enqueue_job(
    owner_id: str,
    thread_id: uuid.UUID,
    kind: JobKind,
    payload: dict[str, Any],
    *,
    conn: psycopg.Connection | None = None,
) -> Job:
    """Raises ConflictError if the thread already has an unfinished job of this kind."""
    try:
        with _translate(), _use_conn(conn) as c:
            actual_owner = _lock_thread(c, thread_id)
            if actual_owner != owner_id:
                raise NotFoundError(
                    f"Thread {thread_id} not found for owner {owner_id!r}"
                )
            row = c.execute(
                """
                INSERT INTO chat.jobs (job_id, thread_id, owner_id, kind, payload, state)
                VALUES (%s, %s, %s, %s, %s, 'queued')
                RETURNING *
                """,
                [uuid.uuid4(), thread_id, owner_id, kind, Jsonb(payload)],
            ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        raise ConflictError(
            f"Thread {thread_id} already has an unfinished {kind} job"
        ) from exc
    return _row_to_job(row)  # type: ignore[arg-type]


def unfinished_jobs(owner_id: str, thread_id: uuid.UUID) -> list[Job]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM chat.jobs
             WHERE thread_id = %s AND owner_id = %s AND state = ANY(%s::text[])
             ORDER BY created_at
            """,
            [thread_id, owner_id, _UNFINISHED_JOB_STATES],
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def any_build_running() -> bool:
    """Not owner-scoped."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT 1 FROM chat.jobs WHERE kind = 'build' AND state = 'running' LIMIT 1"
        ).fetchone() is not None


def start_job(job_id: uuid.UUID) -> Job | None:
    """Mark a queued job running. Not owner-scoped.

    Returns None if the job is no longer queued.
    """
    with _translate(), get_conn() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE chat.jobs SET state = 'running', started_at = now() "
            "WHERE job_id = %s AND state = 'queued' RETURNING *",
            [job_id],
        ).fetchone()
    return _row_to_job(row) if row is not None else None


def expire_stale_jobs(owner_id: str, older_than_s: float, error: str) -> list[Job]:
    """Fail the owner's jobs that have been running longer than older_than_s."""
    with _translate(), get_conn() as conn, conn.transaction():
        rows = conn.execute(
            "UPDATE chat.jobs SET state = 'error', error = %s, finished_at = now() "
            "WHERE owner_id = %s AND state = 'running' "
            "AND started_at < now() - make_interval(secs => %s) RETURNING *",
            [error, owner_id, older_than_s],
        ).fetchall()
    return [_row_to_job(r) for r in rows]


def route_job(job_id: uuid.UUID, route: str) -> Job:
    """Record what the router decided. Not owner-scoped."""
    return _set_job(
        job_id,
        "UPDATE chat.jobs SET route = %s WHERE job_id = %s RETURNING *",
        [route, job_id],
    )


def finish_job(job_id: uuid.UUID, error: str | None = None) -> Job:
    """Settle a job, `done` or `error`. Not owner-scoped."""
    return _set_job(
        job_id,
        "UPDATE chat.jobs SET state = %s, error = %s, finished_at = now() "
        "WHERE job_id = %s RETURNING *",
        ["error" if error else "done", error, job_id],
    )


def _set_job(job_id: uuid.UUID, sql: str, params: list[Any]) -> Job:
    with _translate(), get_conn() as conn, conn.transaction():
        row = conn.execute(sql, params).fetchone()
    if row is None:
        raise NotFoundError(f"Job {job_id} not found")
    return _row_to_job(row)  # type: ignore[arg-type]


@contextmanager
def transaction() -> Generator[psycopg.Connection[Any], None, None]:
    with get_conn() as c, c.transaction():
        yield c


def description_versions(owner_id: str, description_id: uuid.UUID) -> list[Description]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM experiments.experiment_descriptions
             WHERE description_id = %s AND owner_id = %s AND deleted_at IS NULL
             ORDER BY version
            """,
            [description_id, owner_id],
        ).fetchall()
    return [_row_to_description(r) for r in rows]


def thread_for_description(
    owner_id: str, description_id: uuid.UUID
) -> Thread | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT t.*
              FROM chat.threads t
              JOIN chat.messages m ON m.thread_id = t.thread_id
             WHERE m.description_id = %s AND t.owner_id = %s
             ORDER BY m.seq
             LIMIT 1
            """,
            [description_id, owner_id],
        ).fetchone()
    return _row_to_thread(row) if row else None  # type: ignore[arg-type]


def current_system_key() -> SystemKey | None:
    """The key installed on new instances, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM auth.system_keys WHERE retired_at IS NULL"
        ).fetchone()
    return SystemKey(**row) if row else None


def system_key(fingerprint: str) -> SystemKey:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM auth.system_keys WHERE fingerprint = %s", [fingerprint]
        ).fetchone()
    if row is None:
        raise NotFoundError(f"No system key {fingerprint}")
    return SystemKey(**row)


def system_keys() -> list[SystemKey]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM auth.system_keys ORDER BY created_at DESC"
        ).fetchall()
    return [SystemKey(**r) for r in rows]


def add_system_key(fingerprint: str, private_key: str) -> SystemKey:
    """Make this the current key if there is none, and return the current key."""
    with transaction() as c:
        c.execute(
            """
            INSERT INTO auth.system_keys (fingerprint, private_key)
            VALUES (%s, %s)
            ON CONFLICT ((retired_at IS NULL)) WHERE retired_at IS NULL DO NOTHING
            """,
            [fingerprint, private_key],
        )
        row = c.execute(
            "SELECT * FROM auth.system_keys WHERE retired_at IS NULL"
        ).fetchone()
    return SystemKey(**row)  # type: ignore[arg-type]


def rotate_system_key(fingerprint: str, private_key: str) -> SystemKey:
    """Retire the current key and make this one current."""
    with transaction() as c:
        c.execute(
            "UPDATE auth.system_keys SET retired_at = now() WHERE retired_at IS NULL"
        )
        row = c.execute(
            """
            INSERT INTO auth.system_keys (fingerprint, private_key)
            VALUES (%s, %s)
            RETURNING *
            """,
            [fingerprint, private_key],
        ).fetchone()
    return SystemKey(**row)  # type: ignore[arg-type]


def import_user_key(
    owner_id: str, name: str, keypair_name: str, public_key: str
) -> UserKey:
    """Store a public key under name, replacing the key but not the keypair name."""
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO auth.user_keys (owner_id, name, keypair_name, public_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (owner_id, name)
              DO UPDATE SET public_key = EXCLUDED.public_key, updated_at = now()
            RETURNING *
            """,
            [owner_id, name, keypair_name, public_key],
        ).fetchone()
    return UserKey(**row)  # type: ignore[arg-type]


def user_keys(owner_id: str) -> list[UserKey]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM auth.user_keys WHERE owner_id = %s ORDER BY name", [owner_id]
        ).fetchall()
    return [UserKey(**r) for r in rows]


def user_key_for_keypair(owner_id: str, keypair_name: str) -> UserKey | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM auth.user_keys WHERE owner_id = %s AND keypair_name = %s",
            [owner_id, keypair_name],
        ).fetchone()
    return UserKey(**row) if row else None


def record_login(
    user_id: str,
    username: str,
    projects: list[dict[str, Any]],
    refresh_token: str,
    session_hash: str,
    session_expires_at: datetime,
    *,
    email: str | None,
) -> None:
    """Store who logged in as approved, with their encrypted refresh token and new session."""
    with transaction() as c:
        c.execute(
            """
            INSERT INTO auth.users (user_id, username, email, projects, checked_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (user_id) DO UPDATE SET
              username = EXCLUDED.username, email = EXCLUDED.email,
              projects = EXCLUDED.projects, checked_at = now(), approval = 'approved',
              approval_decided_at = CASE WHEN auth.users.approval = 'approved'
                                         THEN auth.users.approval_decided_at ELSE now() END
            """,
            [user_id, username, email, Jsonb(projects)],
        )
        c.execute(
            """
            INSERT INTO auth.refresh_tokens (user_id, refresh_token)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
              refresh_token = EXCLUDED.refresh_token, updated_at = now()
            """,
            [user_id, refresh_token],
        )
        _start_session(c, user_id, session_hash, session_expires_at)


def request_approval(
    user_id: str, username: str, projects: list[dict[str, Any]], *, email: str | None
) -> None:
    """Store a new user as waiting for approval."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth.users
              (user_id, username, email, projects, checked_at, approval, approval_requested_at)
            VALUES (%s, %s, %s, %s, now(), 'waiting', now())
            ON CONFLICT (user_id) DO UPDATE SET
              username = EXCLUDED.username, email = EXCLUDED.email,
              projects = EXCLUDED.projects, checked_at = now()
            """,
            [user_id, username, email, Jsonb(projects)],
        )


def start_session(user_id: str, session_hash: str, session_expires_at: datetime) -> None:
    with transaction() as c:
        _start_session(c, user_id, session_hash, session_expires_at)


def _start_session(
    c: psycopg.Connection[Any], user_id: str, session_hash: str, expires_at: datetime
) -> None:
    c.execute(
        "INSERT INTO auth.sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
        [session_hash, user_id, expires_at],
    )
    c.execute(
        "UPDATE auth.users "
        "SET last_login_at = now(), first_login_at = coalesce(first_login_at, now()) "
        "WHERE user_id = %s",
        [user_id],
    )


def session_user(session_hash: str) -> User | None:
    """Return the session's user, or None if it has expired."""
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT {_USER_COLUMNS} FROM auth.users WHERE disabled_at IS NULL AND user_id = (
              SELECT user_id FROM auth.sessions WHERE token_hash = %s AND expires_at > now()
            )
            """,
            [session_hash],
        ).fetchone()
    return User(**row) if row else None


def update_login(user_id: str, username: str, projects: list[dict[str, Any]]) -> User:
    with get_conn() as conn:
        row = conn.execute(
            f"""
            UPDATE auth.users SET username = %s, projects = %s, checked_at = now()
             WHERE user_id = %s RETURNING {_USER_COLUMNS}
            """,
            [username, Jsonb(projects), user_id],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"No user {user_id!r}")
    return User(**row)


def claim_check(user_id: str, older_than_s: float) -> bool:
    """True for only one of several concurrent callers."""
    with get_conn() as conn:
        return conn.execute(
            "UPDATE auth.users SET checked_at = now() "
            "WHERE user_id = %s AND checked_at < now() - make_interval(secs => %s)",
            [user_id, older_than_s],
        ).rowcount == 1


def end_session(session_hash: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM auth.sessions WHERE token_hash = %s", [session_hash])


def end_sessions(user_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM auth.sessions WHERE user_id = %s", [user_id])


def end_sessions_at(
    user_id: str, expires_at: datetime, longest_s: float, conn: psycopg.Connection[Any]
) -> None:
    """End the user's live sessions at expires_at, at most longest_s after each began."""
    conn.execute(
        "UPDATE auth.sessions "
        "SET expires_at = least(created_at + make_interval(secs => %s), %s) "
        "WHERE user_id = %s AND expires_at > now()",
        [longest_s, expires_at, user_id],
    )


def get_user(user_id: str) -> User:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_USER_COLUMNS} FROM auth.users WHERE user_id = %s", [user_id]
        ).fetchone()
    if row is None:
        raise NotFoundError(f"No user {user_id!r}; they have not logged in")
    return User(**row)


def locked_refresh_token(user_id: str, conn: psycopg.Connection[Any]) -> str | None:
    """The user's encrypted refresh token, locked until conn's transaction ends."""
    row = conn.execute(
        "SELECT refresh_token FROM auth.refresh_tokens WHERE user_id = %s FOR UPDATE",
        [user_id],
    ).fetchone()
    return row["refresh_token"] if row else None


def save_refresh_token(user_id: str, refresh_token: str, conn: psycopg.Connection[Any]) -> None:
    conn.execute(
        "UPDATE auth.refresh_tokens SET refresh_token = %s, updated_at = now() WHERE user_id = %s",
        [refresh_token, user_id],
    )


def ensure_user(user_id: str) -> None:
    """Create a user with no login for user_id, if there is none."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO auth.users (user_id, username) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO NOTHING",
            [user_id, user_id.partition(":")[2]],
        )


def update_user(user_id: str, **fields: str | int | dict[str, str] | None) -> None:
    """Set any of the user's defaults and quota overrides. None clears one.

    Raises NotFoundError.
    """
    unknown = set(fields) - {
        "default_project_id", "default_ssh_key_ref", "default_site_keypairs", "default_vm_site",
        "default_baremetal_site", *QUOTAS,
    }
    if unknown:
        raise AccessError(f"Cannot set user field(s): {sorted(unknown)!r}")
    if not fields:
        return
    with get_conn() as conn:
        updated = conn.execute(
            f"UPDATE auth.users SET {', '.join(f'{k} = %s' for k in fields)} "
            "WHERE user_id = %s",
            [*(_as_jsonb(v) for v in fields.values()), user_id],
        ).rowcount
    if not updated:
        raise NotFoundError(f"No user {user_id!r}")


def delete_user_key(owner_id: str, name: str) -> UserKey:
    """Delete the owner's imported key and return it. Raises NotFoundError."""
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM auth.user_keys WHERE owner_id = %s AND name = %s RETURNING *",
            [owner_id, name],
        ).fetchone()
    if row is None:
        raise NotFoundError(f"No imported key {name!r}")
    return UserKey(**row)


def user_summaries(
    user_id: str | None = None, limit: int | None = None, offset: int = 0
) -> list[UserSummary]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT u.user_id,
                   split_part(u.user_id, ':', 1) AS testbed,
                   u.username,
                   u.email,
                   u.projects,
                   u.password_hash IS NOT NULL AS local,
                   u.first_login_at,
                   u.last_login_at,
                   u.checked_at,
                   u.disabled_at,
                   u.disabled_reason,
                   (SELECT count(*) FROM auth.sessions s
                     WHERE s.user_id = u.user_id AND s.expires_at > now()) AS open_sessions,
                   (SELECT count(*) FROM chat.threads t
                     WHERE t.owner_id = u.user_id AND t.deleted_at IS NULL) AS chats,
                   (SELECT max(t.updated_at) FROM chat.threads t
                     WHERE t.owner_id = u.user_id AND t.deleted_at IS NULL) AS latest_chat_at,
                   coalesce(u.max_messages_per_day, u.max_builds_per_user,
                            u.max_active_experiments_per_user) IS NOT NULL AS overridden,
                   u.approval,
                   u.approval_requested_at,
                   u.approval_decided_at
              FROM auth.users u
             WHERE %(u)s::text IS NULL OR u.user_id = %(u)s
             ORDER BY u.approval = 'waiting' DESC, testbed, u.username
             LIMIT %(limit)s OFFSET %(offset)s
            """,
            {"u": user_id, "limit": limit, "offset": offset},
        ).fetchall()
    return [UserSummary(**r) for r in rows]


def add_local_user(user_id: str, password_hash: str) -> None:
    """Raises ConflictError if user_id is taken."""
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO auth.users (user_id, username, password_hash) VALUES (%s, %s, %s)",
                [user_id, user_id.partition(":")[2], password_hash],
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError(f"{user_id} already exists") from exc


def local_password_hash(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT password_hash FROM auth.users WHERE user_id = %s", [user_id]
        ).fetchone()
    return row["password_hash"] if row else None


def set_local_password(user_id: str, password_hash: str) -> None:
    """Raises NotFoundError if user_id is not a local user."""
    with get_conn() as conn:
        updated = conn.execute(
            "UPDATE auth.users SET password_hash = %s "
            "WHERE user_id = %s AND password_hash IS NOT NULL",
            [password_hash, user_id],
        ).rowcount
    if not updated:
        raise NotFoundError(f"No local user {user_id}")


def delete_local_user(user_id: str) -> None:
    """Delete a local user and everything they own.

    Raises NotFoundError if user_id is not a local user, and ConflictError while one
    of their runs has a live resource.
    """
    with transaction() as c:
        found = c.execute(
            "SELECT 1 FROM auth.users WHERE user_id = %s AND password_hash IS NOT NULL FOR UPDATE",
            [user_id],
        ).fetchone()
        if found is None:
            raise NotFoundError(f"No local user {user_id}")
        live = c.execute(
            """
            SELECT count(*) AS n FROM experiments.run_resources r
              JOIN experiments.runs USING (run_id)
             WHERE runs.owner_id = %s AND r.released_at IS NULL
            """,
            [user_id],
        ).fetchone()
        if live and live["n"]:
            raise ConflictError(
                f"{user_id} still holds {live['n']} live resource(s); tear their runs down first"
            )
        c.execute("DELETE FROM auth.users WHERE user_id = %s", [user_id])


def disabled_reason(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT disabled_reason FROM auth.users WHERE user_id = %s AND disabled_at IS NOT NULL",
            [user_id],
        ).fetchone()
    return row["disabled_reason"] if row else None


def disable_user(user_id: str, reason: str) -> None:
    """Also ends the user's sessions."""
    with transaction() as conn:
        if not conn.execute(
            "UPDATE auth.users SET disabled_at = now(), disabled_reason = %s WHERE user_id = %s",
            [reason, user_id],
        ).rowcount:
            raise NotFoundError(f"No user {user_id!r}")
        conn.execute("DELETE FROM auth.sessions WHERE user_id = %s", [user_id])


def enable_user(user_id: str) -> None:
    with get_conn() as conn:
        if not conn.execute(
            "UPDATE auth.users SET disabled_at = NULL, disabled_reason = NULL WHERE user_id = %s",
            [user_id],
        ).rowcount:
            raise NotFoundError(f"No user {user_id!r}")


def approval(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT approval FROM auth.users WHERE user_id = %s", [user_id]
        ).fetchone()
    return row["approval"] if row else None


def approve_user(user_id: str) -> None:
    """Raises NotFoundError."""
    with get_conn() as conn:
        if not conn.execute(
            "UPDATE auth.users SET approval = 'approved', "
            "approval_decided_at = CASE WHEN approval = 'approved' "
            "                           THEN approval_decided_at ELSE now() END "
            "WHERE user_id = %s",
            [user_id],
        ).rowcount:
            raise NotFoundError(f"No user {user_id!r}")


def deny_user(user_id: str) -> None:
    """Raises NotFoundError, or ConflictError if the user is approved."""
    with transaction() as conn:
        row = conn.execute(
            "SELECT approval FROM auth.users WHERE user_id = %s FOR UPDATE", [user_id]
        ).fetchone()
        if row is None:
            raise NotFoundError(f"No user {user_id!r}")
        if row["approval"] == "approved":
            raise ConflictError("use Disable to lock out an approved account")
        conn.execute(
            "UPDATE auth.users SET approval = 'denied', approval_decided_at = now() "
            "WHERE user_id = %s",
            [user_id],
        )


def waiting_users() -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT count(*) AS n FROM auth.users WHERE approval = 'waiting'"
        ).fetchone()
    return int(row["n"]) if row else 0


def require_approval() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT require_approval FROM auth.settings").fetchone()
    return row is None or bool(row["require_approval"])


def set_require_approval(required: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE auth.settings SET require_approval = %s", [required])


def everyone_limits() -> dict[str, int | None]:
    """The limits for everyone. None means the config file's value."""
    with get_conn() as conn:
        row = conn.execute(f"SELECT {', '.join(QUOTAS)} FROM auth.settings").fetchone()
    return {q: row[q] if row else None for q in QUOTAS}


def set_everyone_limits(**limits: int | None) -> None:
    """None clears one."""
    unknown = set(limits) - set(QUOTAS)
    if unknown:
        raise AccessError(f"Cannot set limit(s): {sorted(unknown)!r}")
    if not limits:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE auth.settings SET {', '.join(f'{k} = %s' for k in limits)}",
            list(limits.values()),
        )


def usage(owner_id: str, conn: psycopg.Connection[Any]) -> Usage:
    """Locks the owner's row until conn's transaction ends."""
    conn.execute("SELECT 1 FROM auth.users WHERE user_id = %s FOR UPDATE", [owner_id])
    row = conn.execute(
        """
        SELECT today.messages_today, today.next_message_at,
          (SELECT count(*) FROM chat.jobs
            WHERE owner_id = %(o)s AND kind = 'build' AND state = ANY(%(unfinished)s))
            AS builds_running,
          (SELECT count(DISTINCT r.run_id) FROM experiments.runs r
             JOIN experiments.run_resources res USING (run_id)
            WHERE r.owner_id = %(o)s AND res.released_at IS NULL
              AND NOT (r.state IN ('planning', 'provisioning', 'configuring', 'validating')
                       AND EXISTS (SELECT 1 FROM chat.jobs j
                                    WHERE j.owner_id = %(o)s AND j.kind = 'build'
                                      AND j.state = ANY(%(unfinished)s)
                                      AND j.payload->>'description_id' = r.description_id::text)))
            AS holding
          FROM (SELECT count(*) AS messages_today,
                       min(m.created_at) + interval '1 day' AS next_message_at
                  FROM chat.messages m JOIN chat.threads t USING (thread_id)
                 WHERE t.owner_id = %(o)s AND m.kind = 'user'
                   AND m.created_at > now() - interval '1 day') today
        """,
        {"o": owner_id, "unfinished": _UNFINISHED_JOB_STATES},
    ).fetchone()
    return Usage(
        messages_today=row["messages_today"],  # type: ignore[index]
        next_message_at=row["next_message_at"],  # type: ignore[index]
        builds_running=row["builds_running"],  # type: ignore[index]
        active_experiments=row["holding"] + row["builds_running"],  # type: ignore[index]
    )


def active_experiments(owner_id: str) -> list[ActiveExperiment]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.run_id, r.state, min(res.site) AS site,
                   r.connection->>'expires_at' AS ends_at, chat.thread_id, chat.title,
                   json_agg(json_build_object(
                       'kind', res.kind, 'provider_id', res.provider_id, 'site', res.site
                   ) ORDER BY res.created_at) AS resources
              FROM experiments.runs r
              JOIN experiments.run_resources res
                ON res.run_id = r.run_id AND res.released_at IS NULL
              LEFT JOIN LATERAL (
                SELECT t.thread_id, t.title FROM chat.messages m
                  JOIN chat.threads t USING (thread_id)
                 WHERE m.description_id = r.description_id AND t.owner_id = r.owner_id
                 ORDER BY m.seq LIMIT 1
              ) chat ON true
             WHERE r.owner_id = %s
             GROUP BY r.run_id, chat.thread_id, chat.title
             ORDER BY r.created_at DESC
            """,
            [owner_id],
        ).fetchall()
    return [ActiveExperiment(**r) for r in rows]


def announcement() -> Announcement | None:
    with get_conn() as conn:
        row = conn.execute("SELECT text, posted_at FROM chat.announcement").fetchone()
    return Announcement(**row) if row else None


def post_announcement(text: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chat.announcement (text) VALUES (%s)
            ON CONFLICT (only_one) DO UPDATE SET text = EXCLUDED.text, posted_at = now()
            """,
            [text],
        )


def clear_announcement() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM chat.announcement")
