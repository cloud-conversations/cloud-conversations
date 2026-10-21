"""Knowledge sources, and the contents of the knowledge base."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# The instruction a bge model expects in front of a query.
BGE_QUERY_INSTRUCTION = "Represent this question for searching relevant passages: "


class SourceBase(BaseModel):
    """The fields every knowledge source carries."""

    model_config = {"extra": "forbid", "frozen": True}

    label: str
    # Passages from a primary source go to the model first.
    primary: bool = False
    min_chars: int = Field(default=0, ge=0)


class SphinxSource(SourceBase):
    """A Sphinx site, enumerated from one page of links."""

    kind: Literal["sphinx"]
    url: str
    index: str = "contents.html"
    exclude: list[str] = []


class BlogSource(SourceBase):
    """A paginated blog listing, narrowed to some of its categories."""

    kind: Literal["blog"]
    url: str
    categories: list[str] = []


Source = Annotated[SphinxSource | BlogSource, Field(discriminator="kind")]


class Knowledge(BaseModel):
    """Settings for building and searching the knowledge base."""

    model_config = {"extra": "forbid", "frozen": True}

    enabled: bool = True
    embedding_model: str = "BAAI/bge-large-en-v1.5"
    embedding_device: str = "cpu"
    embed_batch: int = Field(default=32, ge=1)
    query_instruction: str = BGE_QUERY_INSTRUCTION
    query_prefix: str = ""
    reranker_model: str = "BAAI/bge-reranker-base"
    reranker_device: str = "cpu"
    reranker_max_tokens: int = Field(default=512, ge=1)
    min_reranker_score: float = 0.5

    parent_chars: int = Field(default=2000, ge=1)
    parent_overlap: int = Field(default=200, ge=0)
    child_chars: int = Field(default=400, ge=1)
    child_overlap: int = Field(default=50, ge=0)

    fetch_candidates: int = Field(default=50, ge=1)
    pool_candidates: int = Field(default=20, ge=1)
    passages: int = Field(default=6, ge=1)
    max_history_exchanges: int = Field(default=3, ge=0)

    # Deployment policy, appended to the docs assistant's instructions.
    answer_policy: str = ""

    fetch_timeout_s: float = Field(default=15.0, gt=0)
    fetch_attempts: int = Field(default=3, ge=1)
    fetch_retry_s: float = Field(default=2.0, ge=0)
    cache_dir: str = "var/knowledge"

    sources: dict[str, Source] = {}

    @model_validator(mode="after")
    def _overlap_fits_the_chunk(self) -> Knowledge:
        for name, chars, overlap in (
            ("parent", self.parent_chars, self.parent_overlap),
            ("child", self.child_chars, self.child_overlap),
        ):
            if overlap >= chars:
                raise ValueError(
                    f"{name}_overlap {overlap} is not smaller than "
                    f"{name}_chars {chars}"
                )
        return self


class Chunk(BaseModel):
    """A child chunk: the text that is embedded and searched."""

    content: str
    embedding: list[float] = []


class Parent(BaseModel):
    """A parent chunk: the text passed to the model on a match."""

    content: str
    chunks: list[Chunk] = []


class Document(BaseModel):
    """One fetched page, and the chunks it was split into."""

    source: str
    url: str
    title: str
    content: str
    fetched_at: datetime
    parents: list[Parent] = []


class Passage(BaseModel):
    """A piece of a document the knowledge base returned."""

    parent_id: uuid.UUID
    source: str
    url: str
    content: str


class Index(BaseModel):
    """What the live knowledge index was built from."""

    build_id: uuid.UUID
    embedding_model: str
    dimensions: int
    documents: int
    parents: int
    chunks: int
    built_at: datetime


class SourceState(BaseModel):
    """What one source contributes to the live index."""

    source: str
    documents: int
    chunks: int
    fetched_at: datetime
