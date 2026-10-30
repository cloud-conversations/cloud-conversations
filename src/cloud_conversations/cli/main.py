"""ccx - describe, provision, and verify experiment environments on a testbed."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cloud_conversations.db.store as store
from cloud_conversations import audit, crypt, releases
from cloud_conversations.adapter import credentials
from cloud_conversations.adapter.credentials import CredentialsError
from cloud_conversations.adapter.errors import AdapterError
from cloud_conversations.adapter.fake import FAKE_KEYPAIR, default_fake_sites
from cloud_conversations.adapter.protocol import (
    Capabilities,
    Flavor,
    NodeType,
    SiteDescriptor,
    TestbedAdapter,
)
from cloud_conversations.agent import build as agent_build
from cloud_conversations.agent import turns
from cloud_conversations.agent.build import Build
from cloud_conversations.agent.catalog import Catalog, SiteDoc, get_catalog
from cloud_conversations.agent.contract import DeclineStep, ProposedDescription
from cloud_conversations.agent.describe import Round, propose, store_proposal
from cloud_conversations.agent.inventory import Holdings, read_holdings
from cloud_conversations.agent.recover import recover
from cloud_conversations.agent.revision import build_replacement, keypair_for
from cloud_conversations.config import (
    CONFIG_FILE_ENV,
    Config,
    ConfigError,
    Settings,
    load_config,
    owner_id,
    testbed_of,
)
from cloud_conversations.environment.items import SETUP_KEYS
from cloud_conversations.knowledge.answer import answer, as_turn
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.knowledge.retrieve import Retrieved, open_knowledge, usable_knowledge
from cloud_conversations.llm.client import LLMError, build_client
from cloud_conversations.runner import sequential
from cloud_conversations.schema.description import (
    ComputeKey,
    Requirement,
    Requirements,
    reservation_kind_of,
)
from cloud_conversations.schema.knowledge import Document, Knowledge, SourceState
from cloud_conversations.schema.runs import IllegalTransitionError, RunState
from cloud_conversations.validator.rubric import SOFTWARE_KINDS, Check

log = logging.getLogger(__name__)


def _warn(text: str) -> None:
    print(f"WARNING: {text}", file=sys.stderr)


_MUTED_LOGGERS = ("neutronclient",)


def _configure_logging() -> None:
    """Route library logs to stderr if CC_LOG_LEVEL is set."""
    level = os.environ.get("CC_LOG_LEVEL")
    if (level or "").upper() != "DEBUG":
        for name in _MUTED_LOGGERS:
            logging.getLogger(name).setLevel(logging.ERROR)
    if not level:
        return
    logging.basicConfig(
        level=level.upper(),
        format="%(levelname)-5s %(name)s %(message)s",
        stream=sys.stderr,
    )


FIXTURE_NAMES = ("baremetal", "vm", "software")


_SOFTWARE = [
    Requirement(key="system_package", value="jq", source="stated"),
    Requirement(
        key="repository",
        value={
            "url": "https://github.com/octocat/Hello-World",
            "ref": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
            "path": "Hello-World",
        },
        source="stated",
    ),
    Requirement(key="python_version", value="3.12", source="stated"),
    Requirement(key="pip_package", value="numpy", source="stated"),
    Requirement(key="node_version", value="22", source="stated"),
    Requirement(key="npm_package", value="cowsay", source="stated"),
    Requirement(key="docker", value={"gpu": False}, source="stated"),
    Requirement(key="container", value={"image": "nginx:1.27", "ports": [80]}, source="stated"),
]
_SOFTWARE_CHECKS = [
    Check(id="jq", kind="package_installed", target="jq", expected=True, comparator="eq",
          from_requirement="system_package"),
    Check(id="repo", kind="git_head", target="Hello-World", expected="7fd1a60",
          comparator="prefix", from_requirement="repository"),
    Check(id="python", kind="python_version", expected="3.12", comparator="version_eq",
          from_requirement="python_version"),
    Check(id="numpy", kind="pip_package", target="numpy", expected="1",
          comparator="version_gte", from_requirement="pip_package"),
    Check(id="node", kind="node_version", expected="22", comparator="version_eq",
          from_requirement="node_version"),
    Check(id="cowsay", kind="npm_package", target="cowsay", expected="1",
          comparator="version_gte", from_requirement="npm_package"),
    Check(id="docker", kind="docker_running", expected=True, comparator="eq",
          from_requirement="docker"),
    Check(id="nginx", kind="container_running", target="nginx:1.27", expected=True,
          comparator="eq", from_requirement="container"),
    Check(id="port", kind="port_listening", target="80", expected=True, comparator="eq",
          from_requirement="container"),
]


def _fixture(name: str, settings: Settings) -> tuple[Requirements, list[Check]]:
    image = Requirement(key="image_name", value=settings.image_name, source="default")
    release = Requirement(key="os_release", value=settings.os_release, source="default")
    count = Requirement(key="count", value=1, source="default")
    os_check = Check(
        id="os", kind="os_release", expected=settings.os_release,
        comparator="prefix", from_requirement="os_release",
    )
    ssh_check = Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq")

    if name == "software":
        requirements, rubric = _fixture("baremetal", settings)
        requirements.items += [r.model_copy(deep=True) for r in _SOFTWARE]
        return requirements, rubric + [c.model_copy(deep=True) for c in _SOFTWARE_CHECKS]
    if name == "baremetal":
        return (
            Requirements(
                site="FAKE@BM",
                duration_hours=4,
                items=[
                    Requirement(key="node_type", value="compute_skylake", source="stated"),
                    count, image, release,
                ],
            ),
            [
                Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq",
                      from_requirement="node_type"),
                Check(id="cpus", kind="cpu_count", expected=24, comparator="gte",
                      from_requirement="node_type"),
                os_check, ssh_check,
            ],
        )
    return (
        Requirements(
            site="FAKE@KVM",
            duration_hours=2,
            items=[
                Requirement(key="flavor", value="m1.large", source="stated"),
                count, image, release,
            ],
        ),
        [
            Check(id="cpus", kind="cpu_count", expected=4, comparator="gte",
                  from_requirement="flavor"),
            os_check, ssh_check,
        ],
    )


def _config_from(args: argparse.Namespace) -> Config:
    try:
        return load_config(
            ssh_user=getattr(args, "ssh_user", None),
            image_name=getattr(args, "image_name", None),
            os_release=getattr(args, "os_release", None),
        )
    except ConfigError as exc:
        sys.exit(f"ERROR: {exc}")


def _knowledge(config: Config) -> Knowledge:
    if not config.knowledge.enabled:
        sys.exit("ERROR: the knowledge base is disabled; set [knowledge] enabled = true")
    return config.knowledge


def _owner(args: argparse.Namespace) -> str:
    """ccx's owner, made a user on first use."""
    owner = _owner_id(args)
    store.ensure_user(owner)
    return owner


def _owner_id(args: argparse.Namespace) -> str:
    try:
        return owner_id(_config_from(args))
    except ConfigError as exc:
        sys.exit(f"ERROR: {exc}")


def _local_testbed(args: argparse.Namespace) -> str:
    testbed = _config_from(args).service.local_users_testbed
    if testbed is None:
        sys.exit("ERROR: set local_users_testbed under [service] for local test users")
    return testbed


def _build_adapter(site: str, config: Config) -> TestbedAdapter:
    """Build the adapter that provides this site, or exit saying why not."""
    try:
        return credentials.build_adapter(site, config)
    except (CredentialsError, ValueError) as exc:
        sys.exit(f"ERROR: {exc}")


def _print_events(owner: str, run_id: uuid.UUID) -> None:
    events = store.read_events(owner, run_id)
    print(f"\n{'seq':>4}  {'at':23}  {'actor':9}  {'kind':30}  payload")
    print("-" * 110)
    for e in events:
        at = e.at.strftime("%Y-%m-%d %H:%M:%S.%f")[:23]
        payload = " ".join(f"{k}={v!r}" for k, v in e.payload.items() if k != "v")
        if len(payload) > 140:
            payload = payload[:140] + "…"
        print(f"{e.seq:>4}  {at:23}  {e.actor:9}  {e.kind:30}  {payload}")


def _print_summary(owner: str, run_id: uuid.UUID) -> store.Run:
    run = store.get_run(owner, run_id)
    print(f"\nrun     {run.run_id}")
    print(f"desc    {run.description_id} v{run.description_version}")
    print(f"state   {run.state.value}  (resumes {run.resume_count})")
    if run.connection:
        c = run.connection
        until = f"  until {c.expires_at}" if c.expires_at else ""
        print(f"host    {c.user}@{c.host}:{c.port}{until}")
    if run.results:
        for chk in run.results:
            mark = "PASS" if chk.passed else "FAIL"
            print(f"check   [{mark}] {chk.check_id}: expected {chk.expected!r}, got {chk.actual!r}")
    if run.failure:
        f = run.failure
        where = f" at {f.step_id}/{f.action}" if f.step_id else ""
        print(f"failure [{f.kind}]{where} {f.message}")
    live = store.live_resources(run_id)
    if live:
        print(f"\nLive resources ({len(live)}):")
        for r in live:
            lifetime = f"expires={r.expires_at}" if r.expires_at else "on demand"
            print(f"  {r.kind:14}  {r.provider_id}  site={r.site}  {lifetime}")
    return run


def cmd_describe_fixture(args: argparse.Namespace) -> int:
    """Write a description to the database and print its id."""
    owner = _owner(args)
    config = _config_from(args)
    requirements, rubric = _fixture(args.name, config.for_site(args.site))

    if args.site:
        requirements, rubric = _for_configured_site(args, requirements, rubric)
    else:
        requirements, rubric = _patch_fixture(args, requirements, rubric)
    if args.start_at is not None:
        requirements.start_at = _start_at(args.start_at)

    cap = config.for_site(
        requirements.site, reservation_kind_of(requirements.items)
    ).max_lease_hours
    if requirements.duration_hours > cap:
        sys.exit(
            f"ERROR: --hours {requirements.duration_hours} exceeds max_lease_hours={cap} "
            f"at {requirements.site}"
        )

    desc_id, version = store.create_description(
        owner_id=owner,
        conversation_id=args.conversation_id or f"fixture:{args.name}",
        requirements=requirements,
        rubric=rubric,
        description_id=uuid.UUID(args.description_id) if args.description_id else None,
    )
    print(f"description_id : {desc_id}")
    print(f"version        : {version}  (ccx provision uses the latest unless --version)")
    print(f"site           : {requirements.site}")
    compute = requirements.value_of("node_type") or requirements.value_of("flavor")
    print(f"compute        : {compute}")
    print(f"image_name     : {requirements.value_of('image_name')}")
    print(f"duration_hours : {requirements.duration_hours}")
    for item in requirements.items:
        if item.key in SETUP_KEYS:
            print(f"software       : {item.key} {item.value}")
    if requirements.start_at is not None:
        print(f"start_at       : {requirements.start_at.isoformat()}")
    print("\nrubric         : measured on the built host; every check must pass")
    for chk in rubric:
        source = f"  from {chk.from_requirement}" if chk.from_requirement else ""
        print(f"  {chk.id:<6} {chk.fact():<14} {chk.comparator:<6} {chk.expected!r}{source}")
    return 0


def _start_at(text: str) -> datetime:
    """Parse an ISO 8601 lease start. A time with no offset is read as UTC."""
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        sys.exit(f"ERROR: --start-at {text!r} is not an ISO 8601 time, e.g. 2026-09-08T14:00Z")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        sys.exit(f"ERROR: --start-at {text!r} is in the past")
    return parsed


def cmd_describe(args: argparse.Namespace) -> int:
    """Turn a prompt into a description."""
    owner = _owner(args)
    config = _config_from(args)
    settings = config.for_site(args.site)

    client = build_client(settings, args.model, args.temperature)
    try:
        adapter, unseen = credentials.testbed_adapter(config, testbed_of(owner))
        catalog = get_catalog(adapter, config, args.site, unseen)
    except (AdapterError, ValueError) as exc:
        sys.exit(f"ERROR: {exc}")
    prior = _prior_description(owner, args)

    outcome, rounds = propose(
        client, catalog, args.prompt, settings,
        prior=prior, knowledge=usable_knowledge(config.knowledge, _warn),
        testbed=testbed_of(owner), adapter=adapter, by_user=True,
    )
    if args.verbose:
        for entry in rounds:
            print(_progress(entry), file=sys.stderr)

    if not isinstance(outcome, ProposedDescription):
        conversation_id = store.create_conversation(
            owner, args.prompt, [t.model_dump(mode="json") for t in rounds]
        )
        if isinstance(outcome, DeclineStep):
            print(f"Declined: {outcome.reason}")
            for suggestion in outcome.suggestions:
                print(f"  - {suggestion}")
        else:
            print(f"No description after {len(rounds)} rounds.")
            for reason in rounds[-1].reasons if rounds else []:
                print(f"  {reason}")
        print(f"\nconversation_id: {conversation_id}  (ccx show-conversation)")
        return 1

    stored = store_proposal(owner, args.prompt, rounds, outcome, catalog, prior)
    _print_description(
        outcome, catalog, stored.description_id, stored.version, stored.conversation_id,
        len(rounds),
    )
    notice = config.retirement_notice(stored.requirements)
    if notice is not None:
        print(f"\n{notice}", file=sys.stderr)
    return 0


def _prior_description(owner: str, args: argparse.Namespace) -> store.Description | None:
    """The version being revised, or None for a new description."""
    if not args.description_id:
        return None
    lineage = uuid.UUID(args.description_id)
    if args.version is not None:
        return store.get_description(owner, lineage, args.version)
    return store.latest_description(owner, lineage)


def _progress(entry: Round) -> str:
    if entry.action == "search":
        outcome = f"{entry.found} free" if not entry.reasons else "refused"
        return f"round {entry.round}  search    {outcome}"
    if entry.action == "search_knowledge_base":
        return f"round {entry.round}  docs      {'refused' if entry.reasons else 'read'}"
    if entry.action == "read_repository":
        return f"round {entry.round}  repo      {'refused' if entry.reasons else 'read'}"
    if entry.action == "answer":
        return f"round {entry.round}  answer    {'accepted' if not entry.reasons else 'rejected'}"
    if entry.action == "decline":
        return f"round {entry.round}  decline"
    if entry.action == "error":
        return f"round {entry.round}  error     {'; '.join(entry.reasons)}"
    return f"round {entry.round}  unparsed"


def _print_description(
    proposal: ProposedDescription,
    catalog: Catalog,
    desc_id: uuid.UUID,
    version: int,
    conversation_id: uuid.UUID,
    rounds: int,
) -> None:
    print(f"description_id : {desc_id}")
    print(f"version        : {version}")
    print(f"conversation_id: {conversation_id}  ({rounds} rounds)")
    print(f"site           : {proposal.site}")
    print(f"start_at       : {_start_text(proposal)}")
    print(f"duration_hours : {proposal.duration_hours}")

    doc = catalog.site(proposal.site)
    named_image = proposal.value_of("image_name") is not None
    node_type = proposal.value_of("node_type")
    arch = doc.arch_of("node_type", str(node_type)) if doc and node_type else None
    print("\nrequirements")
    for item in proposal.items:
        resolved = _resolved(item, doc, named_image, arch) if doc else ""
        print(f"  {item.key:<12} {item.value!r:<24} {item.source:<8} {resolved}".rstrip())

    print("\nrubric")
    for check in proposal.rubric:
        source = f"  from {check.from_requirement}" if check.from_requirement else ""
        print(f"  {check.id:<6} {check.kind:<14} {check.comparator:<6} "
              f"{check.expected!r}{source}")

    for label, entries in (("unsupported", proposal.unsupported),
                           ("open questions", proposal.open_questions)):
        if entries:
            print(f"\n{label}")
            for entry in entries:
                print(f"  - {entry}")

    print(f"\nccx provision --description-id {desc_id} --version {version} --teardown")


def _start_text(proposal: ProposedDescription) -> str:
    if proposal.start_at is None:
        return "when provisioned"
    return proposal.start_at.isoformat()


def _resolved(
    item: Requirement, doc: SiteDoc, named_image: bool, cpu_arch: str | None
) -> str:
    """What a requirement resolves to at this site."""
    if item.key == "node_type" or item.key == "flavor":
        spec = doc.compute(item.key, str(item.value))
        return _spec_text(spec) if spec is not None else ""
    if item.key == "image_name":
        image = doc.image_for(str(item.value), None, cpu_arch)
        return image.image_id if image is not None else ""
    if item.key == "os_release" and not named_image:
        image = doc.image_for(None, str(item.value), cpu_arch)
        name = doc.descriptor.images.name_for(str(item.value))
        return f"{name}  {image.image_id}" if image is not None else name
    return ""


def _spec_text(spec: NodeType | Flavor) -> str:
    if isinstance(spec, NodeType):
        gpu = f", gpu {spec.gpu_model}" if spec.gpu_model else ""
        return (f"{spec.cpu_arch}, {spec.cpu_count} cpu, {spec.mem_gb} GB ram, "
                f"{spec.disk_gb} GB disk{gpu}")
    return (f"{spec.vcpus} vcpu, {spec.mem_gb} GB ram, {spec.disk_gb} GB disk"
            f"{', gpu' if spec.gpu else ''}")


def cmd_show_conversation(args: argparse.Namespace) -> int:
    """Print what the model said each round, and what it was told back."""
    conversation = store.get_conversation(_owner_id(args), uuid.UUID(args.conversation_id))
    print(f"prompt         : {conversation.prompt}")
    print(f"created_at     : {conversation.created_at:%Y-%m-%d %H:%M:%S}")
    if conversation.description_id is None:
        print("description    : none")
    else:
        print(f"description    : {conversation.description_id} "
              f"v{conversation.description_version}")

    for entry in conversation.transcript:
        print(f"\n--- round {entry['round']}  {entry['action']} "
              f"({entry.get('finish_reason', '')}) ---")
        print(entry["text"])
        for reason in entry.get("reasons") or []:
            print(f"  rejected: {reason}")
        for url in entry.get("sources") or []:
            print(f"  source: {url}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Answer a question from the knowledge base."""
    owner = _owner(args)
    config = _config_from(args)
    base = open_knowledge(_knowledge(config))
    if base is None:
        sys.exit("ERROR: no knowledge base has been built; run ccx index")
    client = build_client(config.for_site(None), args.model, args.temperature)

    history: list[tuple[str, str]] = []
    for question in (
        [args.question] if args.question else _typed_questions(config.label(testbed_of(owner)))
    ):
        given = answer(client, base, question, history, testbed=testbed_of(owner))
        if args.verbose:
            print(_retrieval(given.retrieved), file=sys.stderr)
        print(f"\n{given.text}")
        if given.sources:
            print("\nsources")
            for url in given.sources:
                print(f"  {url}")
        conversation_id = store.create_conversation(
            owner, question, [as_turn(given)]
        )
        print(f"\nconversation_id: {conversation_id}")
        history.append((question, given.text))
    return 0


def cmd_inventory(args: argparse.Namespace) -> int:
    """List what the caller has at the testbed."""
    owner = _owner(args)
    config = _config_from(args)
    site = args.site or next(iter(config.sites_at(testbed_of(owner))), "")
    adapter = _build_adapter(site, config)
    logged_in = {args.site} if args.site else set(adapter.sites_with_login())
    capabilities = Capabilities(
        sites=[s for s in adapter.capabilities().sites if s.site in logged_in]
    )
    if not capabilities.sites:
        sys.exit("ERROR: no site credentials; run ccx check --site <site>")

    client = build_client(config.for_site(None), args.model, args.temperature)
    read, rounds = read_holdings(
        client, adapter, capabilities, args.request, testbed=testbed_of(owner)
    )
    conversation_id = store.create_conversation(
        owner, args.request, [r.model_dump(mode="json") for r in rounds]
    )
    if not isinstance(read, Holdings):
        if isinstance(read, DeclineStep):
            print(f"Declined: {read.reason}")
            for suggestion in read.suggestions:
                print(f"  - {suggestion}")
        else:
            print(f"Nothing listed after {len(rounds)} rounds.")
            for reason in rounds[-1].reasons if rounds else []:
                print(f"  {reason}")
        print(f"\nconversation_id: {conversation_id}")
        return 1

    for table in read.tables:
        print(f"\n{table}")
    print(f"\n{read.summary}")
    print(f"\nconversation_id: {conversation_id}")
    return 0


def _typed_questions(label: str) -> Iterator[str]:
    """Questions typed at the prompt, until the input ends."""
    print(f"Ask about {label}. Ctrl-D to leave.")
    while True:
        try:
            question = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if question:
            yield question


def _retrieval(retrieved: Retrieved) -> str:
    """What the reranker scored, and which passages the answer was built from."""
    scores: dict[str, list[float]] = {}
    for candidate in retrieved.candidates:
        scores.setdefault(candidate.passage.source, []).append(candidate.score)

    kept = sum(1 for c in retrieved.candidates if c.selected)
    lines = [f"{len(retrieved.candidates)} candidate(s) reranked, {kept} kept"]
    for source, found in sorted(scores.items()):
        lines.append(
            f"  {source:<14} n={len(found):<3} top={max(found):+.3f} "
            f"avg={sum(found) / len(found):+.3f}"
        )
    for candidate in retrieved.candidates:
        mark = "kept" if candidate.selected else "    "
        lines.append(
            f"  {mark} {candidate.score:+.3f}  {candidate.passage.source:<14} "
            f"{candidate.passage.url}"
        )
    return "\n".join(lines)


def cmd_index(args: argparse.Namespace) -> int:
    """Fetch the configured sources and rebuild the knowledge index."""
    from cloud_conversations.knowledge import chunks, index
    from cloud_conversations.knowledge.embed import BgeEmbedder

    config = _config_from(args)
    knowledge = _knowledge(config)
    if not knowledge.sources:
        sys.exit("ERROR: the config file declares no [knowledge.sources]")
    refresh = _refreshed(args, knowledge)

    print(f"fetching {len(knowledge.sources)} source(s)")
    fetched: list[Document] = []
    for name, source in knowledge.sources.items():
        collected = index.collect(name, source, knowledge, refresh=name in refresh)
        print(f"  {name:<14} {len(collected.documents):>5} documents "
              f"({collected.origin})")
        if collected.error:
            print(f"  {name:<14} fetch failed: {collected.error}", file=sys.stderr)
        fetched.extend(collected.documents)

    if not fetched:
        sys.exit("ERROR: no documents were read; the live index is unchanged")

    documents = chunks.split(fetched, knowledge)
    passages = sum(len(d.parents) for d in documents)
    children = sum(len(p.chunks) for d in documents for p in d.parents)
    print(f"\nchunking  {len(documents)} documents into {passages} passages "
          f"and {children} chunks")
    print(f"embedding {children} chunks with {knowledge.embedding_model} "
          f"on {knowledge.embedding_device}")
    index.embed(documents, BgeEmbedder(knowledge), knowledge)

    live = store.replace_knowledge(documents, knowledge.embedding_model)
    print(f"\nindex live: {live.documents} documents, {live.parents} passages, "
          f"{live.chunks} chunks of {live.dimensions} dimensions")
    return 0


def _refreshed(args: argparse.Namespace, knowledge: Knowledge) -> set[str]:
    """The sources to fetch again."""
    if args.use_cache:
        return set()
    if not args.refresh:
        return set(knowledge.sources)
    unknown = sorted(set(args.refresh) - set(knowledge.sources))
    if unknown:
        sys.exit(
            f"ERROR: unknown source(s) {', '.join(unknown)}; the config file "
            f"declares {', '.join(knowledge.sources)}"
        )
    return set(args.refresh)


def cmd_knowledge(args: argparse.Namespace) -> int:
    """Print what is in the live knowledge index."""
    config = _config_from(args)
    knowledge = _knowledge(config)
    live = store.latest_index()
    if live is None:
        print("no knowledge base has been built; run ccx index")
        return 1

    print(f"built_at  : {live.built_at:%Y-%m-%d %H:%M:%S}")
    print(f"model     : {live.embedding_model}  ({live.dimensions} dimensions)")
    print(f"documents : {live.documents}")
    print(f"passages  : {live.parents}")
    print(f"chunks    : {live.chunks}")

    print("\nsources")
    indexed = {s.source: s for s in store.knowledge_sources()}
    for name, source in knowledge.sources.items():
        tier = "primary" if source.primary else "supplementary"
        print(_source_line(name, indexed.pop(name, None), tier))
    for name, state in indexed.items():
        print(_source_line(name, state, "unconfigured"))
    return 0


def _source_line(name: str, state: SourceState | None, tier: str) -> str:
    if state is None:
        return f"  {name:<14} {'not in the index':<30}{tier}"
    return (
        f"  {name:<14} {state.documents:>4} documents {state.chunks:>6} chunks  "
        f"{tier:<13} fetched {state.fetched_at:%Y-%m-%d %H:%M}"
    )


def _for_configured_site(
    args: argparse.Namespace, fixture: Requirements, fixture_rubric: list[Check]
) -> tuple[Requirements, list[Check]]:
    """Build a description for a configured site, using the fixture only as defaults."""
    site: str = args.site
    testbed = testbed_of(_owner_id(args))
    caps = credentials.public_adapter(_config_from(args), testbed).capabilities()
    descriptor = caps.site(site)
    if descriptor is None:
        known = ", ".join(s.site for s in caps.sites)
        sys.exit(f"ERROR: unknown site {site!r}; known sites: {known}")

    key = _compute_key(args, descriptor, fallback="flavor" if fixture.value_of("flavor")
                       else "node_type")
    compute = Requirement(
        key=key, value=getattr(args, key) or fixture.value_of(key), source="stated"
    )
    if not compute.value:
        sys.exit(f"ERROR: {site} needs {_FLAGS[key]}")

    image_name = args.image_name or fixture.value_of("image_name")
    os_release = args.os_release or fixture.value_of("os_release")
    requirements = Requirements(
        site=site,
        duration_hours=fixture.duration_hours if args.hours is None else args.hours,
        items=[
            compute,
            Requirement(key="count", value=1, source="default"),
            Requirement(key="image_name", value=image_name, source="stated"),
            Requirement(key="os_release", value=os_release, source="stated"),
            *(i for i in fixture.items if i.key in SETUP_KEYS),
        ],
    )
    rubric = [
        *(c for c in fixture_rubric if c.kind in SOFTWARE_KINDS),
        Check(id="arch", kind="cpu_arch", expected="x86_64", comparator="eq"),
        Check(id="os", kind="os_release", expected=os_release, comparator="prefix",
              from_requirement="os_release"),
        Check(id="dns", kind="dns_resolves", expected=True, comparator="eq"),
        Check(id="ssh", kind="ssh_reachable", expected=True, comparator="eq"),
    ]
    if args.min_cpus is not None:
        rubric.append(
            Check(id="cpus", kind="cpu_count", expected=args.min_cpus, comparator="gte")
        )
    return requirements, rubric


_FLAGS: dict[ComputeKey, str] = {"node_type": "--node-type", "flavor": "--flavor"}

_KEYPAIR_HELP = "A keypair at the site, or an imported key's; required at a testbed site"


def _compute_key(
    args: argparse.Namespace, descriptor: SiteDescriptor, fallback: ComputeKey
) -> ComputeKey:
    if args.node_type and args.flavor:
        sys.exit("ERROR: give one of --node-type or --flavor")
    key: ComputeKey = "flavor" if args.flavor else "node_type" if args.node_type else fallback
    offered = descriptor.reserves_by()
    if key not in offered:
        sys.exit(
            f"ERROR: {descriptor.site} takes "
            f"{' or '.join(_FLAGS[k] for k in offered)}, not {_FLAGS[key]}"
        )
    return key


def _patch_fixture(
    args: argparse.Namespace, fixture: Requirements, rubric: list[Check]
) -> tuple[Requirements, list[Check]]:
    """Apply the non-site flags to a fixture, keeping it on the fixture's site."""
    bare_metal = fixture.value_of("node_type") is not None
    if args.node_type and not bare_metal:
        sys.exit(f"ERROR: fixture site {fixture.site} is KVM; use --flavor, not --node-type")
    if args.flavor and bare_metal:
        sys.exit(
            f"ERROR: fixture site {fixture.site} is bare metal; use --node-type, not --flavor"
        )

    patched = fixture.model_copy(deep=True)
    if args.hours is not None:
        patched.duration_hours = args.hours
    for item in patched.items:
        if item.key == "node_type" and args.node_type:
            item.value = args.node_type
        elif item.key == "flavor" and args.flavor:
            item.value = args.flavor

    checks = [c.model_copy(deep=True) for c in rubric]
    if args.min_cpus is not None:
        cpus = next((c for c in checks if c.kind == "cpu_count"), None)
        if cpus is not None:
            cpus.expected = args.min_cpus
        else:
            checks.append(
                Check(id="cpus", kind="cpu_count", expected=args.min_cpus, comparator="gte")
            )

    return Requirements.model_validate(patched.model_dump()), checks


def _provision_and_report(
    owner: str,
    description_id: uuid.UUID,
    version: int,
    site: str,
    adapter: TestbedAdapter,
    settings: Settings,
    project_id: str | None,
    keypair: str,
    args: argparse.Namespace,
) -> int:
    request_id = audit.approved(owner, "cli", description_id=description_id, version=version)
    with audit.acting(owner, request_id):
        run_id = sequential.provision(
            owner_id=owner,
            description_id=description_id,
            version=version,
            project_id=project_id,
            keypair=keypair,
            adapter=adapter,
            site=site,
            settings=settings,
        )
    return _report(owner, run_id, adapter, args)


def _report(
    owner: str, run_id: uuid.UUID, adapter: TestbedAdapter, args: argparse.Namespace
) -> int:
    """Print the run, release it if asked, and return the exit status."""
    _print_events(owner, run_id)
    run = _print_summary(owner, run_id)

    if args.teardown:
        print("\nTearing down …")
        if not _teardown(owner, run_id, adapter, args.yes, f"ccx {args.command} --teardown"):
            return 1

    return 0 if run.state == RunState.passed else 1


def _teardown(
    owner: str, run_id: uuid.UUID, adapter: TestbedAdapter, yes: bool, reason: str
) -> bool:
    """Release the run's resources and print what is still live. True if nothing is."""
    resources = releases.held(run_id)
    if not resources:
        print("No live resources for this run.")
        return True
    question = f"Release {releases.named(resources)}?"
    request_id = releases.propose(
        owner, "user", "release", reason, resources, via="cli", run_id=run_id,
        shown=None if yes else question,
    )
    if not (yes or _asked(question)):
        releases.decline(owner, request_id, "cli")
        print("Nothing released.")
        return False
    releases.confirm(owner, request_id, "cli")
    outcomes = releases.carry_out(owner, request_id, adapter)
    remaining = store.live_resources(run_id)
    if not remaining:
        print("All resources released.")
        return True
    print(f"WARNING: {len(remaining)} resource(s) still live:", file=sys.stderr)
    errors = {(o.kind, o.provider_id): o.error for o in outcomes if not o.success}
    for r in remaining:
        why = errors.get((r.kind, r.provider_id), "not attempted")
        print(f"  {r.kind} {r.provider_id} at {r.site}: {why}", file=sys.stderr)
    print(f"  retry: ccx teardown --run-id {run_id}", file=sys.stderr)
    return False


def _asked(question: str) -> bool:
    """Ask a yes-or-no question. True only for yes."""
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_build(args: argparse.Namespace) -> int:
    """Provision a description, and let the agent recover a failed attempt."""
    owner = _owner(args)
    config = _config_from(args)

    if args.run_id:
        run_id = uuid.UUID(args.run_id)
        run = store.get_run(owner, run_id)
        site = run.site or store.get_description(
            owner, run.description_id, run.description_version
        ).requirements.site
        if not site:
            sys.exit("ERROR: that run names no site")
        adapter = _build_adapter(site, config)
        request_id = audit.resumed(owner, run_id)
        print(f"Continuing the build at run {run_id} on {site} …")
    else:
        if not args.description_id:
            sys.exit("ERROR: give --description-id to start a build, or --run-id to continue one")
        description_id = uuid.UUID(args.description_id)
        version = args.version
        if version is None:
            version = store.latest_description(owner, description_id).version
        requirements = store.get_description(owner, description_id, version).requirements
        site = args.site or requirements.site
        if not site:
            sys.exit("ERROR: no --site given and the description names none")
        adapter = _build_adapter(site, config)
        keypair = _keypair(args, site)
        print(f"Building {description_id} v{version} at {site} …")
        request_id = audit.approved(
            owner, "cli", description_id=description_id, version=version
        )
        with audit.acting(owner, request_id):
            run_id = sequential.provision(
                owner_id=owner,
                description_id=description_id,
                version=version,
                project_id=args.project_id,
                keypair=keypair,
                adapter=adapter,
                site=site,
                settings=config.for_site(site, reservation_kind_of(requirements.items)),
            )

    run_id = _settle(owner, run_id, request_id, adapter, config, site, args)
    return _report(owner, run_id, adapter, args)


def _settle(
    owner: str,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
    adapter: TestbedAdapter,
    config: Config,
    site: str,
    args: argparse.Namespace,
) -> uuid.UUID:
    """Recover a failed run from the CLI."""
    while store.get_run(owner, run_id).state != RunState.passed:
        earlier = store.pending_release(owner, run_id)
        with audit.acting(owner, request_id):
            run_id = _recover(
                owner, run_id, adapter, config, site, args.model, args.temperature,
                args.verbose
            )
        proposal = store.pending_release(owner, run_id)
        if proposal is None or (earlier is not None and proposal.request_id == earlier.request_id):
            return run_id
        version = proposal.version
        print(f"\n{proposal.shown}")
        run = store.get_run(owner, run_id)
        if not _asked("Release them?"):
            releases.decline(owner, proposal.request_id, "cli")
            print("Kept them.")
            if version is not None:
                print(
                    f"Build version {version} with: ccx build "
                    f"--description-id {run.description_id} --version {version}"
                )
            return run_id
        releases.confirm(owner, proposal.request_id, "cli")
        releases.carry_out(owner, proposal.request_id, adapter)
        if version is None or store.live_resources(run_id):
            return run_id

        site = store.get_description(owner, run.description_id, version).requirements.site or site
        request_id = proposal.request_id
        with audit.acting(owner, request_id):
            run_id = build_replacement(
                owner, run, version, str(proposal.detail["change"]),
                keypair_for(owner, run, site) or _keypair(args, site), adapter, config,
            )
    return run_id


def _recover(
    owner: str,
    run_id: uuid.UUID,
    adapter: TestbedAdapter,
    config: Config,
    site: str,
    model: str | None,
    temperature: float | None,
    verbose: bool,
) -> uuid.UUID:
    """Drive the experiment agent to recover a failed run."""
    run = store.get_run(owner, run_id)
    requirements = store.get_description(
        owner, run.description_id, run.description_version
    ).requirements
    settings = config.for_site(site, reservation_kind_of(requirements.items))
    try:
        client = build_client(settings, model, temperature)
    except LLMError as exc:
        print(f"\nNot recovered: {exc}", file=sys.stderr)
        return run_id

    def report(outcome: Build) -> None:
        if verbose:
            for entry in outcome.rounds:
                print(_build_progress(entry), file=sys.stderr)
                for reason in entry.reasons:
                    print(f"          {reason}", file=sys.stderr)
        if outcome.revision is not None:
            print(f"Revising the description: {outcome.revision.change}")

    print("\nProvisioning failed; asking the experiment agent what to do …")
    return recover(
        client, owner, run_id, adapter, config, settings, usable_knowledge(config.knowledge, _warn),
        report=report,
    )


def _build_progress(entry: turns.Round) -> str:
    if entry.action in ("unparsed", "error"):
        return f"round {entry.round}  {entry.action}"
    return f"round {entry.round}  {entry.action:<8} {'refused' if entry.reasons else 'taken'}"


def cmd_provision(args: argparse.Namespace) -> int:
    """Provision against an existing description."""
    owner = _owner(args)
    description_id = uuid.UUID(args.description_id)
    version = args.version
    if version is None:
        version = store.latest_description(owner, description_id).version

    requirements = store.get_description(owner, description_id, version).requirements
    site = args.site
    source = "--site"
    if not site:
        site = requirements.site
        source = "description"
    if not site:
        sys.exit("ERROR: no --site given and the description names none")

    config = _config_from(args)
    adapter = _build_adapter(site, config)
    keypair = _keypair(args, site)
    print(f"Provisioning {description_id} v{version} at {site} (from {source}) …")
    return _provision_and_report(
        owner, description_id, version, site, adapter,
        config.for_site(site, reservation_kind_of(requirements.items)),
        args.project_id, keypair, args,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """Describe a fixture and provision it."""
    owner = _owner(args)
    config = _config_from(args)
    requirements, rubric = _fixture(args.fixture, config.for_site(args.site))
    site = args.site or requirements.site
    if not site:
        sys.exit(f"ERROR: fixture {args.fixture!r} names no site")

    adapter = _build_adapter(site, config)
    keypair = _keypair(args, site)
    if args.image_name:
        requirements = requirements.model_copy(deep=True)
        for item in requirements.items:
            if item.key == "image_name":
                item.value = args.image_name
    if site != requirements.site:
        requirements = requirements.model_copy(update={"site": site})
    desc_id, version = store.create_description(
        owner_id=owner,
        conversation_id=f"cli:{args.fixture}",
        requirements=requirements,
        rubric=rubric,
    )
    print(f"description {desc_id} v{version} -> {site}")
    return _provision_and_report(
        owner, desc_id, version, site, adapter,
        config.for_site(site, reservation_kind_of(requirements.items)),
        args.project_id, keypair, args,
    )


def _keypair(args: argparse.Namespace, site: str) -> str:
    if args.keypair:
        return str(args.keypair)
    if site in default_fake_sites():
        return FAKE_KEYPAIR
    sys.exit(f"ERROR: {site} needs --keypair; name a keypair at the site or an imported key's")


_BUILD_EVENTS = frozenset({
    "step_failed", "step_retried", "run_terminal", "validation_completed",
    "description_revision_requested", "connection_ready",
})


def cmd_show_build(args: argparse.Namespace) -> int:
    """Print a build end to end: both agents, every run, in order."""
    owner = _owner_id(args)
    run_id = uuid.UUID(args.run_id) if args.run_id else _latest_run(owner, args)
    runs = agent_build.chain(owner, run_id)
    first = store.get_run(owner, runs[0])
    talk = {
        c.description_version: c
        for c in store.conversations_for_description(owner, first.description_id)
    }

    print(f"build of {len(runs)} run(s) against description {first.description_id}")
    _print_talk(
        f"version {first.description_version}, from the prompt",
        talk.get(first.description_version),
    )
    for number, rid in enumerate(runs, start=1):
        revision = _print_build_run(owner, rid, number)
        if revision is not None:
            _print_talk(
                f"version {revision['version']}, asked for: {revision['change']}",
                talk.get(revision["version"]),
            )
    return 0


def _latest_run(owner: str, args: argparse.Namespace) -> uuid.UUID:
    """The most recent run against a description."""
    if not args.description_id:
        sys.exit("ERROR: give --run-id or --description-id")
    runs = store.runs_for_description(owner, uuid.UUID(args.description_id))
    if not runs:
        sys.exit(f"ERROR: no run against description {args.description_id}")
    if len(runs) > 1:
        print(f"{len(runs)} runs against this description; showing the most recent")
    return runs[-1].run_id


def _print_talk(label: str, conversation: store.Conversation | None) -> None:
    """One exchange with the experiment description agent."""
    print(f"\n{'=' * 72}\ndescription  {label}\n{'=' * 72}")
    if conversation is None:
        print("  written without a model")
        return
    print(f"asked: {conversation.prompt}")
    for entry in conversation.transcript:
        print(f"\n  round {entry['round']}  {entry['action']}")
        for line in entry["text"].splitlines():
            print(f"    {line}")
        for reason in entry.get("reasons") or []:
            print(f"    refused: {reason}")


def _print_build_run(owner: str, run_id: uuid.UUID, number: int) -> dict | None:
    """One run of a build. Returns the revision it asked for, if any."""
    run = store.get_run(owner, run_id)
    print(f"\n{'=' * 72}\nrun {number}  {run_id}  [{run.state.value}]\n{'=' * 72}")

    revision = None
    for event in store.read_events(owner, run_id):
        payload = event.payload
        if event.kind == "model_turn":
            print(f"\n  experiment agent, round {payload['round']}: {payload['action']}")
            for line in payload["text"].splitlines():
                print(f"    {line}")
            for reason in payload.get("reasons") or []:
                print(f"    refused: {reason}")
        elif event.kind == "tool_called":
            print(f"\n  it read {payload['read']}:")
            for line in payload["result"].splitlines():
                print(f"    {line}")
        elif event.kind in _BUILD_EVENTS:
            fields = " ".join(
                f"{k}={v!r}" for k, v in payload.items() if k not in ("v", "change")
            )
            print(f"  {event.kind:31} {fields}")
            if event.kind == "description_revision_requested":
                revision = payload
    if run.failure:
        print(f"\n  failure [{run.failure.kind}] {run.failure.message}")
    return revision


def cmd_show_run(args: argparse.Namespace) -> int:
    owner = _owner_id(args)
    run_id = uuid.UUID(args.run_id)

    if args.json:
        run = store.get_run(owner, run_id)
        out: dict[str, Any] = {
            "run_id": str(run.run_id),
            "description_id": str(run.description_id),
            "description_version": run.description_version,
            "state": run.state.value,
            "resume_count": run.resume_count,
            "plan": run.plan.model_dump() if run.plan else None,
            "results": [r.model_dump() for r in run.results] if run.results else None,
            "connection": run.connection.model_dump() if run.connection else None,
            "failure": run.failure.model_dump() if run.failure else None,
            "created_at": run.created_at.isoformat(),
            "heartbeat_at": run.heartbeat_at.isoformat(),
            "live_resources": [
                {"kind": r.kind, "provider_id": r.provider_id, "site": r.site}
                for r in store.live_resources(run_id)
            ],
        }
        if args.events:
            out["events"] = [
                {
                    "seq": e.seq,
                    "at": e.at.isoformat(),
                    "actor": e.actor,
                    "kind": e.kind,
                    "payload": e.payload,
                }
                for e in store.read_events(owner, run_id)
            ]
        print(json.dumps(out, indent=2))
        return 0

    if args.events:
        _print_events(owner, run_id)
    _print_summary(owner, run_id)
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    owner = _owner_id(args)
    run_id = uuid.UUID(args.run_id)
    run = store.get_run(owner, run_id)

    live = store.live_resources(run_id)
    if not live:
        print("No live resources for this run.")
        return 0
    site = run.site or live[0].site

    adapter = _build_adapter(site, _config_from(args))
    return 0 if _teardown(owner, run_id, adapter, args.yes, "ccx teardown") else 1


def cmd_resume(args: argparse.Namespace) -> int:
    owner = _owner_id(args)
    run_id = uuid.UUID(args.run_id)
    run = store.get_run(owner, run_id)
    if run.plan is None:
        sys.exit("ERROR: run has no plan; nothing to resume")

    config = _config_from(args)
    adapter = _build_adapter(run.plan.site, config)
    requirements = store.get_description(
        owner, run.description_id, run.description_version
    ).requirements
    with audit.acting(owner, audit.resumed(owner, run_id)):
        sequential.resume(
            run_id, adapter,
            config.for_site(run.plan.site, reservation_kind_of(requirements.items)),
        )
    updated = _print_summary(owner, run_id)
    return 0 if updated.state == RunState.passed else 1


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn

    from cloud_conversations import oidc
    from cloud_conversations.api.app import app
    from cloud_conversations.api.deps import public_url

    config = _config_from(args)
    try:
        for testbed in config.logins():
            oidc.client(testbed)
        public_url()
    except (oidc.OidcError, ConfigError) as exc:
        sys.exit(f"ERROR: the API needs the login settings: {exc}")
    service = config.service
    logging.getLogger("uvicorn.access").addFilter(_drop_query_string)
    uvicorn.run(app, host=args.host or service.host, port=args.port or service.port)
    return 0


def _drop_query_string(record: logging.LogRecord) -> bool:
    if isinstance(record.args, tuple) and len(record.args) == 5:
        client, method, path, version, status = record.args
        record.args = (client, method, str(path).partition("?")[0], version, status)
    return True


def cmd_import_key(args: argparse.Namespace) -> int:
    from cloud_conversations.adapter import user_key

    try:
        public_key = Path(args.public_key).expanduser().read_text()
    except OSError as exc:
        sys.exit(f"ERROR: {exc}")
    key = user_key.import_key(_owner(args), args.name, public_key)
    print(f"{key.name}  {key.keypair_name}  {user_key.fingerprint(key.public_key)}")
    return 0


def cmd_show_keys(args: argparse.Namespace) -> int:
    """Print the imported keys and the keypair name each gets at a site."""
    from cloud_conversations.adapter import user_key

    keys = store.user_keys(_owner_id(args))
    if not keys:
        print("no imported keys; ccx import-key adds one")
        return 1
    for key in keys:
        print(f"{key.name}  {key.keypair_name}  {user_key.fingerprint(key.public_key)}")
    return 0


def cmd_add_local_user(args: argparse.Namespace) -> int:
    """Add a test user who logs in to the page with a password."""
    from cloud_conversations import local_users

    testbed = _local_testbed(args)
    local_users.add(testbed, args.name, _password())
    print(f"added {local_users.user_id(testbed, args.name)}")
    return 0


def cmd_set_local_password(args: argparse.Namespace) -> int:
    from cloud_conversations import local_users

    testbed = _local_testbed(args)
    local_users.set_password(testbed, args.name, _password())
    print(f"set the password of {local_users.user_id(testbed, args.name)}")
    return 0


def cmd_delete_local_user(args: argparse.Namespace) -> int:
    """Delete a local test user and everything they own."""
    from cloud_conversations import local_users

    testbed = _local_testbed(args)
    local_users.delete(testbed, args.name)
    print(f"deleted {local_users.user_id(testbed, args.name)}")
    return 0


def _password() -> str:
    """A password typed twice at the terminal, or the first line of standard input."""
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    typed = getpass.getpass("password: ")
    if getpass.getpass("again: ") != typed:
        sys.exit("ERROR: the two passwords differ")
    return typed


def cmd_show_system_key(args: argparse.Namespace) -> int:
    """Print every system key, or one key's private half."""
    from cloud_conversations.adapter import system_key

    if args.private:
        print(system_key.private_half(args.private), end="")
        return 0
    keys = store.system_keys()
    if not keys:
        print("no system key yet; the first boot creates one")
        return 1
    for k in keys:
        status = "current" if k.retired_at is None else f"retired {k.retired_at:%Y-%m-%d %H:%M}"
        print(f"{k.fingerprint}  created {k.created_at:%Y-%m-%d %H:%M}  {status}")
        print(f"  {system_key.authorized_key(system_key.load(k.private_key))}")
    return 0


def cmd_rotate_system_key(args: argparse.Namespace) -> int:
    """Retire the current system key and create its replacement."""
    from cloud_conversations.adapter import system_key

    retired = store.current_system_key()
    key = system_key.rotate()
    if retired is not None:
        print(f"retired {retired.fingerprint}")
    print(f"current {key.fingerprint}")
    print(f"  {system_key.authorized_key(key)}")
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    """List the configured sites, and a site's node types when asked."""
    config = _config_from(args)
    adapter = credentials.public_adapter(config, testbed_of(_owner_id(args)))
    caps = adapter.capabilities()
    if args.site:
        descriptor = caps.site(args.site)
        if descriptor is None:
            known = ", ".join(s.site for s in caps.sites)
            sys.exit(f"ERROR: unknown site {args.site!r}; known sites: {known}")
        if descriptor.node_catalog_url is None:
            sys.exit(f"ERROR: {args.site} publishes no node catalog")
        try:
            node_types = adapter.published_node_types(args.site)
        except AdapterError as exc:
            sys.exit(f"ERROR: {exc}")
        print(f"{args.site}: {len(node_types)} node types")
        for nt in node_types:
            gpu = f"  gpu={nt.gpu_model}" if nt.gpu_model else ""
            print(
                f"  {nt.name:26} {nt.cpu_count:4} cpu  {nt.mem_gb:5}G  "
                f"{nt.disk_gb:6}G disk  {nt.cpu_arch}{gpu}"
            )
        return 0

    for s in caps.sites:
        kinds = ",".join(k.reservation_kind for k in s.kinds)
        print(f"{s.site:12}  {kinds:32}  net={s.instance_network or '-':12}  {s.auth_url}")
        for nt in s.node_types:
            print(f"    node   {nt.name:22} {nt.cpu_arch} {nt.cpu_count}c {nt.mem_gb}G")
        for fl in s.flavors:
            print(f"    flavor {fl.name:22} {fl.vcpus}c {fl.mem_gb}G")
    return 0


class _SkipCheck(Exception):
    """Raised by a check that cannot run here."""


def cmd_check(args: argparse.Namespace) -> int:
    """Run read-only checks against a site."""
    config = _config_from(args)
    site = args.site
    adapter = _build_adapter(site, config)
    settings = config.for_site(site)
    descriptor = adapter.capabilities().site(site)
    if descriptor is None:
        known = ", ".join(s.site for s in adapter.capabilities().sites)
        sys.exit(f"ERROR: unknown site {site!r}; known sites: {known}")
    offered = descriptor.reserves_by()
    key = (
        _compute_key(args, descriptor, fallback=offered[0])
        if args.node_type or args.flavor else None
    )
    print(
        f"{descriptor.site}  kinds={','.join(k.reservation_kind for k in descriptor.kinds)}  "
        f"net={descriptor.instance_network or '-'}  {descriptor.auth_url}\n"
    )

    def probe(label: str, fn: Callable[[], str]) -> str:
        try:
            detail = fn()
        except _SkipCheck as exc:
            print(f"[SKIP] {label:<44}{exc}")
            return "SKIP"
        except Exception as exc:
            print(f"[FAIL] {label:<44}{type(exc).__name__}: {exc}")
            log.debug("check %s failed", label, exc_info=exc)
            return "FAIL"
        print(f"[PASS] {label:<44}{'' if detail is None else detail}")
        return "PASS"

    def check_keypair() -> str:
        if not args.keypair:
            raise _SkipCheck("pass --keypair")
        kp = adapter.find_keypair(site, args.keypair)
        imported = store.user_key_for_keypair(_owner_id(args), args.keypair)
        if imported is not None:
            return f"imported key {imported.name}" + (
                "" if kp else "; the first launch here creates it"
            )
        if kp is None:
            raise ValueError(f"keypair {args.keypair!r} not found at this site")
        return kp.name

    def check_compute() -> str:
        if key is None:
            raise _SkipCheck(f"pass {' or '.join(_FLAGS[k] for k in offered)}")
        if key == "flavor":
            fl = adapter.find_flavor(site, args.flavor)
            if fl is None:
                raise ValueError(f"flavor {args.flavor!r} not found or not reservable")
            return f"{fl.name} = {fl.vcpus} vcpu, {fl.mem_gb}G"

        types = adapter.published_node_types(site)
        names = {t.name for t in types}
        if args.node_type not in names:
            raise ValueError(
                f"{args.node_type!r} not in the {len(names)} node types at this site; "
                + (", ".join(sorted(names)[:6]) or "(catalog empty)")
            )
        hit = next(t for t in types if t.name == args.node_type)
        return f"{args.node_type} = {hit.cpu_count} cpu, {hit.mem_gb}G"

    def check_availability() -> str:
        if key is None:
            raise _SkipCheck(f"pass {' or '.join(_FLAGS[k] for k in offered)}")
        flavor_id = None
        if key == "flavor":
            fl = adapter.find_flavor(site, args.flavor)
            flavor_id = fl.flavor_id if fl else None
            if flavor_id is None:
                raise ValueError("flavor lookup failed")
        windows = adapter.published_availability(
            site, args.node_type, flavor_id, float(args.hours)
        )
        unit = f"{args.flavor} instances" if key == "flavor" else f"{args.node_type} hosts"
        if not windows:
            return f"calendar readable; no {unit} free for the next {args.hours}h"
        return (
            f"{windows[0].count} {unit} free from "
            f"{windows[0].start:%Y-%m-%d %H:%M} UTC"
        )

    outcomes = [
        probe("login + lease list", lambda: f"{len(adapter.list_leases(site))} lease(s)"),
        probe("instance list", lambda: f"{len(adapter.list_instances(site))} instance(s)"),
        probe(
            f"keypair {args.keypair!r}" if args.keypair else "keypair",
            check_keypair,
        ),
        probe(
            f"image {args.os_release or (args.image_name or settings.image_name)!r}",
            lambda: _image_detail(
                adapter,
                site,
                None if args.os_release else (args.image_name or settings.image_name),
                args.os_release,
            ),
        ),
        probe("compute catalog", check_compute),
        probe("availability calendar", check_availability),
    ]

    failed = outcomes.count("FAIL")
    skipped = outcomes.count("SKIP")
    print()
    if failed:
        print(f"{failed} check(s) failed; do not provision against {site} yet.")
        return 1
    if skipped:
        print(
            f"{len(outcomes) - skipped}/{len(outcomes)} read-only checks passed for "
            f"{site}; {skipped} not applicable here."
        )
        return 0
    print(f"All read-only checks passed for {site}.")
    return 0


def _image_detail(
    adapter: TestbedAdapter, site: str, image_name: str | None, os_release: str | None
) -> str:
    img = adapter.find_image(site, os_release=os_release, name=image_name)
    if img is None:
        descriptor = adapter.capabilities().site(site)
        named = descriptor.images.name_for(os_release) if descriptor and os_release else "?"
        wanted = image_name or named
        raise ValueError(f"no image named {wanted!r} at this site")
    return f"{img.name} ({img.image_id})"


def main() -> None:
    _configure_logging()
    p = argparse.ArgumentParser(prog="ccx", description="cloud-conversations CLI")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument_group("settings overrides").add_argument(
        "--config", default=None, help="Path to config.toml; overrides CC_CONFIG_FILE"
    )

    runs_steps = argparse.ArgumentParser(add_help=False)
    runs_steps.add_argument_group("settings overrides").add_argument(
        "--ssh-user", default=None, help="Login on the built host"
    )

    releasing = argparse.ArgumentParser(add_help=False)
    releasing.add_argument("--yes", action="store_true", help="Release without asking first")

    model_opts = argparse.ArgumentParser(add_help=False)
    endpoint = model_opts.add_argument_group("settings overrides")
    endpoint.add_argument(
        "--model", default=None, help="Model at the endpoint; overrides CC_LLM_MODEL"
    )
    endpoint.add_argument(
        "--temperature", type=float, default=None, help="Sampling temperature, 0 to 2"
    )

    sub = p.add_subparsers(dest="command", required=True)

    p_fix = sub.add_parser(
        "describe-fixture",
        help="Write a description to the DB (override any value for a real site)",
        parents=[common],
    )
    p_fix.add_argument("--name", required=True, choices=list(FIXTURE_NAMES))
    p_fix.add_argument("--conversation-id", default=None)
    p_fix.add_argument(
        "--description-id", default=None, help="Append a version to this lineage"
    )
    p_fix.add_argument("--site", default=None, help="e.g. CHI@UC or CHI@TACC")
    p_fix.add_argument("--node-type", default=None, help="bare metal")
    p_fix.add_argument("--flavor", default=None, help="a VM")
    p_fix.add_argument("--hours", type=int, default=None)
    p_fix.add_argument(
        "--start-at",
        default=None,
        help="ISO 8601 lease start, e.g. 2026-09-08T14:00Z; omit to start as soon as "
             "the calendar allows",
    )
    p_fix.add_argument(
        "--image-name",
        default=None,
        help="Exact image name; overrides the config",
    )
    p_fix.add_argument(
        "--os-release",
        default=None,
        help="Release the rubric expects the booted host to report. Set --image-name "
             "to match.",
    )
    p_fix.add_argument("--min-cpus", type=int, default=None, help="adds a cpu_count >= check")

    p_desc = sub.add_parser(
        "describe",
        help="Turn a prompt into a description, using a model",
        parents=[common, model_opts],
    )
    p_desc.add_argument("prompt")
    p_desc.add_argument("--site", default=None, help="Restrict the catalog to one site")
    p_desc.add_argument(
        "--description-id", default=None, help="Revise this lineage instead"
    )
    p_desc.add_argument(
        "--version", type=int, default=None, help="Revise this version, not the latest"
    )
    p_desc.add_argument(
        "--verbose", action="store_true", help="Report each round on stderr as it runs"
    )

    p_conv = sub.add_parser(
        "show-conversation", help="Print a stored conversation", parents=[common]
    )
    p_conv.add_argument("--conversation-id", required=True)

    p_ask = sub.add_parser(
        "ask",
        help="Answer a question from the knowledge base",
        parents=[common, model_opts],
    )
    p_ask.add_argument(
        "question", nargs="?", default=None, help="Omit it to ask at a prompt"
    )
    p_ask.add_argument(
        "--verbose",
        action="store_true",
        help="Report what the reranker scored, on stderr",
    )

    p_inv = sub.add_parser(
        "inventory",
        help="List the leases, instances and images you hold",
        parents=[common, model_opts],
    )
    p_inv.add_argument("request", help='What to list, as in "my leases and instances"')
    p_inv.add_argument(
        "--site", default=None, help="One site; the default is every site you hold"
    )

    p_index = sub.add_parser(
        "index", help="Build the knowledge index from the configured sources",
        parents=[common],
    )
    refreshing = p_index.add_mutually_exclusive_group()
    refreshing.add_argument(
        "--refresh",
        nargs="*",
        metavar="SOURCE",
        default=None,
        help="Fetch only these sources again; the rest come from the cache",
    )
    refreshing.add_argument(
        "--use-cache",
        action="store_true",
        help="Fetch nothing; rebuild from the cache",
    )

    sub.add_parser(
        "knowledge", help="Show what the live knowledge index holds", parents=[common]
    )

    p_chk = sub.add_parser("check", help="Read-only checks against a site", parents=[common])
    p_chk.add_argument("--site", required=True)
    p_chk.add_argument("--keypair", default=None)
    p_chk.add_argument("--node-type", default=None)
    p_chk.add_argument("--flavor", default=None)
    p_chk.add_argument(
        "--image-name",
        default=None,
        help="Image a run would boot",
    )
    p_chk.add_argument(
        "--os-release",
        default=None,
        help="Check the CC-<Distro><Version> derivation instead of --image-name",
    )
    p_chk.add_argument("--hours", type=int, default=2)

    p_prov = sub.add_parser(
        "provision",
        help="Provision against an existing description",
        parents=[common, runs_steps, releasing],
    )
    p_prov.add_argument("--description-id", required=True)
    p_prov.add_argument("--version", type=int, default=None)
    p_prov.add_argument("--site", default=None, help="Defaults to the description's site")
    p_prov.add_argument("--project-id", default=None)
    p_prov.add_argument("--keypair", default=None, help=_KEYPAIR_HELP)
    p_prov.add_argument("--teardown", action="store_true", help="Release resources when done")

    p_build = sub.add_parser(
        "build",
        help="Provision a description, and let the agent recover a failed attempt",
        parents=[common, runs_steps, model_opts, releasing],
    )
    p_build.add_argument("--description-id", default=None, help="Start a build")
    p_build.add_argument("--version", type=int, default=None)
    p_build.add_argument("--site", default=None, help="Defaults to the description's site")
    p_build.add_argument(
        "--run-id",
        default=None,
        help="Continue the build this run belongs to, instead of starting one",
    )
    p_build.add_argument("--project-id", default=None)
    p_build.add_argument("--keypair", default=None, help=_KEYPAIR_HELP)
    p_build.add_argument("--teardown", action="store_true", help="Release resources when done")
    p_build.add_argument(
        "--verbose", action="store_true", help="Report each round on stderr as it runs"
    )

    p_run = sub.add_parser(
        "run",
        help="describe-fixture + provision in one call",
        parents=[common, runs_steps, releasing],
    )
    p_run.add_argument("--fixture", choices=list(FIXTURE_NAMES), default="baremetal")
    p_run.add_argument("--site", default=None, help="Override the fixture's site")
    p_run.add_argument("--keypair", default=None, help=_KEYPAIR_HELP)
    p_run.add_argument(
        "--image-name", default=None, help="Exact image name; overrides the config"
    )
    p_run.add_argument("--project-id", default=None)
    p_run.add_argument("--teardown", action="store_true", help="Release resources when done")

    p_build_show = sub.add_parser(
        "show-build", help="Print a build end to end: both agents, every run",
        parents=[common],
    )
    p_build_show.add_argument("--run-id", default=None, help="Any run in the build")
    p_build_show.add_argument(
        "--description-id", default=None, help="Its most recent run"
    )

    p_show = sub.add_parser(
        "show-run", help="Display run state and event stream", parents=[common]
    )
    p_show.add_argument("--run-id", required=True)
    p_show.add_argument("--events", action="store_true")
    p_show.add_argument("--json", action="store_true")

    p_td = sub.add_parser(
        "teardown", help="Release resources for a run", parents=[common, releasing]
    )
    p_td.add_argument("--run-id", required=True)

    p_res = sub.add_parser(
        "resume",
        help="Resume an incomplete or failed run",
        parents=[common, runs_steps],
    )
    p_res.add_argument("--run-id", required=True)

    p_api = sub.add_parser("api", help="Serve the HTTP API", parents=[common])
    p_api.add_argument("--host", default=None, help="Interface to bind; default from [service]")
    p_api.add_argument("--port", type=int, default=None, help="Port to bind")

    p_cap = sub.add_parser("capabilities", help="Show the configured sites", parents=[common])
    p_cap.add_argument(
        "--site", default=None, help="Also list this site's node types, read live"
    )

    p_import = sub.add_parser(
        "import-key", help="Import a public key to name as --keypair at any site",
        parents=[common],
    )
    p_import.add_argument("--name", required=True, help="The key's name in cloud conversations")
    p_import.add_argument("--public-key", required=True, help="Path to an OpenSSH public key")

    sub.add_parser(
        "show-keys", help="Print the imported keys and their keypair names", parents=[common]
    )

    for command, does in (
        ("add-local-user", "Add a local test user, reading a password"),
        ("set-local-password", "Set a local test user's password"),
        ("delete-local-user", "Delete a local test user and everything they own"),
    ):
        sub.add_parser(command, help=does, parents=[common]).add_argument(
            "--name", required=True, help="The name after local:"
        )

    p_key = sub.add_parser(
        "show-system-key", help="Print the system keys new and older instances trust"
    )
    p_key.add_argument(
        "--private", metavar="FINGERPRINT", default=None, help="Print this key's private half"
    )

    sub.add_parser(
        "rotate-system-key", help="Retire the system key; new instances get a new one"
    )

    args = p.parse_args()
    if getattr(args, "config", None):
        os.environ[CONFIG_FILE_ENV] = args.config
    handlers = {
        "check": cmd_check,
        "describe": cmd_describe,
        "describe-fixture": cmd_describe_fixture,
        "show-conversation": cmd_show_conversation,
        "ask": cmd_ask,
        "inventory": cmd_inventory,
        "index": cmd_index,
        "knowledge": cmd_knowledge,
        "provision": cmd_provision,
        "build": cmd_build,
        "run": cmd_run,
        "show-build": cmd_show_build,
        "show-run": cmd_show_run,
        "teardown": cmd_teardown,
        "resume": cmd_resume,
        "api": cmd_api,
        "capabilities": cmd_capabilities,
        "import-key": cmd_import_key,
        "show-keys": cmd_show_keys,
        "add-local-user": cmd_add_local_user,
        "set-local-password": cmd_set_local_password,
        "delete-local-user": cmd_delete_local_user,
        "show-system-key": cmd_show_system_key,
        "rotate-system-key": cmd_rotate_system_key,
    }
    try:
        raise SystemExit(handlers[args.command](args))
    except (
        store.NotFoundError,
        store.ConflictError,
        store.AccessError,
        store.ConcurrencyError,
        store.DuplicateKeyError,
        IllegalTransitionError,
        AdapterError,
        crypt.CryptError,
        ConfigError,
        KnowledgeError,
        LLMError,
        ValueError,
    ) as exc:
        sys.exit(f"ERROR: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
