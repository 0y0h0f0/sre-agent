"""Runbook ingestion pipeline and CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import BaseModel, Field

from packages.common.ids import new_id
from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.db.session import SessionLocal
from packages.rag.embeddings import FakeEmbedding
from packages.rag.metadata import RunbookMetadataError, parse_runbook_markdown
from packages.rag.splitter import split_markdown_document


class RunbookIngestResult(BaseModel):
    path: str
    files_scanned: int = 0
    chunks_created: int = 0
    chunks_skipped: int = 0
    chunks_total: int = 0
    errors: list[str] = Field(default_factory=list)


class RunbookIngestor:
    def __init__(
        self,
        repository: RunbookChunkRepository,
        *,
        embedding_provider: FakeEmbedding | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_provider = embedding_provider or FakeEmbedding()

    def ingest_path(self, path: str | Path, *, reingest: bool = True) -> RunbookIngestResult:
        base_path = Path(path)
        if not base_path.exists():
            raise FileNotFoundError(str(base_path))

        files = _markdown_files(base_path)
        result = RunbookIngestResult(path=base_path.as_posix(), files_scanned=len(files))
        for markdown_path in files:
            source_path = markdown_path.as_posix()
            text = markdown_path.read_text(encoding="utf-8")
            document = parse_runbook_markdown(text, source_path=source_path)
            drafts = split_markdown_document(document)
            if not reingest and self.repository.document_has_chunks(document.document_id):
                result.chunks_skipped += len(drafts)
                continue

            for draft in drafts:
                if self.repository.get_by_content_hash(draft.content_hash) is not None:
                    result.chunks_skipped += 1
                    continue
                self.repository.create_chunk(
                    chunk_id=new_id("chk_"),
                    document_id=draft.document_id,
                    source_path=draft.source_path,
                    title=draft.title,
                    content=draft.content,
                    content_hash=draft.content_hash,
                    embedding=self.embedding_provider.embed_text(f"{draft.title}\n{draft.content}"),
                    embedding_model=self.embedding_provider.model_name,
                    metadata=dict(draft.metadata),
                )
                result.chunks_created += 1
        result.chunks_total = self.repository.count_chunks()
        return result


def _markdown_files(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() != ".md":
            raise RunbookMetadataError(f"{path.as_posix()}: expected a Markdown file")
        return [path]
    return sorted(item for item in path.rglob("*.md") if item.is_file())


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest local Markdown runbooks")
    parser.add_argument("--path", default="demo/runbooks")
    parser.add_argument("--no-reingest", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as session:
        result = RunbookIngestor(RunbookChunkRepository(session)).ingest_path(
            args.path,
            reingest=not args.no_reingest,
        )
        session.commit()
        print(result.model_dump_json())


if __name__ == "__main__":
    main()
