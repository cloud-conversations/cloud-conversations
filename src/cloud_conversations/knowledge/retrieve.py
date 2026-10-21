"""Finding the passages that answer a question, and assembling them into context."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel

import cloud_conversations.db.store as store
from cloud_conversations.knowledge.embed import BgeEmbedder, BgeReranker, Embedder, Reranker
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.schema.knowledge import Knowledge, Passage

log = logging.getLogger(__name__)

PRIMARY_SECTION = "PRIMARY DOCUMENTATION"
SUPPLEMENTARY_SECTION = "SUPPLEMENTARY CONTEXT"

NOTHING_FOUND = "the knowledge base has nothing on that question"


class Scored(BaseModel):
    """A candidate chunk and the score the reranker gave it."""

    passage: Passage
    score: float
    selected: bool


class Retrieved(BaseModel):
    """What one search of the knowledge base returned."""

    sources: list[str]
    context: str
    candidates: list[Scored]  # every candidate scored, best first


class KnowledgeBase(Protocol):
    """A knowledge base an agent can search."""

    def search(self, question: str) -> str: ...


class Retriever:
    """The knowledge base, searched with the configured models."""

    def __init__(self, knowledge: Knowledge) -> None:
        self.knowledge = knowledge
        self._models: tuple[Embedder, Reranker] | None = None

    def retrieve(self, question: str) -> Retrieved:
        """One search. Raises KnowledgeError when the models cannot be loaded."""
        if self._models is None:
            self._models = _load_models(self.knowledge)
        embedder, reranker = self._models
        return search(question, embedder, reranker, self.knowledge)

    def search(self, question: str) -> str:
        """One search, as text for a model. A failure comes back as text."""
        try:
            found = self.retrieve(question)
        except KnowledgeError as exc:
            return f"that search failed: {exc}"
        return found.context or NOTHING_FOUND


# The loaded models, by model name and device.
_LOADED: dict[tuple[str, str, str, str], tuple[Embedder, Reranker]] = {}


def _load_models(knowledge: Knowledge) -> tuple[Embedder, Reranker]:
    """The embedder and the reranker, loaded once for the life of the process."""
    key = (
        knowledge.embedding_model,
        knowledge.embedding_device,
        knowledge.reranker_model,
        knowledge.reranker_device,
    )
    if key not in _LOADED:
        _LOADED[key] = (BgeEmbedder(knowledge), BgeReranker(knowledge))
    return _LOADED[key]


def open_knowledge(knowledge: Knowledge) -> Retriever | None:
    """The knowledge base, or None if no index has been built.

    Raises KnowledgeError if the index was built with another embedding model.
    """
    index = store.latest_index()
    if index is None:
        return None
    if index.embedding_model != knowledge.embedding_model:
        raise KnowledgeError(
            f"the index was built with {index.embedding_model} and embedding_model "
            f"is {knowledge.embedding_model}; rebuild it with ccx index"
        )
    return Retriever(knowledge)


def usable_knowledge(
    knowledge: Knowledge, report: Callable[[str], None] = log.warning
) -> Retriever | None:
    """The knowledge base, or None if it is disabled or unusable."""
    if not knowledge.enabled:
        return None
    try:
        return open_knowledge(knowledge)
    except KnowledgeError as exc:
        report(f"the knowledge base is unusable: {exc}")
        return None


def search(
    question: str,
    embedder: Embedder,
    reranker: Reranker,
    knowledge: Knowledge,
) -> Retrieved:
    """Search the index, rerank what it returned, and build the context."""
    query = knowledge.query_prefix + question
    matched = store.search_chunks(
        embedder.embed_query(query), knowledge.fetch_candidates
    )
    pool = _pool(matched, knowledge)
    candidates = _rank(pool, reranker.score(query, [c.content for c in pool]), knowledge)

    parents = store.parents_of(
        [c.passage.parent_id for c in candidates if c.selected]
    )
    chosen = [
        parents[c.passage.parent_id]
        for c in candidates
        if c.selected and c.passage.parent_id in parents
    ]
    return Retrieved(
        sources=[p.url for p in chosen],
        context=_context(chosen, knowledge),
        candidates=candidates,
    )


def _pool(matched: list[Passage], knowledge: Knowledge) -> list[Passage]:
    """The nearest chunks, capped per source."""
    by_source: dict[str, list[Passage]] = {}
    for passage in matched:
        by_source.setdefault(passage.source, []).append(passage)
    per_source = max(
        knowledge.passages, knowledge.pool_candidates // max(len(by_source), 1)
    )
    return [p for group in by_source.values() for p in group[:per_source]]


def _rank(
    pool: list[Passage], scores: list[float], knowledge: Knowledge
) -> list[Scored]:
    """Every candidate, best first, marked if selected for the context."""
    ranked = sorted(zip(pool, scores, strict=True), key=lambda pair: pair[1], reverse=True)
    urls: set[str] = set()
    scored: list[Scored] = []
    for passage, score in ranked:
        selected = (
            score >= knowledge.min_reranker_score
            and len(urls) < knowledge.passages
            and passage.url not in urls
        )
        if selected:
            urls.add(passage.url)
        scored.append(Scored(passage=passage, score=float(score), selected=selected))
    return scored


def _context(chosen: list[Passage], knowledge: Knowledge) -> str:
    """The passages as prompt text, official documentation first."""
    primary: list[str] = []
    supplementary: list[str] = []
    for passage in chosen:
        source = knowledge.sources.get(passage.source)
        label = source.label if source else passage.source
        text = f"[{label}: {passage.url}]\n{passage.content}"
        if source is not None and source.primary:
            primary.append(text)
        else:
            supplementary.append(text)

    sections = [
        _section(heading, parts)
        for heading, parts in (
            (PRIMARY_SECTION, primary),
            (SUPPLEMENTARY_SECTION, supplementary),
        )
        if parts
    ]
    return "\n\n".join(sections)


def _section(heading: str, parts: list[str]) -> str:
    plural = "s" if len(parts) > 1 else ""
    body = "\n\n---\n\n".join(parts)
    return f"=== {heading} ({len(parts)} passage{plural}) ===\n\n{body}"
