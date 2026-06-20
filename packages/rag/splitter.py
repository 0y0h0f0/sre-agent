"""Markdown-aware runbook chunking."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from packages.rag.metadata import RunbookDocument

TOKEN_RE = re.compile(r"\S+")
H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")


@dataclass(frozen=True)
class RunbookChunkDraft:
    """In-memory chunk ready for embedding and repository insertion."""

    document_id: str
    source_path: str
    title: str
    parent_title: str
    content: str
    content_hash: str
    metadata: dict[str, object]


@dataclass(frozen=True)
class _Section:
    title: str
    content: str


def estimate_tokens(text: str) -> int:
    """Use the shared deterministic token estimate for chunk sizing."""
    from packages.memory.token_counter import TokenCounter
    return TokenCounter().count_tokens(text)


def split_markdown_document(
    document: RunbookDocument,
    *,
    target_tokens: int = 450,
    max_tokens: int = 900,
    overlap_tokens: int = 80,
) -> list[RunbookChunkDraft]:
    """Split one parsed Markdown runbook into stable chunk drafts.

    H2 sections are the primary boundary because runbooks usually group triage,
    checks, and remediation steps there. Oversized sections are split further
    with overlap so retrieval can still match context across paragraph edges.
    """
    if target_tokens <= 0 or max_tokens <= 0:
        msg = "target_tokens and max_tokens must be positive"
        raise ValueError(msg)
    if target_tokens > max_tokens:
        msg = "target_tokens must be <= max_tokens"
        raise ValueError(msg)
    if overlap_tokens < 0:
        msg = "overlap_tokens must be >= 0"
        raise ValueError(msg)

    sections = _split_h2_sections(document.body, fallback_title=document.title)
    drafts: list[RunbookChunkDraft] = []
    for section in sections:
        normalized = _normalize_content(section.content)
        if not normalized:
            continue
        if estimate_tokens(normalized) <= max_tokens:
            drafts.append(_draft(document, section.title, document.title, normalized, len(drafts)))
            continue
        # Cap overlap to one third of the chunk to avoid duplicated context
        # dominating the stored token count for very small max_tokens values.
        for part in _split_long_section(
            section,
            max_tokens=max_tokens,
            overlap_tokens=min(overlap_tokens, max_tokens // 3),
        ):
            drafts.append(_draft(document, part.title, document.title, part.content, len(drafts)))
    return drafts


def _split_h2_sections(body: str, *, fallback_title: str) -> list[_Section]:
    """Split markdown at H2 headings, preserving each heading line in content."""
    sections: list[_Section] = []
    current_title = fallback_title
    current_lines: list[str] = []

    for line in body.splitlines():
        match = H2_RE.match(line.strip())
        if match is not None and current_lines:
            sections.append(_Section(title=current_title, content="\n".join(current_lines)))
            current_lines = []
        if match is not None:
            current_title = match.group("title").strip()
        current_lines.append(line)

    if current_lines:
        sections.append(_Section(title=current_title, content="\n".join(current_lines)))
    return sections


def _split_long_section(
    section: _Section, *, max_tokens: int, overlap_tokens: int
) -> list[_Section]:
    """Split a large section by paragraph while carrying a tail overlap."""
    lines = section.content.splitlines()
    heading_lines: list[str] = []
    body_lines = lines
    if lines and HEADING_RE.match(lines[0].strip()):
        heading_lines = [lines[0]]
        body_lines = lines[1:]

    paragraphs = _paragraphs("\n".join(body_lines))
    chunks: list[_Section] = []
    current: list[str] = []
    current_tokens = estimate_tokens("\n\n".join(heading_lines))

    for paragraph in paragraphs:
        paragraph_tokens = estimate_tokens(paragraph)
        if current and current_tokens + paragraph_tokens > max_tokens:
            chunks.append(
                _Section(
                    title=_part_title(section.title, len(chunks)),
                    content=_normalize_content("\n\n".join([*heading_lines, *current])),
                )
            )
            # Carry only the tail tokens, not the whole previous chunk. This
            # keeps neighboring chunks connected without duplicating large logs.
            current = [_tail_tokens("\n\n".join(current), overlap_tokens)] if overlap_tokens else []
            current = [item for item in current if item]
            current_tokens = estimate_tokens("\n\n".join([*heading_lines, *current]))
        current.append(paragraph)
        current_tokens += paragraph_tokens

    if current:
        chunks.append(
            _Section(
                title=_part_title(section.title, len(chunks)),
                content=_normalize_content("\n\n".join([*heading_lines, *current])),
            )
        )
    return chunks


def _paragraphs(text: str) -> list[str]:
    """Return non-empty markdown paragraphs."""
    return [part.strip() for part in re.split(r"\n\s*\n", text.strip()) if part.strip()]


def _tail_tokens(text: str, count: int) -> str:
    """Return the last ``count`` whitespace tokens for chunk overlap."""
    tokens = TOKEN_RE.findall(text)
    return " ".join(tokens[-count:])


def _part_title(title: str, index: int) -> str:
    return title if index == 0 else f"{title} part {index + 1}"


def _draft(
    document: RunbookDocument,
    title: str,
    parent_title: str,
    content: str,
    chunk_index: int,
) -> RunbookChunkDraft:
    """Build a chunk draft with stable metadata and content hash."""
    metadata = document.metadata.storage_dict()
    token_count = estimate_tokens(content)
    content_hash = _content_hash(document.source_path, title, content)
    metadata.update(
        {
            "document_title": document.title,
            "parent_title": parent_title,
            "chunk_index": chunk_index,
            "token_count": token_count,
            "content_hash": content_hash,
        }
    )
    return RunbookChunkDraft(
        document_id=document.document_id,
        source_path=document.source_path,
        title=title,
        parent_title=parent_title,
        content=content,
        content_hash=content_hash,
        metadata=metadata,
    )


def _content_hash(source_path: str, title: str, content: str) -> str:
    """Hash source/title/content so changed runbook text gets a new identity."""
    normalized = f"{source_path}\n{title}\n{_normalize_content(content)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _normalize_content(text: str) -> str:
    """Trim trailing whitespace while preserving markdown line structure."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines).strip()
