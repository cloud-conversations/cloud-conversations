"""Splitting a page into parent chunks for context and child chunks for embedding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_conversations.knowledge.errors import MISSING_EXTRA, KnowledgeError
from cloud_conversations.schema.knowledge import Chunk, Document, Knowledge, Parent

if TYPE_CHECKING:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

_PARENT_SEPARATORS = ["\n## ", "\n### ", "\n#### ", "\n", " ", ""]
_CHILD_SEPARATORS = ["\n### ", "\n#### ", "\n", " ", ""]


def split(documents: list[Document], knowledge: Knowledge) -> list[Document]:
    """Split every page into parent chunks, and each parent into child chunks."""
    parents = _splitter(knowledge.parent_chars, knowledge.parent_overlap,
                        _PARENT_SEPARATORS)
    children = _splitter(knowledge.child_chars, knowledge.child_overlap,
                         _CHILD_SEPARATORS)
    return [
        document.model_copy(
            update={"parents": _parents(document, parents, children)}
        )
        for document in documents
    ]


def _parents(
    document: Document,
    parents: RecursiveCharacterTextSplitter,
    children: RecursiveCharacterTextSplitter,
) -> list[Parent]:
    header = _header(document)
    return [
        Parent(
            content=parent,
            chunks=[
                Chunk(content=header + child) for child in children.split_text(parent)
            ],
        )
        for parent in parents.split_text(document.content)
    ]


def _header(document: Document) -> str:
    """The line in front of each child chunk's text."""
    if document.title:
        return f"[{document.source}: {document.title}]\n"
    return f"[{document.source}]\n"


def _splitter(
    chars: int, overlap: int, separators: list[str]
) -> RecursiveCharacterTextSplitter:
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise KnowledgeError(MISSING_EXTRA) from exc
    return RecursiveCharacterTextSplitter(
        chunk_size=chars, chunk_overlap=overlap, separators=separators
    )
