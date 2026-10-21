"""Retrieval against a real index: the pool, the reranker, and the context built."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import cloud_conversations.db.store as store
from cloud_conversations.knowledge import chunks, index, retrieve
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.knowledge.retrieve import (
    NOTHING_FOUND,
    open_knowledge,
    search,
    usable_knowledge,
)
from cloud_conversations.schema.knowledge import Document, Knowledge
from tests.fakes import KNOWLEDGE, LEASES, publish

FETCHED = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _document(source: str, slug: str, title: str, content: str) -> Document:
    return Document(
        source=source,
        url=f"https://{source}.invalid/{slug}",
        title=title,
        content=content,
        fetched_at=FETCHED,
    )


NOTEBOOKS = _document(
    "readthedocs", "notebooks", "Notebooks",
    "Open a Jupyter notebook from the Trovi artifact catalogue.",
)


def test_the_nearest_chunk_to_a_question_comes_back_first(embedder):
    publish([LEASES, NOTEBOOKS], KNOWLEDGE, embedder)

    nearest = store.search_chunks(
        embedder.embed_query("creating a lease in Blazar"), 2
    )

    assert [p.url for p in nearest] == [LEASES.url, NOTEBOOKS.url]


def test_only_as_many_chunks_as_asked_for_come_back(embedder, reranker):
    knowledge = KNOWLEDGE.model_copy(update={"fetch_candidates": 1})
    publish([LEASES, NOTEBOOKS], knowledge, embedder)

    found = search("creating a lease in Blazar", embedder, reranker, knowledge)

    assert [c.passage.url for c in found.candidates] == [LEASES.url]


def test_the_question_is_embedded_with_the_configured_prefix(embedder, reranker):
    knowledge = KNOWLEDGE.model_copy(update={"query_prefix": "About Chameleon: "})
    publish([LEASES], knowledge, embedder)
    embedder.queries.clear()

    search("what is a lease", embedder, reranker, knowledge)

    assert embedder.queries == ["About Chameleon: what is a lease"]


def test_the_pool_the_reranker_scores_is_capped_per_source(embedder, reranker):
    knowledge = KNOWLEDGE.model_copy(update={"pool_candidates": 6, "passages": 1})
    publish(
        [
            _document(source, f"page-{n}", f"Page {n}", f"A lease page about {n}.")
            for source in ("readthedocs", "blog")
            for n in range(5)
        ],
        knowledge,
        embedder,
    )

    found = search("lease", embedder, reranker, knowledge)

    by_source: dict[str, int] = {}
    for candidate in found.candidates:
        by_source[candidate.passage.source] = (
            by_source.get(candidate.passage.source, 0) + 1
        )
    assert by_source == {"readthedocs": 3, "blog": 3}


def test_a_candidate_below_the_reranker_threshold_is_not_selected(embedder, reranker):
    publish([LEASES, NOTEBOOKS], KNOWLEDGE, embedder)

    found = search("lease Blazar", embedder, reranker, KNOWLEDGE)

    selected = [c for c in found.candidates if c.selected]
    assert [c.passage.url for c in selected] == [LEASES.url]
    assert all(c.score < KNOWLEDGE.min_reranker_score
               for c in found.candidates if not c.selected)


def test_one_page_contributes_one_passage(embedder, reranker):
    knowledge = Knowledge(
        query_prefix="",
        sources=KNOWLEDGE.sources,
        parent_chars=60,
        parent_overlap=0,
        child_chars=60,
        child_overlap=0,
    )
    publish(
        [_document("readthedocs", "leases", "Leases", "A lease. " * 30)],
        knowledge,
        embedder,
    )

    found = search("a lease", embedder, reranker, knowledge)

    assert len(found.candidates) > 1
    assert sum(1 for c in found.candidates if c.selected) == 1


def test_the_context_holds_the_parent_chunk_not_the_matched_child(embedder, reranker):
    publish([LEASES], KNOWLEDGE, embedder)

    found = search("creating a lease in Blazar", embedder, reranker, KNOWLEDGE)

    assert LEASES.content in found.context
    assert "[readthedocs: Leases]" not in found.context


def test_official_documentation_leads_the_context(embedder, reranker):
    post = _document("blog", "tip", "A lease tip", "Create a lease the quick way.")
    publish([LEASES, post], KNOWLEDGE, embedder)

    found = search("create a lease", embedder, reranker, KNOWLEDGE)

    assert found.context.index("=== PRIMARY DOCUMENTATION (1 passage) ===") == 0
    assert f"[Chameleon Docs: {LEASES.url}]" in found.context
    assert "=== SUPPLEMENTARY CONTEXT (1 passage) ===" in found.context
    assert f"[Chameleon Blog: {post.url}]" in found.context


def test_a_question_the_index_cannot_answer_reads_as_nothing_found(
    embedder, open_base
):
    publish([LEASES], KNOWLEDGE, embedder)

    assert open_base(KNOWLEDGE).search("kubernetes ingress") == NOTHING_FOUND


def test_a_found_question_reads_as_the_context(embedder, open_base):
    publish([LEASES], KNOWLEDGE, embedder)

    assert LEASES.content in open_base(KNOWLEDGE).search("creating a lease in Blazar")


def test_there_is_no_knowledge_base_before_an_index_is_built():
    assert open_knowledge(KNOWLEDGE) is None


def test_a_disabled_knowledge_base_is_not_offered_to_the_agents(embedder):
    publish([LEASES], KNOWLEDGE, embedder)

    assert usable_knowledge(KNOWLEDGE.model_copy(update={"enabled": False})) is None


def test_an_index_built_with_another_model_is_refused(embedder):
    publish([LEASES], KNOWLEDGE, embedder)

    with pytest.raises(KnowledgeError, match="rebuild it with ccx index"):
        open_knowledge(KNOWLEDGE.model_copy(update={"embedding_model": "other-model"}))


def test_a_rebuild_replaces_everything_the_last_one_indexed(embedder, reranker):
    publish([LEASES], KNOWLEDGE, embedder)
    publish([NOTEBOOKS], KNOWLEDGE, embedder)

    found = search("lease Blazar", embedder, reranker, KNOWLEDGE)

    live = store.latest_index()
    assert [c.passage.url for c in found.candidates] == [NOTEBOOKS.url]
    assert live is not None
    assert live.documents == 1


def test_each_source_reports_what_it_contributes(embedder):
    knowledge = Knowledge(
        query_prefix="", sources=KNOWLEDGE.sources,
        parent_chars=60, parent_overlap=0, child_chars=60, child_overlap=0,
    )
    publish(
        [
            _document("readthedocs", "leases", "Leases", "A lease. " * 30),
            NOTEBOOKS,
            _document("blog", "tip", "Tip", "A tip."),
        ],
        knowledge,
        embedder,
    )

    states = {s.source: s for s in store.knowledge_sources()}

    assert states["readthedocs"].documents == 2
    assert states["readthedocs"].chunks > 2
    assert states["blog"].documents == 1
    assert states["blog"].chunks == 1
    assert states["readthedocs"].fetched_at == FETCHED


def test_an_index_of_no_documents_is_refused():
    with pytest.raises(ValueError, match="no documents"):
        store.replace_knowledge([], KNOWLEDGE.embedding_model)


def test_an_unembedded_chunk_is_refused():
    prepared = chunks.split([LEASES], KNOWLEDGE)

    with pytest.raises(ValueError, match="no embedding"):
        store.replace_knowledge(prepared, KNOWLEDGE.embedding_model)


def test_only_as_many_passages_as_configured_are_selected(embedder, reranker):
    knowledge = KNOWLEDGE.model_copy(update={"passages": 2})
    publish(
        [
            _document("readthedocs", f"page-{n}", f"Page {n}", "Create a lease.")
            for n in range(4)
        ],
        knowledge,
        embedder,
    )

    found = search("create a lease", embedder, reranker, knowledge)

    assert all(c.score >= knowledge.min_reranker_score for c in found.candidates)
    assert sum(1 for c in found.candidates if c.selected) == 2


def test_embeddings_of_differing_widths_are_refused(embedder):
    prepared = chunks.split([LEASES, NOTEBOOKS], KNOWLEDGE)
    index.embed(prepared, embedder, KNOWLEDGE)
    prepared[0].parents[0].chunks[0].embedding = [1.0, 0.0]

    with pytest.raises(ValueError, match="one model built these"):
        store.replace_knowledge(prepared, KNOWLEDGE.embedding_model)


def test_the_models_load_once_for_the_life_of_the_process(
    monkeypatch, embedder, reranker
):
    built: list[str] = []

    def build_embedder(_knowledge):
        built.append("embedder")
        return embedder

    def build_reranker(_knowledge):
        built.append("reranker")
        return reranker

    monkeypatch.setattr(retrieve, "_LOADED", {})
    monkeypatch.setattr(retrieve, "BgeEmbedder", build_embedder)
    monkeypatch.setattr(retrieve, "BgeReranker", build_reranker)

    retrieve.Retriever(KNOWLEDGE).retrieve("one question")
    retrieve.Retriever(KNOWLEDGE).retrieve("another question")

    assert built == ["embedder", "reranker"]
