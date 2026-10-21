"""Checks on what the agents propose. Each returns reasons to refuse, or an empty list."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from cloud_conversations.adapter.protocol import Flavor
from cloud_conversations.agent.catalog import Catalog, SiteDoc
from cloud_conversations.agent.contract import (
    AbandonStep,
    BuildStep,
    ImageSearchStep,
    InspectStep,
    KnowledgeStep,
    ProposedDescription,
    RetryStep,
)
from cloud_conversations.db.store import Run
from cloud_conversations.knowledge.retrieve import KnowledgeBase
from cloud_conversations.runner.sequential import PlanError, build_plan
from cloud_conversations.schema.description import (
    RESERVATION_KINDS,
    VERIFIABLE_KEYS,
    ComputeKey,
    ImageChoice,
    Requirement,
    Requirements,
)
from cloud_conversations.schema.images import parse_release
from cloud_conversations.schema.runs import SETTLED_STATES
from cloud_conversations.schema.search import Candidates, NodeQuery

# Fields that make one search different from another.
_FILTERS: set[str] = set(NodeQuery.model_fields) - {"intent"}

# Fields that narrow a search to fewer than every node.
_NARROWING = ("node_type", "flavor", "arch", "gpu", "infiniband", "min_ram_gb")

# Narrowing fields allowed for each reserve_by.
_ANSWERED: dict[ComputeKey, frozenset[str]] = {
    "node_type": frozenset({"node_type", "arch", "gpu", "infiniband", "min_ram_gb"}),
    "flavor": frozenset({"flavor", "gpu", "min_ram_gb"}),
}


def validate_query(
    query: NodeQuery,
    catalog: Catalog,
    seen: list[NodeQuery],
) -> list[str]:
    """Check that a search is worth spending a round on."""
    reasons: list[str] = []
    doc = catalog.site(query.site) if query.site is not None else None

    if query.site is not None and doc is None:
        reasons.append(f"unknown site {query.site!r}; known sites: {_site_names(catalog)}")
    elif doc is not None and query.reserve_by not in doc.reserve_by:
        return [_not_offered(doc, query.reserve_by)]

    if not any(getattr(query, f) is not None for f in _NARROWING):
        reasons.append(
            f"a search must narrow: set one of {', '.join(_NARROWING)}"
        )

    reasons += _query_compute(query, catalog, doc)
    reasons += _window(
        query.start_at,
        query.duration_hours,
        _query_cap(query, catalog, doc),
        max((s.max_lead_days for s in _sites_for(catalog, doc, query.reserve_by)), default=0),
    )

    if any(q.model_dump(include=_FILTERS) == query.model_dump(include=_FILTERS) for q in seen):
        reasons.append("this search has already been run; widen it or move the window")

    return reasons


def _query_compute(query: NodeQuery, catalog: Catalog, doc: SiteDoc | None) -> list[str]:
    """Check that a search's hardware exists at its site."""
    reasons: list[str] = []
    sites = _sites_for(catalog, doc, query.reserve_by)
    answered = _ANSWERED[query.reserve_by]

    unanswered = [f for f in _NARROWING if f not in answered and getattr(query, f) is not None]
    if unanswered:
        reasons.append(
            f"a search by {query.reserve_by} takes {', '.join(sorted(answered))}, "
            f"not {', '.join(unanswered)}"
        )

    if query.flavor is not None:
        known = {f.name for s in sites for f in s.flavors}
        if query.flavor not in known:
            reasons.append(
                f"unknown flavor {query.flavor!r}; known flavors: {', '.join(sorted(known))}"
            )

    if query.node_type is not None:
        known = {n.name for s in sites for n in s.node_types}
        if query.node_type not in known:
            reasons.append(
                f"unknown node_type {query.node_type!r}; known node types: "
                f"{', '.join(sorted(known))}"
            )

    if query.arch is not None:
        known = {n.cpu_arch for s in sites for n in s.node_types}
        if query.arch not in known:
            reasons.append(
                f"unknown arch {query.arch!r}; known architectures: {', '.join(sorted(known))}"
            )

    return reasons


def _sites_for(catalog: Catalog, doc: SiteDoc | None, key: ComputeKey) -> list[SiteDoc]:
    return [doc] if doc is not None else [s for s in catalog.sites if key in s.reserve_by]


def _not_offered(doc: SiteDoc, key: str) -> str:
    return f"{doc.site} takes {' or '.join(doc.reserve_by)}, not {key}"


def validate_image_search(
    step: ImageSearchStep, catalog: Catalog, searched: list[tuple[str, str]]
) -> list[str]:
    """Reasons to refuse an image search."""
    doc = catalog.site(step.site)
    if doc is None:
        return [f"unknown site {step.site!r}; known sites: {_site_names(catalog)}"]
    if not doc.images:
        return [f"the images at {step.site} are not visible: {doc.images_unseen}"]
    if (step.site, step.text.strip().lower()) in searched:
        return ["this image search has already been run; search for something else"]
    return []


def validate_knowledge(
    question: str, knowledge: KnowledgeBase | None, asked: list[str]
) -> list[str]:
    """Check that a knowledge-base search is worth spending a round on."""
    if knowledge is None:
        return ["there is no knowledge base to search here"]
    if _normalised(question) in {_normalised(q) for q in asked}:
        return ["that question has already been asked; it says the same thing"]
    return []


def _normalised(question: str) -> str:
    return " ".join(question.lower().split())


def validate_build_step(
    step: BuildStep,
    run: Run,
    inspected: list[str],
    leases_left: int,
    knowledge: KnowledgeBase | None,
) -> list[str]:
    """Check that a build step may be executed against this run."""
    if leases_left <= 0 and not isinstance(step, AbandonStep):
        return ["the lease budget for this build is spent; abandon is the only action left"]

    if isinstance(step, KnowledgeStep):
        return validate_knowledge(step.question, knowledge, inspected)

    if isinstance(step, InspectStep):
        if step.read in inspected:
            return [f"{step.read} has already been read this build; it says the same thing"]
        return []

    if isinstance(step, RetryStep):
        reasons = []
        if run.state in SETTLED_STATES:
            reasons.append(
                f"this run is {run.state.value}; its resources are gone and there is "
                "nothing to run again"
            )
        if run.plan is None:
            reasons.append("this run has no plan; there is nothing to run again")
        return reasons

    return []


def validate_description(
    proposed: ProposedDescription,
    catalog: Catalog,
    seen: list[Candidates],
) -> list[str]:
    """Check that an answer may be stored."""
    doc = catalog.site(proposed.site)
    if doc is None:
        return [f"unknown site {proposed.site!r}; known sites: {_site_names(catalog)}"]
    reasons: list[str] = []
    chosen, compute_reasons = _answer_compute(proposed, doc)
    reasons += compute_reasons

    if chosen is not None:
        reasons += _found_free(proposed, chosen[1], seen)
        reasons += _stated_arch(proposed, chosen, doc)
        reasons += _flavor_gpu(proposed, chosen, doc)

    reasons += _image(proposed, doc, doc.arch_of(*chosen) if chosen else None)

    count = proposed.value_of("count")
    if count is not None and count != 1:
        reasons.append(f"count={count!r} is not supported; a description boots one instance")

    reasons += _window(
        proposed.start_at,
        proposed.duration_hours,
        doc.lease_cap(*chosen) if chosen else max(doc.max_lease_hours.values()),
        doc.max_lead_days,
    )

    reasons += _rubric(proposed)

    if not reasons:
        try:
            build_plan(
                to_requirements(proposed, catalog),
                frozenset(c.kind for c in proposed.rubric),
                uuid.uuid4(),
                doc.descriptor,
                plan_seq=0,
                keypair="",
            )
        except PlanError as exc:
            reasons.append(str(exc))

    return reasons


def _image(
    proposed: ProposedDescription, doc: SiteDoc, cpu_arch: str | None
) -> list[str]:
    """Check that the answer names an image for the chosen architecture."""
    image_name = proposed.value_of("image_name")
    os_release = proposed.value_of("os_release")
    if image_name is None and os_release is None:
        return ["give an image_name or an os_release requirement to choose an image"]
    if os_release is not None and parse_release(str(os_release)) is None:
        return [
            f"os_release {os_release!r} names no distribution and version; "
            f"give it as, for example, Ubuntu 24.04"
        ]
    if not doc.images:
        return []

    image = doc.image_for(image_name, os_release, cpu_arch)
    if image is None:
        wanted = str(image_name) if image_name else doc.descriptor.images.name_for(str(os_release))
        built = f" built for {cpu_arch}" if cpu_arch else ""
        supported = sorted({i.name for i in doc.images if i.supported})
        others = len({i.name for i in doc.images if not i.supported})
        more = f", and {others} other images (search_images to look)" if others else ""
        return [
            f"no image named {wanted}{built} at {doc.site}; it supports "
            f"{', '.join(supported) or 'none'}{more}"
        ]
    if image_name is not None and str(image_name) == image.name:
        return []
    stated = (
        f"image_name {image_name!r} is not an image at {doc.site}"
        if image_name is not None
        else f"os_release {os_release!r} alone does not name an image"
    )
    return [
        f"{stated}; give image_name {image.name!r}, which is what {doc.site} boots "
        f"for {cpu_arch or 'this site'}"
    ]


def _query_cap(query: NodeQuery, catalog: Catalog, doc: SiteDoc | None) -> int:
    """The longest lease allowed at any site the search covers."""
    key = query.reserve_by
    sites = _sites_for(catalog, doc, key)
    if query.gpu is True:
        return max((s.max_gpu_lease_hours[key] for s in sites), default=0)
    named = query.node_type or query.flavor
    return max((s.lease_cap(key, named) for s in sites), default=0)


def _rubric(proposed: ProposedDescription) -> list[str]:
    """Check that the rubric verifies the requirements it can measure."""
    reasons: list[str] = []
    keys = {item.key for item in proposed.items}
    kinds = {check.kind for check in proposed.rubric}

    for check in proposed.rubric:
        if check.from_requirement is None:
            continue
        if check.kind not in VERIFIABLE_KEYS:
            reasons.append(
                f"rubric check {check.id!r} is kind {check.kind}, which verifies no "
                "requirement; leave its from_requirement null"
            )
        elif check.from_requirement not in keys:
            reasons.append(
                f"rubric check {check.id!r} reads from_requirement "
                f"{check.from_requirement!r}, which is not a requirement here"
            )

    for key in sorted((keys & VERIFIABLE_KEYS) - kinds):
        reasons.append(
            f"the rubric does not verify the {key} requirement; add a {key} check"
        )

    if not proposed.rubric:
        reasons.append("the rubric must contain at least one check")
    elif "ssh_reachable" not in kinds:
        reasons.append("the rubric must contain an ssh_reachable check")
    return reasons


def _answer_compute(
    proposed: ProposedDescription, doc: SiteDoc
) -> tuple[tuple[ComputeKey, str] | None, list[str]]:
    """The answer's node type or flavor, or the reasons it is unusable."""
    named = [item for item in proposed.items if item.key in RESERVATION_KINDS]
    if not named:
        return None, [f"{doc.site} requires a {' or '.join(doc.reserve_by)} requirement"]
    if len(named) > 1:
        return None, ["give one of node_type or flavor, not both"]

    key: ComputeKey = "node_type" if named[0].key == "node_type" else "flavor"
    value = str(named[0].value)
    if key not in doc.reserve_by:
        return None, [_not_offered(doc, key)]

    if doc.compute(key, value) is None:
        available = (
            {n.name for n in doc.node_types}
            if key == "node_type"
            else {f.name for f in doc.flavors}
        )
        return None, [
            f"unknown {key} {value!r} at {doc.site}; "
            f"the catalog lists {', '.join(sorted(available))}"
        ]
    return (key, value), []


def _stated_arch(
    proposed: ProposedDescription, chosen: tuple[ComputeKey, str], doc: SiteDoc
) -> list[str]:
    """Check that a stated cpu_arch is the architecture of the compute chosen."""
    stated = proposed.value_of("cpu_arch")
    actual = doc.arch_of(*chosen)
    if stated is None or actual is None or stated == actual:
        return []
    return [f"cpu_arch {stated!r} does not match {chosen[1]}, which is {actual}"]


def _flavor_gpu(
    proposed: ProposedDescription, chosen: tuple[ComputeKey, str], doc: SiteDoc
) -> list[str]:
    """Check that an answer at a GPU flavor gives gpu_count, not gpu_model."""
    flavor = doc.compute(*chosen)
    if not isinstance(flavor, Flavor):
        return []
    reasons: list[str] = []
    if proposed.value_of("gpu_model") is not None:
        reasons.append("a flavor names no GPU model; give gpu_count, not gpu_model")
    if flavor.gpu and proposed.value_of("gpu_count") != flavor.gpu_count:
        reasons.append(
            f"{flavor.name} holds {flavor.gpu_count} GPU(s); "
            f"give a gpu_count requirement of {flavor.gpu_count}"
        )
    return reasons


def _found_free(
    proposed: ProposedDescription, compute: str, seen: list[Candidates]
) -> list[str]:
    """Check that a search found this hardware free in the requested window."""
    matched = [
        candidates
        for candidates in seen
        for item in candidates.items
        if item.site == proposed.site and item.compute == compute and item.free >= 1
    ]
    if not matched:
        return [
            f"no search found {compute} free at {proposed.site}; "
            "search for it before answering with it"
        ]
    if not any(_same_window(proposed, c) for c in matched):
        windows = ", ".join(sorted({_describe_window(c) for c in matched}))
        remedy = (
            "give start_at to answer for one of those, or search with it omitted"
            if proposed.start_at is None
            else "search that window before answering with it"
        )
        return [
            f"{compute} at {proposed.site} was found free for {windows}, "
            f"not for {_describe_ask(proposed)}; {remedy}"
        ]
    return []


def _same_window(proposed: ProposedDescription, candidates: Candidates) -> bool:
    if candidates.end - candidates.start != timedelta(hours=proposed.duration_hours):
        return False
    if proposed.start_at is None:
        return candidates.query.start_at is None
    return proposed.start_at == candidates.start


def _describe_window(candidates: Candidates) -> str:
    hours = (candidates.end - candidates.start).total_seconds() / 3600
    when = "as soon as possible" if candidates.query.start_at is None else (
        candidates.start.isoformat()
    )
    return f"{hours:g}h from {when}"


def _describe_ask(proposed: ProposedDescription) -> str:
    when = (
        "as soon as possible" if proposed.start_at is None else proposed.start_at.isoformat()
    )
    return f"{proposed.duration_hours}h from {when}"


def _window(
    start_at: datetime | None,
    duration_hours: int,
    cap_hours: int,
    lead_days: int,
) -> list[str]:
    """Check that a lease window is within the site's limits."""
    reasons: list[str] = []
    if not 1 <= duration_hours <= cap_hours:
        reasons.append(
            f"duration_hours={duration_hours} is outside 1 to {cap_hours}"
        )
    if start_at is not None:
        now = datetime.now(timezone.utc)
        if start_at <= now:
            reasons.append(f"start_at {start_at.isoformat()} is in the past")
        elif start_at - now > timedelta(days=lead_days):
            reasons.append(
                f"start_at {start_at.isoformat()} is more than {lead_days} days ahead"
            )
    return reasons


def to_requirements(proposed: ProposedDescription, catalog: Catalog) -> Requirements:
    """Turn an accepted answer into a run's requirements."""
    doc = catalog.site(proposed.site)
    image = doc.image_for(
        proposed.value_of("image_name"), proposed.value_of("os_release"),
        proposed.value_of("cpu_arch"),
    ) if doc else None
    return Requirements(
        site=proposed.site,
        start_at=proposed.start_at,
        start_source=proposed.start_source,
        duration_hours=proposed.duration_hours,
        duration_source=proposed.duration_source,
        items=[Requirement.model_validate(item.model_dump()) for item in proposed.items],
        image=(
            ImageChoice(
                image_id=image.image_id, name=image.name, supported=image.supported,
                testbed=doc.testbed_label,
            )
            if image and doc else None
        ),
    )


def _site_names(catalog: Catalog) -> str:
    return ", ".join(sorted(s.site for s in catalog.sites))
