"""Route a chat message, run its agent, and post the reply."""

from __future__ import annotations

import logging
import uuid

import cloud_conversations.db.store as store
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.credentials import user_scope
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.protocol import Capabilities
from cloud_conversations.agent.catalog import Catalog, get_catalog
from cloud_conversations.agent.contract import DeclineStep, ProposedDescription, Route
from cloud_conversations.agent.describe import Round, propose, store_proposal
from cloud_conversations.agent.inventory import read_holdings
from cloud_conversations.agent.route import route
from cloud_conversations.agent.validate import to_requirements
from cloud_conversations.config import Config, ConfigError, testbed_of
from cloud_conversations.db.store import Description, Job, Message, Thread
from cloud_conversations.knowledge.answer import answer, as_turn
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.knowledge.retrieve import open_knowledge, usable_knowledge
from cloud_conversations.llm.client import LLMError, Model, build_client
from cloud_conversations.schema.description import Requirements

log = logging.getLogger(__name__)

NO_ENDPOINT = "I cannot reach a model right now, so I cannot answer that yet."
NO_KNOWLEDGE = (
    "There is no documentation index to search yet. Build one with `ccx index`."
)
KNOWLEDGE_DISABLED = "This deployment does not answer questions from the documentation."
NO_SITES = "No {testbed} site is configured, so there is nothing to list."
NO_LOGINS = "You have no login at any {testbed} site, so there is nothing to list."


def exchange(job: Job, config: Config) -> Message:
    """Carry out one exchange and return the reply."""
    owner = job.owner_id
    thread = store.get_thread(owner, job.thread_id)
    text = str(job.payload["text"])
    settings = config.for_site(None)

    try:
        client = build_client(settings)
    except LLMError as exc:
        return _notice(thread, f"{NO_ENDPOINT} ({exc})")

    history = store.read_messages(owner, thread.thread_id)
    versions = _versions(thread)
    step, rounds = route(
        client, text, _recent(history, config.service.route_history), versions,
        testbed=testbed_of(owner),
    )
    routing = store.create_conversation(
        owner, text, [t.model_dump(mode="json") for t in rounds]
    )
    if rounds and rounds[-1].action == "error":
        return _notice(thread, f"{NO_ENDPOINT} ({rounds[-1].reasons[0]})", routing)
    store.route_job(job.job_id, step.route)

    if step.route == "ask":
        return _answer(client, thread, text, history, config, routing)
    if step.route == "inventory":
        return _holdings(client, thread, text, config, routing)
    if step.route == "build" and thread.description_id is not None:
        return _again(
            thread, thread.description_id, step.version or versions[-1], routing
        )
    return _describe(client, thread, text, step.route, config, routing)


def _holdings(
    client: Model,
    thread: Thread,
    text: str,
    config: Config,
    routing: uuid.UUID,
) -> Message:
    """List what the owner has at the sites they have credentials for."""
    testbed = testbed_of(thread.owner_id)
    first = next(iter(config.sites_at(testbed)), None)
    if first is None:
        return _notice(thread, NO_SITES.format(testbed=config.label(testbed)), routing)
    try:
        adapter = credentials.build_adapter(
            first, config, user_scope(thread.owner_id, thread.project_id)
        )
    except (credentials.CredentialsError, ConfigError, ValueError) as exc:
        return _notice(thread, f"I cannot reach the testbed: {exc}", routing)

    logged_in = set(adapter.sites_with_login())
    capabilities = Capabilities(
        sites=[s for s in adapter.capabilities().sites if s.site in logged_in]
    )
    if not capabilities.sites:
        return _notice(thread, NO_LOGINS.format(testbed=config.label(testbed)), routing)

    read, rounds = read_holdings(client, adapter, capabilities, text, testbed=testbed)
    conversation = store.create_conversation(
        thread.owner_id, text, [r.model_dump(mode="json") for r in rounds]
    )
    if isinstance(read, DeclineStep):
        return _notice(
            thread, read.reason, routing, conversation,
            payload={"suggestions": list(read.suggestions)},
        )
    if read is None:
        return _notice(
            thread,
            f"I could not settle on what to list in {len(rounds)} rounds. Name "
            "what you want: your leases, your instances, or your images.",
            routing,
            conversation,
        )
    return store.append_message(
        thread.thread_id, "assistant", "holdings", read.summary,
        {"tables": read.tables, "routing_conversation_id": str(routing)},
        owner_id=thread.owner_id,
        conversation_id=conversation,
    )


def _answer(
    client: Model,
    thread: Thread,
    text: str,
    history: list[Message],
    config: Config,
    routing: uuid.UUID,
) -> Message:
    """Answer from the documentation."""
    if not config.knowledge.enabled:
        return _notice(thread, KNOWLEDGE_DISABLED, routing)
    try:
        base = open_knowledge(config.knowledge)
    except KnowledgeError as exc:
        return _notice(thread, f"{NO_KNOWLEDGE} ({exc})", routing)
    if base is None:
        return _notice(thread, NO_KNOWLEDGE, routing)

    given = answer(
        client, base, text, _asked(history, config.knowledge.max_history_exchanges),
        testbed=testbed_of(thread.owner_id),
    )
    conversation = store.create_conversation(
        thread.owner_id, text, [as_turn(given)]
    )
    return store.append_message(
        thread.thread_id, "assistant", "answer", given.text,
        {"sources": given.sources, "routing_conversation_id": str(routing)},
        owner_id=thread.owner_id,
        conversation_id=conversation,
    )


def _describe(
    client: Model,
    thread: Thread,
    text: str,
    chosen: Route,
    config: Config,
    routing: uuid.UUID,
) -> Message:
    """Write a description, or a new version of the chat's description."""
    prior = _prior(thread) if chosen == "revise" else None
    try:
        adapter, unseen = credentials.testbed_adapter(
            config,
            testbed_of(thread.owner_id),
            user_scope(thread.owner_id, thread.project_id),
        )
        catalog = get_catalog(adapter, config, None, unseen)
    except (AdapterError, ConfigError, ValueError) as exc:
        return _notice(thread, f"I cannot read the hardware catalog: {exc}", routing)

    vm_site, baremetal_site = _preferred_sites(thread.owner_id, config)
    outcome, rounds = propose(
        client,
        catalog,
        text,
        config.for_site(None),
        prior=prior,
        knowledge=usable_knowledge(config.knowledge),
        vm_site=vm_site,
        baremetal_site=baremetal_site,
        testbed=testbed_of(thread.owner_id),
        adapter=adapter,
        by_user=True,
    )
    transcript = [t.model_dump(mode="json") for t in rounds]

    if not isinstance(outcome, ProposedDescription):
        conversation = store.create_conversation(thread.owner_id, text, transcript)
        return _refused(thread, outcome, len(rounds), routing, conversation)

    if prior is not None and _unchanged(outcome, prior, catalog):
        conversation = store.create_conversation(thread.owner_id, text, transcript)
        return _no_change(thread, prior, rounds, routing, conversation)

    stored = store_proposal(thread.owner_id, text, rounds, outcome, catalog, prior)
    if prior is None:
        store.update_thread(
            thread.owner_id, thread.thread_id, description_id=stored.description_id
        )
    described = store.append_message(
        thread.thread_id, "assistant", "description",
        _summary(stored.requirements),
        {"routing_conversation_id": str(routing)},
        owner_id=thread.owner_id,
        conversation_id=stored.conversation_id,
        description_id=stored.description_id,
        description_version=stored.version,
    )
    notice = config.retirement_notice(stored.requirements)
    if notice is not None:
        _notice(thread, notice, routing, stored.conversation_id)
    return described


def _refused(
    thread: Thread,
    outcome: DeclineStep | None,
    rounds: int,
    routing: uuid.UUID,
    conversation: uuid.UUID,
) -> Message:
    """What to say when no description came back."""
    if isinstance(outcome, DeclineStep):
        return _notice(
            thread, outcome.reason, routing, conversation,
            payload={"suggestions": list(outcome.suggestions)},
        )
    return _notice(
        thread,
        f"I could not settle on hardware for that in {rounds} rounds. "
        "Try saying it another way, or narrowing it.",
        routing,
        conversation,
    )


def _unchanged(
    outcome: ProposedDescription, prior: Description, catalog: Catalog
) -> bool:
    """Whether an answer repeats the prior version."""
    return (
        to_requirements(outcome, catalog).model_dump(exclude={"image"})
        == prior.requirements.model_dump(exclude={"image"})
        and outcome.rubric == prior.rubric
        and list(outcome.unsupported) == prior.unsupported
        and list(outcome.open_questions) == prior.open_questions
    )


def _no_change(
    thread: Thread,
    prior: Description,
    rounds: list[Round],
    routing: uuid.UUID,
    conversation: uuid.UUID,
) -> Message:
    """Say that a revision changed nothing, and what stood in the way."""
    blocked = [reason for round_ in rounds for reason in round_.reasons]
    said = f"I could not change anything, so version {prior.version} still stands."
    if blocked:
        said += f" What I tried was refused: {blocked[-1]}"
    said += " Say it another way, or ask for something else."
    return _notice(thread, said, routing, conversation)


def _notice(
    thread: Thread,
    text: str,
    routing: uuid.UUID | None = None,
    conversation: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
) -> Message:
    said = dict(payload or {})
    if routing is not None:
        said["routing_conversation_id"] = str(routing)
    return store.append_message(
        thread.thread_id, "assistant", "notice", text, said,
        owner_id=thread.owner_id, conversation_id=conversation,
    )


def _again(
    thread: Thread,
    description_id: uuid.UUID,
    version: int,
    routing: uuid.UUID,
) -> Message:
    """Put a version already described back in front of the user to approve."""
    described = store.get_description(thread.owner_id, description_id, version)
    return store.append_message(
        thread.thread_id, "assistant", "description", _summary(described.requirements),
        {"routing_conversation_id": str(routing), "again": True},
        owner_id=thread.owner_id,
        conversation_id=uuid.UUID(described.conversation_id),
        description_id=description_id,
        description_version=version,
    )


def _versions(thread: Thread) -> list[int]:
    """Every version of the chat's description, oldest first."""
    if thread.description_id is None:
        return []
    return [
        d.version
        for d in store.description_versions(thread.owner_id, thread.description_id)
    ]


def _prior(thread: Thread) -> Description | None:
    """The chat's latest description."""
    if thread.description_id is None:
        return None
    return store.latest_description(thread.owner_id, thread.description_id)


def _summary(requirements: Requirements) -> str:
    """One line naming what was described."""
    compute = requirements.value_of("node_type") or requirements.value_of("flavor")
    image = requirements.value_of("image_name") or requirements.value_of("os_release")
    start = (
        requirements.start_at.isoformat() if requirements.start_at else "as soon as built"
    )
    return (
        f"{compute} at {requirements.site} for {requirements.duration_hours}h, "
        f"running {image}, starting {start}."
    )


def _preferred_sites(owner_id: str, config: Config) -> tuple[str | None, str | None]:
    owner = store.get_user(owner_id)
    offered = config.sites_at(owner.testbed)
    vm, baremetal = owner.default_vm_site, owner.default_baremetal_site
    return (vm if vm in offered else None, baremetal if baremetal in offered else None)


def _recent(history: list[Message], limit: int) -> list[tuple[str, str]]:
    """The last few messages, as who said what."""
    return [(m.role, m.text) for m in history[-limit:]] if limit > 0 else []


def _asked(history: list[Message], exchanges: int) -> list[tuple[str, str]]:
    """The documentation questions already answered on this thread, oldest first."""
    pairs: list[tuple[str, str]] = []
    question: str | None = None
    for message in history:
        if message.kind == "user":
            question = message.text
        elif message.kind == "answer" and question is not None:
            pairs.append((question, message.text))
            question = None
    return pairs[-exchanges:] if exchanges > 0 else []
