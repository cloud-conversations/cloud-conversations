"""Building the knowledge index."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from cloud_conversations.knowledge import sources
from cloud_conversations.knowledge.embed import Embedder
from cloud_conversations.knowledge.errors import KnowledgeError
from cloud_conversations.schema.knowledge import Document, Knowledge, Source

log = logging.getLogger(__name__)


class Collected(BaseModel):
    """The pages of one source, and where they came from."""

    origin: Literal["fetched", "cache"]
    documents: list[Document]
    # What a failed fetch reported.
    error: str | None = None


def collect(
    name: str, source: Source, knowledge: Knowledge, *, refresh: bool
) -> Collected:
    """One source's pages, fetched and cached or read back from the cache."""
    path = _cache_path(name, knowledge)
    if not refresh:
        return Collected(origin="cache", documents=_load(path))
    try:
        documents = sources.fetch(name, source, knowledge)
    except KnowledgeError as exc:
        return Collected(
            origin="cache", documents=_load(path), error=str(exc)
        )
    _save(path, documents)
    return Collected(origin="fetched", documents=documents)


def embed(
    documents: list[Document], embedder: Embedder, knowledge: Knowledge
) -> int:
    """Embed every child chunk in place, and return the width of the vectors."""
    chunks = [c for d in documents for p in d.parents for c in p.chunks]
    if not chunks:
        raise KnowledgeError("these documents split into no chunks")

    for start in range(0, len(chunks), knowledge.embed_batch):
        batch = chunks[start : start + knowledge.embed_batch]
        vectors = embedder.embed_documents([c.content for c in batch])
        for chunk, vector in zip(batch, vectors, strict=True):
            chunk.embedding = vector
        log.info("embedded %d/%d chunks", start + len(batch), len(chunks))

    return len(chunks[0].embedding)


def _cache_path(name: str, knowledge: Knowledge) -> Path:
    return Path(knowledge.cache_dir) / f"{name}.json"


def _load(path: Path) -> list[Document]:
    """The cached pages. Empty if there is no usable cache."""
    if not path.exists():
        return []
    try:
        return [Document.model_validate(row) for row in json.loads(path.read_text())]
    except (OSError, ValueError, ValidationError) as exc:
        log.warning("ignoring the cache at %s: %s", path, exc)
        return []


def _save(path: Path, documents: list[Document]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [d.model_dump(mode="json", exclude={"parents"}) for d in documents]
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(rows))
    temp.replace(path)
