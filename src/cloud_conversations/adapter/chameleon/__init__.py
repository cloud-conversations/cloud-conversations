"""The Chameleon testbed: OpenStack with Blazar reservations, and its reference API."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_conversations.adapter.chameleon.adapter import ChameleonAdapter
    from cloud_conversations.adapter.credentials import UserScope
    from cloud_conversations.config import Config

LOGIN_HINT = (
    "set OS_CLOUD, or OS_APPLICATION_CREDENTIAL_ID and OS_APPLICATION_CREDENTIAL_SECRET, "
    "or OS_USERNAME, OS_PASSWORD and OS_PROJECT_NAME"
)


def connect(config: Config, user: UserScope | None) -> ChameleonAdapter:
    """The adapter, logged in as user or as ccx.

    Raises CredentialsError if user is in no project.
    """
    from cloud_conversations.adapter.chameleon.adapter import ChameleonAdapter
    from cloud_conversations.adapter.credentials import CredentialsError

    if user is None:
        from cloud_conversations.adapter.chameleon.logins import cli_logins

        return ChameleonAdapter(logins=cli_logins(config), config=config)
    if user.project is None:
        raise CredentialsError("this account cannot reach Chameleon")
    from cloud_conversations.adapter.chameleon.federated import user_logins

    return ChameleonAdapter(logins=user_logins(user, config), config=config)


def public(config: Config) -> ChameleonAdapter:
    """The adapter with no login, for catalog and search calls."""
    from cloud_conversations.adapter.chameleon.adapter import ChameleonAdapter

    return ChameleonAdapter(logins={}, config=config)
