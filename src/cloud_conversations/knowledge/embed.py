"""The models that turn text into vectors and score a query against a candidate."""

from __future__ import annotations

from typing import Any, Protocol

from cloud_conversations.knowledge.errors import MISSING_EXTRA, KnowledgeError
from cloud_conversations.schema.knowledge import Knowledge


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class BgeEmbedder:
    """A bge sentence-transformer."""

    def __init__(self, knowledge: Knowledge) -> None:
        self._model = _sentence_transformer(
            knowledge.embedding_model, knowledge.embedding_device
        )
        self._instruction = knowledge.query_instruction
        self._batch = knowledge.embed_batch

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._encode([_oneline(text) for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._encode([self._instruction + _oneline(text)])[0]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        rows = self._model.encode(
            texts, batch_size=self._batch, normalize_embeddings=True
        )
        return [[float(value) for value in row] for row in rows]


class BgeReranker:
    """A cross-encoder that scores a query against each candidate, 0 to 1."""

    def __init__(self, knowledge: Knowledge) -> None:
        self._model = _cross_encoder(
            knowledge.reranker_model,
            knowledge.reranker_max_tokens,
            knowledge.reranker_device,
        )

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in self._model.predict([(query, t) for t in texts])]


def _oneline(text: str) -> str:
    return text.replace("\n", " ")


def _sentence_transformer(model: str, device: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise KnowledgeError(MISSING_EXTRA) from exc
    return SentenceTransformer(model, device=device)


def _cross_encoder(model: str, max_tokens: int, device: str) -> Any:
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise KnowledgeError(MISSING_EXTRA) from exc
    return CrossEncoder(model, max_length=max_tokens, device=device)
