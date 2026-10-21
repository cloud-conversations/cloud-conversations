"""Settings for the API and the workers."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

DEFAULT_BROKER_URL = "redis://localhost:6379/0"

BROKER_URL_ENV = "CC_BROKER_URL"

# What a thread is called until its first message names it.
NEW_THREAD_TITLE = "New experiment"

# Per-user limits. Each is a Service field and an auth.users column.
QUOTAS = ("max_messages_per_day", "max_builds_per_user", "max_active_experiments_per_user")

# The queues tasks are routed to.
CHAT_QUEUE = "chat"
RUNS_QUEUE = "runs"

# Seconds a job gets after its soft time limit before it is killed.
JOB_KILL_GRACE_S = 60


class Service(BaseModel):
    """The tunable values the API and the workers share."""

    model_config = {"extra": "forbid", "frozen": True}

    # Prompts and testbed guidance.
    bundle_dir: str = "bundle"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    broker_url: str = DEFAULT_BROKER_URL
    # A job is stopped at this many seconds.
    job_timeout_s: float = Field(default=5400.0, gt=0)
    # Thread messages the router sees.
    route_history: int = Field(default=6, ge=0)
    thread_title_chars: int = Field(default=60, ge=1)
    oidc_scope: str = "openid profile email"
    # Each call to the login provider, in seconds.
    oidc_s: float = Field(default=10.0, gt=0)
    # A login must finish within this many seconds of starting.
    login_s: float = Field(default=600.0, gt=0)
    session_days: float = Field(default=30.0, gt=0)
    # Seconds between rechecks of a login.
    login_check_s: float = Field(default=300.0, gt=0)
    # Days a deleted chat stays in the trash.
    restore_days: int = Field(default=30, ge=1)
    # Choices of feedback per page on the Admin page, default first.
    feedback_page_sizes: list[Annotated[int, Field(ge=1)]] = Field(
        default=[5, 25, 100], min_length=1
    )
    # Choices of users per page on the Admin page, default first.
    user_page_sizes: list[Annotated[int, Field(ge=1)]] = Field(default=[25, 100], min_length=1)
    max_messages_per_day: int = Field(default=200, ge=1)
    max_builds_per_user: int = Field(default=2, ge=1)
    # Counts runs with hardware, and unfinished builds.
    max_active_experiments_per_user: int = Field(default=4, ge=1)
    # Whether local test users may log in with a password.
    local_login: bool = False
    # The testbed local test users belong to.
    local_users_testbed: str | None = None

    @property
    def session_s(self) -> float:
        return self.session_days * 86400

    @property
    def job_kill_s(self) -> float:
        return self.job_timeout_s + JOB_KILL_GRACE_S

    @model_validator(mode="after")
    def _local_users_have_a_testbed(self) -> Service:
        if self.local_login and self.local_users_testbed is None:
            raise ValueError("local_login needs local_users_testbed")
        return self
