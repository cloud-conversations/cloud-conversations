"""The embedding model and the reranker."""

from __future__ import annotations

from cloud_conversations.knowledge import embed
from cloud_conversations.knowledge.embed import BgeEmbedder, BgeReranker
from cloud_conversations.schema.knowledge import BGE_QUERY_INSTRUCTION, Knowledge


class _Encoder:
    def __init__(self) -> None:
        self.encoded: list[list[str]] = []
        self.kwargs: list[dict] = []

    def encode(self, texts, **kwargs):
        self.encoded.append(list(texts))
        self.kwargs.append(kwargs)
        return [[float(len(text))] for text in texts]


class _Scorer:
    def __init__(self) -> None:
        self.pairs: list[list[tuple[str, str]]] = []

    def predict(self, pairs):
        self.pairs.append(list(pairs))
        return [0.25] * len(self.pairs[-1])


def _embedder(monkeypatch, knowledge: Knowledge | None = None):
    encoder = _Encoder()
    monkeypatch.setattr(embed, "_sentence_transformer", lambda model, device: encoder)
    return BgeEmbedder(knowledge or Knowledge()), encoder


def _reranker(monkeypatch):
    scorer = _Scorer()
    monkeypatch.setattr(
        embed, "_cross_encoder", lambda model, max_tokens, device: scorer
    )
    return BgeReranker(Knowledge()), scorer


def test_a_query_carries_the_instruction_and_no_newlines(monkeypatch):
    embedder, encoder = _embedder(monkeypatch)

    embedder.embed_query("what is\na lease")

    assert encoder.encoded == [
        [f"{BGE_QUERY_INSTRUCTION}what is a lease"]
    ]


def test_a_document_is_embedded_without_the_instruction(monkeypatch):
    embedder, encoder = _embedder(monkeypatch)

    embedder.embed_documents(["one\ntwo", "three"])

    assert encoder.encoded == [["one two", "three"]]


def test_embeddings_are_normalised_and_batched_as_configured(monkeypatch):
    embedder, encoder = _embedder(monkeypatch, Knowledge(embed_batch=8))

    embedder.embed_documents(["one"])

    assert encoder.kwargs == [{"batch_size": 8, "normalize_embeddings": True}]


def test_the_reranker_pairs_the_query_with_every_candidate(monkeypatch):
    reranker, scorer = _reranker(monkeypatch)

    scores = reranker.score("a lease", ["about leases", "about images"])

    assert scorer.pairs == [[("a lease", "about leases"), ("a lease", "about images")]]
    assert scores == [0.25, 0.25]


def test_the_reranker_is_not_asked_to_score_nothing(monkeypatch):
    reranker, scorer = _reranker(monkeypatch)

    assert reranker.score("a lease", []) == []
    assert scorer.pairs == []
