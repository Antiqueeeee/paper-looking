"""Shared enums, dataclasses and protocols.

This module is part of the Wave 0 contract. Other agents must import status
values from here instead of using raw strings, so a typo fails loudly at
import time rather than corrupting data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Mapping, Protocol


class SourceName(str, Enum):
    ACL = "acl"
    OPENALEX = "openalex"
    CROSSREF = "crossref"
    ARXIV = "arxiv"
    MANUAL = "manual"


class PaperStatus(str, Enum):
    NEW = "new"
    IN_QUEUE = "in_queue"
    READING = "reading"
    DONE = "done"
    LATER = "later"


class PdfStatus(str, Enum):
    NONE = "none"
    NEEDS_UPLOAD = "needs_upload"
    DOWNLOADING = "downloading"
    DOWNLOADED = "downloaded"
    DOWNLOAD_FAILED = "download_failed"
    COLD = "cold"


class ParseStatus(str, Enum):
    NONE = "none"
    QUEUED = "queued"
    UPLOADING = "uploading"
    PARSING = "parsing"
    DOWNLOADING = "downloading"
    DONE = "done"
    FAILED = "failed"


class TranslateStatus(str, Enum):
    NONE = "none"
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class TaskType(str, Enum):
    DOWNLOAD_PDF = "download_pdf"
    PARSE_PDF = "parse_pdf"
    TRANSLATE_META = "translate_meta"
    TRANSLATE_FULL = "translate_full"


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FetchStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class PaperDraft:
    """Normalized paper record produced by a data source.

    `id` is the canonical deduplication key:
      - ACL Anthology: anthology id, e.g. `2026.findings-acl.38`
      - OpenAlex / Crossref: DOI (lower-cased)
      - arXiv: `arxiv:{paper_id}` (versionless)
      - manual upload: generated `manual:{sha256[:16]}`
    """

    id: str
    source: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    year: int | None = None
    venue: str = ""
    url: str = ""
    pdf_url: str = ""
    doi: str = ""
    tags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_paper_row(self) -> dict[str, Any]:
        """Convert to the dict shape stored in the `papers` table."""
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "authors": self.authors,
            "abstract": self.abstract,
            "year": self.year,
            "venue": self.venue,
            "url": self.url,
            "pdf_url": self.pdf_url,
            "doi": self.doi,
            "tags": self.tags,
            "extra": self.extra,
        }


@dataclass
class SourceState:
    """Persisted fetch checkpoint for one data source."""

    name: str
    last_success_at: str | None = None
    cursor: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""


class PaperSource(Protocol):
    """Every collector implements this protocol."""

    name: str

    def fetch_incremental(self, since: str, state: SourceState) -> Iterator[PaperDraft]:
        """Yield normalized paper drafts newer than `since`.

        Implementations must be resumable and must not raise for a single
        malformed paper; per-record errors are logged or attached to the
        draft's `extra` field.
        """
        ...


@dataclass
class LLMMessage:
    role: str
    content: str
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMResponse:
    content: str
    role: str = "assistant"
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: LLMUsage = field(default_factory=LLMUsage)
    raw: Mapping[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    """All LLM traffic must go through an implementation of this protocol."""

    def chat(
        self,
        messages: list[LLMMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        budget_tag: str = "default",
    ) -> LLMResponse:
        """Send one chat completion request and return a normalized response."""
        ...


class ObjectStore(Protocol):
    """Cold storage for PDF files and database backups."""

    def put(self, key: str, local_path: str) -> None: ...
    def get(self, key: str, local_path: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
