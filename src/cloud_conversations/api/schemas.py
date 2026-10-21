"""API request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, model_validator

from cloud_conversations.schema.description import Check, Requirements
from cloud_conversations.schema.runs import CheckResult, Connection, Failure, RunState

MAX_MESSAGE_CHARS = 4000
MAX_PUBLIC_KEY_CHARS = 8000


class NewMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class FeedbackIn(BaseModel):
    message_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    rating: Literal["up", "down"] | None = None
    comment: str | None = Field(default=None, min_length=1, max_length=MAX_MESSAGE_CHARS)

    @model_validator(mode="after")
    def _one_thing_said(self) -> FeedbackIn:
        if self.message_id and self.run_id:
            raise ValueError("rate a message or a run, not both")
        if self.rating is None and self.comment is None:
            raise ValueError("give a rating or a comment")
        return self


class FeedbackOut(BaseModel):
    model_config = {"from_attributes": True}

    feedback_id: uuid.UUID
    message_id: uuid.UUID | None
    run_id: uuid.UUID | None
    rating: str | None
    comment: str | None
    updated_at: datetime


class ListedFeedbackOut(BaseModel):
    model_config = {"from_attributes": True}

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


class FeedbackDetailOut(ListedFeedbackOut):
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


class FeedbackPage(BaseModel):
    feedback: list[ListedFeedbackOut]
    # Whether there is an older page.
    more: bool
    size: int
    sizes: list[int]


class ThreadPatch(BaseModel):
    ssh_key_ref: str | None = Field(default=None, min_length=1, max_length=200)
    # A keypair name at every site of the testbed.
    site_keypairs: dict[str, str] | None = None
    project_id: str | None = Field(default=None, min_length=1, max_length=200)


class KeyImport(BaseModel):
    name: str
    public_key: str = Field(min_length=1, max_length=MAX_PUBLIC_KEY_CHARS)


class KeyOut(BaseModel):
    name: str
    keypair_name: str
    fingerprint: str


class MeOut(BaseModel):
    testbed: str
    username: str
    projects: list[dict[str, Any]]
    admin: bool
    # Accounts waiting for approval. 0 unless the caller is an admin.
    waiting_accounts: int


class LoginOption(BaseModel):
    testbed: str
    label: str


class AuthMethods(BaseModel):
    local: bool
    logins: list[LoginOption]
    refused: str | None = None


class LocalLogin(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class QuotaOut(BaseModel):
    name: str
    used: int
    limit: int
    default: int
    override: int | None


class ActiveExperimentOut(BaseModel):
    model_config = {"from_attributes": True}

    run_id: uuid.UUID
    state: str
    site: str
    ends_at: str | None
    thread_id: uuid.UUID | None
    title: str | None
    resources: list[ResourceOut]


class ProfileOut(BaseModel):
    testbed: str
    username: str
    projects: list[dict[str, Any]]
    default_project_id: str | None
    default_ssh_key_ref: str | None
    default_site_keypairs: dict[str, str] | None
    default_vm_site: str | None
    default_baremetal_site: str | None
    keys: list[KeyOut]
    sites: list[str]
    vm_sites: list[str]
    baremetal_sites: list[str]
    quotas: list[QuotaOut]
    active_experiments: list[ActiveExperimentOut]


class ProfilePatch(BaseModel):
    default_project_id: str | None = Field(default=None, min_length=1, max_length=200)
    # null clears a default.
    default_ssh_key_ref: str | None = Field(default=None, min_length=1, max_length=200)
    default_site_keypairs: dict[str, str] | None = None
    default_vm_site: str | None = Field(default=None, min_length=1, max_length=200)
    default_baremetal_site: str | None = Field(default=None, min_length=1, max_length=200)


class KeypairOut(BaseModel):
    name: str


class SiteOutcome(BaseModel):
    site: str
    outcome: Literal["deleted", "absent", "failed"]
    error: str | None = None


class KeyDeleted(BaseModel):
    name: str
    keypair_name: str
    sites: list[SiteOutcome]


class UserSummaryOut(BaseModel):
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
    admin: bool
    approval: Literal["approved", "waiting", "denied"]
    approval_requested_at: datetime | None
    approval_decided_at: datetime | None


class UserDetailOut(UserSummaryOut):
    quotas: list[QuotaOut]


class UsersPage(BaseModel):
    users: list[UserSummaryOut]
    # Whether there is a next page.
    more: bool
    size: int
    sizes: list[int]


class LimitOut(BaseModel):
    name: str
    # The value in the config file.
    default: int
    override: int | None


class AdminSettingsOut(BaseModel):
    require_approval: bool
    limits: list[LimitOut]


class AdminSettingsPatch(BaseModel):
    require_approval: bool | None = None
    # null clears an override.
    max_messages_per_day: int | None = Field(default=None, ge=1)
    max_builds_per_user: int | None = Field(default=None, ge=1)
    max_active_experiments_per_user: int | None = Field(default=None, ge=1)


class UserPatch(BaseModel):
    disabled: bool | None = None
    approval: Literal["approved", "denied"] | None = None
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    # null clears an override.
    max_messages_per_day: int | None = Field(default=None, ge=1)
    max_builds_per_user: int | None = Field(default=None, ge=1)
    max_active_experiments_per_user: int | None = Field(default=None, ge=1)


class AnnouncementOut(BaseModel):
    model_config = {"from_attributes": True}

    text: str
    posted_at: datetime


class NewAnnouncement(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class SystemKeyOut(BaseModel):
    fingerprint: str
    created_at: datetime
    retired_at: datetime | None
    authorized_key: str


class Approval(BaseModel):
    description_id: uuid.UUID
    version: int = Field(ge=1)


class ThreadSummary(BaseModel):
    model_config = {"from_attributes": True}

    thread_id: uuid.UUID
    title: str
    ssh_key_ref: str | None
    site_keypairs: dict[str, str] | None
    project_id: str | None


class ThreadListed(ThreadSummary):
    activity: Literal["running", "reserved"] | None = None


class TrashedThread(BaseModel):
    model_config = {"from_attributes": True}

    thread_id: uuid.UUID
    title: str
    deleted_at: datetime
    restorable_until: datetime


class MessageOut(BaseModel):
    model_config = {"from_attributes": True}

    message_id: uuid.UUID
    role: str
    kind: str
    text: str
    payload: dict[str, Any]
    description_id: uuid.UUID | None
    description_version: int | None
    created_at: datetime


class JobOut(BaseModel):
    model_config = {"from_attributes": True}

    job_id: uuid.UUID
    kind: str
    route: str | None
    payload: dict[str, Any] = Field(exclude=True)
    # The job is queued behind another build.
    waiting: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def run_id(self) -> uuid.UUID | None:
        """The run to release, for a teardown job."""
        named = self.payload.get("run_id")
        return uuid.UUID(str(named)) if named else None


class Posted(BaseModel):
    """What posting a message returns."""

    message: MessageOut
    job: JobOut


class VersionOut(BaseModel):
    model_config = {"from_attributes": True}

    description_id: uuid.UUID
    version: int
    requirements: Requirements
    rubric: list[Check]
    unsupported: list[str]
    open_questions: list[str]


class RunSummary(BaseModel):
    model_config = {"from_attributes": True}

    run_id: uuid.UUID
    description_version: int
    state: RunState
    created_at: datetime


class Limits(BaseModel):
    """Limits on each approved build."""

    model_config = {"from_attributes": True}

    max_build_rounds: int
    max_leases_per_build: int


class ThreadOut(BaseModel):
    thread: ThreadSummary
    messages: list[MessageOut]
    versions: list[VersionOut]
    jobs: list[JobOut]
    runs: list[RunSummary]
    limits: Limits
    # The user's answer to each agent proposal, by request ID.
    decisions: dict[uuid.UUID, str]
    feedback: list[FeedbackOut]


class ResourceOut(BaseModel):
    model_config = {"from_attributes": True}

    kind: str
    provider_id: str
    site: str


class Release(BaseModel):
    """The resources the user confirmed releasing."""

    resources: list[ResourceOut]


class RunOut(BaseModel):
    description_version: int
    state: RunState
    resume_count: int
    results: list[CheckResult] | None
    connection: Connection | None
    failure: Failure | None
    live_resources: list[ResourceOut]


class EventOut(BaseModel):
    model_config = {"from_attributes": True}

    seq: int
    at: datetime
    kind: str
    payload: dict[str, Any]
