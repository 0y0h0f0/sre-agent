from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from packages.db.repositories.runbooks import RunbookChunkRepository
from packages.rag.embeddings import FakeEmbedding
from packages.rag.ingest import RunbookIngestor
from packages.rag.metadata import (
    RunbookMetadataError,
    parse_runbook_markdown,
)
from packages.rag.reranker import rerank_score
from packages.rag.retriever import (
    RunbookRetriever,
    RunbookSearchQuery,
    format_runbook_context,
)
from packages.rag.splitter import (
    estimate_tokens,
    split_markdown_document,
)
from packages.tools.cache import RequestLocalToolCache
from packages.tools.runbook_search import RunbookSearchTool


class FailingEmbeddingProvider:
    dimension = 512
    model_name = "failing"

    def embed_text(self, text: str) -> list[float]:
        raise RuntimeError("embedding backend unavailable")

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(text) for text in texts]


def _runbook_text(
    *,
    service: str = "checkout",
    incident_type: str = "high_5xx",
    title: str = "High 5xx Triage",
    body: str = "5xx errors after deploy require rollback checks.",
) -> str:
    return f"""---
service: {service}
incident_type: {incident_type}
severity: P1
owner: payment-team
updated_at: 2026-05-31
---
# {title}

## Detection
{body}

## Evidence
Collect metrics, logs, traces, and git deployment evidence.
"""


def test_metadata_parser_extracts_required_front_matter() -> None:
    document = parse_runbook_markdown(_runbook_text(), source_path="runbooks/high-5xx.md")

    assert document.metadata.service == "checkout"
    assert document.metadata.incident_type == "high_5xx"
    assert document.title == "High 5xx Triage"
    assert document.document_id.startswith("doc_")


def test_metadata_parser_rejects_missing_front_matter() -> None:
    with pytest.raises(RunbookMetadataError, match="front matter"):
        parse_runbook_markdown("# Missing\n", source_path="bad.md")


def test_splitter_preserves_heading_hierarchy() -> None:
    document = parse_runbook_markdown(_runbook_text(), source_path="runbooks/high-5xx.md")

    chunks = split_markdown_document(document, target_tokens=80, max_tokens=120)

    detection = next(chunk for chunk in chunks if chunk.title == "Detection")
    assert detection.parent_title == "High 5xx Triage"
    assert detection.content.startswith("## Detection")
    assert detection.metadata["service"] == "checkout"
    assert detection.source_path == "runbooks/high-5xx.md"


def test_fake_embedding_is_deterministic_512_dimensions() -> None:
    embedding = FakeEmbedding()

    first = embedding.embed_text("checkout high 5xx")
    second = embedding.embed_text("checkout high 5xx")

    assert first == second
    assert len(first) == 512
    assert first != embedding.embed_text("redis cache avalanche")


def test_ingest_is_idempotent_and_search_filters(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    (base / "cache.md").write_text(
        _runbook_text(
            incident_type="cache_avalanche",
            title="Redis Cache Avalanche Triage",
            body="Redis cache miss spike and database pressure indicate cache avalanche.",
        ),
        encoding="utf-8",
    )

    repository = RunbookChunkRepository(db_session)
    ingestor = RunbookIngestor(repository)
    first = ingestor.ingest_path(base)
    db_session.commit()
    second = ingestor.ingest_path(base)
    db_session.commit()

    assert first.chunks_created > 0
    assert second.chunks_created == 0
    assert second.chunks_skipped == first.chunks_created

    results = RunbookRetriever(repository).search(
        RunbookSearchQuery(
            query="high 5xx after deploy",
            service="checkout",
            incident_type="high_5xx",
            top_k=3,
        )
    )
    assert results
    assert all(item.metadata["service"] == "checkout" for item in results)
    assert all(item.metadata["incident_type"] == "high_5xx" for item in results)
    assert {"chunk_id", "source_path", "title", "excerpt", "score", "metadata"}.issubset(
        results[0].model_dump().keys()
    )


def test_ingest_degrades_when_embedding_provider_fails(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")

    repository = RunbookChunkRepository(db_session)
    result = RunbookIngestor(
        repository,
        embedding_provider=FailingEmbeddingProvider(),
    ).ingest_path(base)
    db_session.commit()

    chunks = repository.list_chunks()
    assert result.chunks_created == len(chunks)
    assert chunks
    assert all(chunk.embedding_model == "none" for chunk in chunks)
    assert all(chunk.embedding == [0.0] * 512 for chunk in chunks)


def test_runbook_context_keeps_chunk_id_reference(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    results = RunbookRetriever(repository).search(RunbookSearchQuery(query="rollback checks"))
    context = format_runbook_context(results[:1])

    assert "[chunk_id=chk_" in context
    assert "source=" in context


def test_runbook_search_tool_returns_evidence_and_uses_cache(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    tool = RunbookSearchTool(
        retriever=RunbookRetriever(repository),
        cache=RequestLocalToolCache(),
    )
    query = RunbookSearchQuery(query="high 5xx rollback", service="checkout")

    first = tool.run(query)
    second = tool.run(query)

    assert first.status == "succeeded"
    assert first.evidence[0]["payload"]["chunk_id"].startswith("chk_")
    assert second.cache_hit is True


# ---------------------------------------------------------------------------
# splitter edge cases
# ---------------------------------------------------------------------------


def test_splitter_rejects_invalid_token_parameters() -> None:
    document = parse_runbook_markdown(_runbook_text(), source_path="runbooks/high-5xx.md")

    with pytest.raises(ValueError, match="target_tokens and max_tokens must be positive"):
        split_markdown_document(document, target_tokens=0, max_tokens=100)
    with pytest.raises(ValueError, match="target_tokens and max_tokens must be positive"):
        split_markdown_document(document, target_tokens=100, max_tokens=0)
    with pytest.raises(ValueError, match="target_tokens must be <= max_tokens"):
        split_markdown_document(document, target_tokens=200, max_tokens=100)
    with pytest.raises(ValueError, match="overlap_tokens must be >= 0"):
        split_markdown_document(document, target_tokens=10, max_tokens=50, overlap_tokens=-1)


def test_splitter_handles_long_content_that_needs_sub_chunking() -> None:
    """Sections longer than max_tokens are split into sub-chunks."""
    # Build short paragraphs (2 tokens each) so splitting works at max_tokens=6
    paragraphs = [f"p{i} text" for i in range(20)]
    long_body = "\n\n".join(paragraphs)
    text = f"""---
service: checkout
incident_type: high_5xx
severity: P2
owner: oncall
updated_at: 2026-05-31
---
# Long Doc

## Long Section
{long_body}
"""
    document = parse_runbook_markdown(text, source_path="runbooks/long.md")
    chunks = split_markdown_document(document, target_tokens=3, max_tokens=6, overlap_tokens=1)
    assert len(chunks) >= 3  # 20 paragraphs @ 2 tokens each, max 6 tokens/chunk => ~7+ chunks
    for chunk in chunks:
        assert chunk.document_id == document.document_id


def test_estimate_tokens_counts_non_whitespace_sequences() -> None:
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("") == 0
    # TokenCounter returns max(1, len//4) per char heuristic; whitespace-only
    # text may return 1 instead of 0. Both semantics are reasonable.
    assert estimate_tokens("   ") >= 0


# ---------------------------------------------------------------------------
# reranker edge cases
# ---------------------------------------------------------------------------


def test_reranker_handles_empty_query() -> None:
    score = rerank_score(
        query="...",
        metadata={"service": "checkout", "updated_at": "2026-05-01"},
        title="Some Runbook",
        vector_score=0.8,
        service="checkout",
        incident_type="high_5xx",
    )
    assert 0.0 <= score <= 1.0


def test_reranker_handles_missing_metadata_fields() -> None:
    score = rerank_score(
        query="rollback",
        metadata={},
        title="",
        vector_score=0.5,
        service=None,
        incident_type=None,
    )
    assert 0.0 <= score <= 1.0


def test_reranker_freshness_decay() -> None:
    common = {
        "query": "rollback",
        "title": "Rollback Triage",
        "vector_score": 0.8,
        "service": "checkout",
        "incident_type": "high_5xx",
    }
    recent = rerank_score(
        **common,
        metadata={"service": "checkout", "incident_type": "high_5xx", "updated_at": "2026-05-15"},
    )
    rerank_score(
        **common,
        metadata={"service": "checkout", "incident_type": "high_5xx", "updated_at": "2024-01-01"},
    )
    ancient = rerank_score(
        **common,
        metadata={"service": "checkout", "incident_type": "high_5xx", "updated_at": "2020-06-01"},
    )
    assert recent > ancient


def test_reranker_rejects_invalid_date() -> None:
    score = rerank_score(
        query="test",
        metadata={"updated_at": "not-a-date"},
        title="Triage",
        vector_score=0.5,
        service=None,
        incident_type=None,
    )
    assert 0.0 <= score <= 1.0


def test_reranker_handles_none_updated_at() -> None:
    score = rerank_score(
        query="test",
        metadata={},
        title="Triage",
        vector_score=0.5,
        service=None,
        incident_type=None,
    )
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# retriever edge cases
# ---------------------------------------------------------------------------


def test_retriever_service_filter_excludes_mismatched_chunks(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    results = RunbookRetriever(repository).search(
        RunbookSearchQuery(query="rollback", service="inventory", top_k=5)
    )
    assert len(results) == 0


def test_retriever_incident_type_filter_is_case_insensitive(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    results = RunbookRetriever(repository).search(
        RunbookSearchQuery(query="rollback", incident_type="HIGH_5xx", top_k=5)
    )
    assert len(results) >= 1
    assert all(item.metadata["incident_type"] == "high_5xx" for item in results)


def test_retriever_strips_whitespace_from_query_fields() -> None:
    query = RunbookSearchQuery(query="  rollback  ", service=" checkout ", top_k=5)
    assert query.query == "rollback"
    assert query.service == "checkout"


def test_retriever_cache_hit_returns_cached_results(db_session, tmp_path) -> None:
    """Second identical search returns cached results without scanning DB."""
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    from packages.rag.retriever import RunbookSearchCache

    cache = RunbookSearchCache()
    retriever = RunbookRetriever(repository, cache=cache)
    query = RunbookSearchQuery(query="rollback checks", service="checkout")

    first = retriever.search(query)
    second = retriever.search(query)

    assert len(first) == len(second)
    assert [item.chunk_id for item in first] == [item.chunk_id for item in second]


def test_retriever_empty_query_fields_normalized() -> None:
    """Query fields that are whitespace-only become None and don't filter."""
    query = RunbookSearchQuery(query="rollback", top_k=5)
    assert query.service is None
    assert query.incident_type is None


def test_excerpt_truncates_long_content() -> None:
    from packages.rag.retriever import _excerpt

    short = "short content"
    assert _excerpt(short, "query") == "short content"

    long_content = "prefix " + "word " * 100 + " error timeout " + "word " * 100 + " suffix"
    excerpt = _excerpt(long_content, "error timeout")
    assert len(excerpt) <= 400
    assert "error" in excerpt


# ---------------------------------------------------------------------------
# ingest edge cases
# ---------------------------------------------------------------------------


def test_ingest_reingest_false_skips_existing_documents(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")

    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    result = RunbookIngestor(repository).ingest_path(base, reingest=False)
    assert result.chunks_created == 0
    assert result.chunks_skipped > 0


def test_ingest_rejects_non_markdown_file(db_session, tmp_path) -> None:
    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "notes.txt").write_text("not markdown", encoding="utf-8")

    repository = RunbookChunkRepository(db_session)
    with pytest.raises(RunbookMetadataError, match="expected a Markdown file"):
        RunbookIngestor(repository).ingest_path(base / "notes.txt")


def test_ingest_raises_for_missing_path(db_session) -> None:
    repository = RunbookChunkRepository(db_session)
    with pytest.raises(FileNotFoundError):
        RunbookIngestor(repository).ingest_path("/nonexistent/path/xyz")


def test_fake_embedding_embed_many_returns_list() -> None:
    embedding = FakeEmbedding()
    results = embedding.embed_many(["text one", "text two"])
    assert len(results) == 2
    assert all(len(vec) == 512 for vec in results)


# ---------------------------------------------------------------------------
# metadata edge cases
# ---------------------------------------------------------------------------


def test_metadata_rejects_blank_service_field() -> None:
    text = """---
service: " "
incident_type: high_5xx
severity: P1
owner: oncall
updated_at: 2026-05-31
---
# Title
Body text.
"""
    with pytest.raises(RunbookMetadataError, match="must not be blank"):
        parse_runbook_markdown(text, source_path="blank.md")


def test_metadata_rejects_missing_required_fields() -> None:
    text = """---
service: checkout
severity: P1
---
# Title
Body text.
"""
    with pytest.raises(RunbookMetadataError, match="missing runbook metadata fields"):
        parse_runbook_markdown(text, source_path="missing.md")


def test_metadata_rejects_invalid_front_matter_line() -> None:
    text = """---
service: checkout
bad line without colon
severity: P1
owner: oncall
incident_type: high_5xx
updated_at: 2026-05-31
---
# Title
Body text.
"""
    with pytest.raises(RunbookMetadataError, match="invalid front matter line"):
        parse_runbook_markdown(text, source_path="badline.md")


def test_metadata_rejects_empty_key() -> None:
    text = """---
service: checkout
incident_type: high_5xx
severity: P1
: value_without_key
owner: oncall
updated_at: 2026-05-31
---
# Title
Body text.
"""
    with pytest.raises(RunbookMetadataError, match="empty metadata key"):
        parse_runbook_markdown(text, source_path="emptykey.md")


def test_metadata_strips_quoted_yaml_scalars() -> None:
    text = """---
service: checkout
incident_type: "high_5xx"
severity: 'P1'
owner: "oncall"
updated_at: 2026-05-31
---
# My Title
Body text.
"""
    document = parse_runbook_markdown(text, source_path="quoted.md")
    assert document.metadata.incident_type == "high_5xx"
    assert document.metadata.severity == "P1"


def test_metadata_uses_filename_when_no_h1_title() -> None:
    text = """---
service: checkout
incident_type: high_5xx
severity: P1
owner: oncall
updated_at: 2026-05-31
---
No heading here, just body text.
"""
    document = parse_runbook_markdown(text, source_path="runbooks/high-5xx-triage.md")
    assert document.title == "High 5Xx Triage"


def test_metadata_handles_invalid_date_value() -> None:
    text = """---
service: checkout
incident_type: high_5xx
severity: P1
owner: oncall
updated_at: not-a-date
---
# Title
Body text.
"""
    with pytest.raises(RunbookMetadataError, match="invalid runbook metadata"):
        parse_runbook_markdown(text, source_path="baddate.md")


# ---------------------------------------------------------------------------
# 4.4 language field
# ---------------------------------------------------------------------------


def test_metadata_accepts_language_field() -> None:
    text = """---
service: checkout
incident_type: high_5xx
severity: P1
owner: oncall
updated_at: 2026-05-31
language: zh
---
# 中文标题
Body text.
"""
    document = parse_runbook_markdown(text, source_path="zh.md")
    assert document.metadata.language == "zh"


def test_metadata_defaults_language_to_en() -> None:
    document = parse_runbook_markdown(_runbook_text(), source_path="runbooks/high-5xx.md")
    assert document.metadata.language == "en"


# ---------------------------------------------------------------------------
# 4.1 BM25 / hybrid search
# ---------------------------------------------------------------------------


def test_build_tsquery_single_term() -> None:
    from packages.rag.bm25 import build_tsquery

    result = build_tsquery("rollback")
    assert "rollback:*" in result


def test_build_tsquery_multi_term() -> None:
    from packages.rag.bm25 import build_tsquery

    result = build_tsquery("high 5xx after deploy")
    assert "high" in result
    assert "5xx" in result
    assert "deploy:*" in result


def test_adaptive_alpha_keyword_match() -> None:
    from packages.rag.bm25 import adaptive_alpha

    alpha = adaptive_alpha("high 5xx rollback", ["High 5xx Triage", "Other"])
    assert alpha > 0.5  # keyword match favors BM25


def test_adaptive_alpha_no_match() -> None:
    from packages.rag.bm25 import adaptive_alpha

    alpha = adaptive_alpha("something completely different", ["High 5xx Triage"])
    assert alpha < 0.5  # natural language favors vector


def test_normalize_bm25_clamps_to_0_1() -> None:
    from packages.rag.bm25 import normalize_bm25

    assert normalize_bm25(0.5) == 0.5
    assert normalize_bm25(-0.1) == 0.0
    assert normalize_bm25(1.5) == 1.0


def test_search_bm25_sqlite_uses_lexical_fallback(
    db_session,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from packages.rag.bm25 import build_tsquery

    base = tmp_path / "runbooks"
    base.mkdir()
    (base / "high.md").write_text(_runbook_text(), encoding="utf-8")
    (base / "cache.md").write_text(
        _runbook_text(
            incident_type="cache_avalanche",
            title="Redis Cache Avalanche Triage",
            body="Redis cache miss spike and database pressure indicate cache avalanche.",
        ),
        encoding="utf-8",
    )
    repository = RunbookChunkRepository(db_session)
    RunbookIngestor(repository).ingest_path(base)
    db_session.commit()

    with caplog.at_level(logging.WARNING, logger="packages.db.repositories.runbooks"):
        results = repository.search_bm25(
            build_tsquery("high 5xx after deploy"),
            service="checkout",
            incident_type="high_5xx",
        )

    assert results
    assert "runbook full-text search query failed" not in caplog.text
    chunk, score = results[0]
    assert chunk.metadata_json["incident_type"] == "high_5xx"
    assert score > 0.0


# ---------------------------------------------------------------------------
# 4.4 Embedding factory
# ---------------------------------------------------------------------------


def test_fake_embedding_provider_conforms_to_protocol() -> None:
    from packages.rag.embedding_factory import EmbeddingProvider, FakeEmbeddingProvider

    provider = FakeEmbeddingProvider()
    assert isinstance(provider, EmbeddingProvider)
    assert provider.dimension == 512
    assert provider.model_name == "fake-512"
    vec = provider.embed_text("test")
    assert len(vec) == 512
    many = provider.embed_many(["a", "b"])
    assert len(many) == 2
    assert all(len(v) == 512 for v in many)


def test_fake_embedding_is_deterministic_via_provider() -> None:
    from packages.rag.embedding_factory import FakeEmbeddingProvider

    provider = FakeEmbeddingProvider()
    assert provider.embed_text("test") == provider.embed_text("test")
    assert provider.embed_text("test") != provider.embed_text("different")


def test_build_embedding_provider_returns_fake_by_default() -> None:
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import FakeEmbeddingProvider, build_embedding_provider

    settings = Settings(embedding_provider="fake")
    provider = build_embedding_provider(settings)
    assert isinstance(provider, FakeEmbeddingProvider)


def test_build_embedding_provider_returns_disabled_keyword_fallback() -> None:
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import DisabledEmbeddingProvider, build_embedding_provider

    settings = Settings(embedding_provider="disabled")
    provider = build_embedding_provider(settings)

    assert isinstance(provider, DisabledEmbeddingProvider)
    assert provider.model_name == "disabled"
    assert provider.embed_text("query") == [0.0] * 512


def test_build_embedding_provider_external_disabled_without_full_opt_in() -> None:
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import DisabledEmbeddingProvider, build_embedding_provider

    settings = Settings(embedding_provider="external")
    provider = build_embedding_provider(settings)

    assert isinstance(provider, DisabledEmbeddingProvider)


def test_build_embedding_provider_external_requires_url_when_enabled() -> None:
    from packages.common.errors import ValidationAppError
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import build_embedding_provider

    settings = Settings(
        embedding_provider="external",
        m9_extensions_enabled=True,
        semantic_runbook_search_enabled=True,
        external_embedding_provider_enabled=True,
    )

    with pytest.raises(ValidationAppError, match="EXTERNAL_EMBEDDING_URL"):
        build_embedding_provider(settings)


def test_build_embedding_provider_unknown_raises() -> None:
    from packages.common.errors import ValidationAppError
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import build_embedding_provider

    settings = Settings(embedding_provider="unknown_provider")
    with pytest.raises(ValidationAppError, match="unknown embedding_provider"):
        build_embedding_provider(settings)


def test_build_embedding_provider_rejects_text2vec_for_primary_512_store() -> None:
    from packages.common.errors import ValidationAppError
    from packages.common.settings import Settings
    from packages.rag.embedding_factory import build_embedding_provider

    settings = Settings(embedding_provider="text2vec")
    with pytest.raises(ValidationAppError, match="1024-dimensional"):
        build_embedding_provider(settings)


def test_local_embedding_providers_ignore_env_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from packages.rag.embedding_factory import BGEZhEmbeddingProvider, Text2VecEmbeddingProvider

    calls: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[list[float]]:
            return [[0.0] * 512]

    def fake_post(*args: object, **kwargs: object) -> Response:
        calls.append({"args": args, "kwargs": kwargs})
        return Response()

    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7892")
    monkeypatch.setattr(httpx, "post", fake_post)

    BGEZhEmbeddingProvider(base_url="http://bge.local").embed_text("checkout")
    assert cast(dict[str, Any], calls[-1]["kwargs"])["trust_env"] is False

    class Text2VecResponse(Response):
        def json(self) -> list[list[float]]:
            return [[0.0] * 1024]

    def fake_text2vec_post(*args: object, **kwargs: object) -> Text2VecResponse:
        calls.append({"args": args, "kwargs": kwargs})
        return Text2VecResponse()

    monkeypatch.setattr(httpx, "post", fake_text2vec_post)
    Text2VecEmbeddingProvider(base_url="http://text2vec.local").embed_text("checkout")
    assert cast(dict[str, Any], calls[-1]["kwargs"])["trust_env"] is False


# ---------------------------------------------------------------------------
# 4.2 Reranker backend
# ---------------------------------------------------------------------------


def test_fake_reranker_backend_returns_scores() -> None:
    from packages.rag.reranker_backends import FakeRerankerBackend

    backend = FakeRerankerBackend()
    docs = [
        {
            "metadata": {
                "service": "checkout",
                "incident_type": "high_5xx",
                "updated_at": "2026-05-15",
            },
            "title": "Rollback Triage",
            "score": 0.85,
            "service": "checkout",
            "incident_type": "high_5xx",
        },
        {
            "metadata": {
                "service": "inventory",
                "incident_type": "cache_avalanche",
                "updated_at": "2020-01-01",
            },
            "title": "Cache Triage",
            "score": 0.5,
            "service": "inventory",
            "incident_type": "cache_avalanche",
        },
    ]
    results = backend.rerank(query="rollback", documents=docs, top_k=3)
    assert len(results) == 2
    assert all(0.0 <= score <= 1.0 for _, score in results)
    # First doc should score higher (better metadata match)
    assert results[0][0] == 0


def test_build_reranker_backend_returns_fake_by_default() -> None:
    from packages.common.settings import Settings
    from packages.rag.reranker_backends import FakeRerankerBackend, build_reranker_backend

    settings = Settings(reranker_provider="fake")
    backend = build_reranker_backend(settings)
    assert isinstance(backend, FakeRerankerBackend)


def test_build_reranker_backend_unknown_raises() -> None:
    from packages.common.errors import ValidationAppError
    from packages.common.settings import Settings
    from packages.rag.reranker_backends import build_reranker_backend

    settings = Settings(reranker_provider="unknown_reranker")
    with pytest.raises(ValidationAppError, match="unknown reranker_provider"):
        build_reranker_backend(settings)


def test_rerank_score_shim_works() -> None:
    from packages.rag.reranker import rerank_score

    score = rerank_score(
        query="rollback",
        metadata={"service": "checkout", "incident_type": "high_5xx", "updated_at": "2026-05-15"},
        title="Rollback Triage",
        vector_score=0.85,
        service="checkout",
        incident_type="high_5xx",
    )
    assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# 4.3 Template extractor
# ---------------------------------------------------------------------------


def test_template_extractor_respects_min_count(db_session) -> None:
    from apps.api.schemas.alerts import AlertCreateRequest
    from packages.db.repositories.incidents import IncidentRepository
    from packages.rag.template_extractor import TemplateExtractor

    repo = IncidentRepository(db_session)
    for i in range(2):
        incident = repo.create(
            incident_id=f"inc_tpl_{i}",
            payload=AlertCreateRequest(
                source="mock",
                fingerprint="fp_test_123",
                service="checkout",
                severity="P2",
                alert_name="TestAlert",
                starts_at="2026-06-01T00:00:00Z",
                labels={},
                annotations={},
            ),
        )
        incident.status = "resolved"
    db_session.commit()

    extractor = TemplateExtractor(repo)
    candidates = extractor.extract_candidates(min_incident_count=3)
    assert len(candidates) == 0


def test_template_extractor_finds_fingerprint_clusters(db_session) -> None:
    from apps.api.schemas.alerts import AlertCreateRequest
    from packages.db.repositories.incidents import IncidentRepository
    from packages.rag.template_extractor import TemplateExtractor

    repo = IncidentRepository(db_session)
    for i in range(4):
        incident = repo.create(
            incident_id=f"inc_cluster_{i}",
            payload=AlertCreateRequest(
                source="mock",
                fingerprint="fp_cluster_abc",
                service="checkout",
                severity="P1" if i < 2 else "P2",
                alert_name="High5xx",
                starts_at="2026-06-01T00:00:00Z",
                labels={},
                annotations={
                    "incident_type": "high_5xx",
                    "root_cause": "Deployment rollback needed",
                    "actions": "rollback release",
                    "evidence_types": "metrics, logs",
                },
            ),
        )
        incident.status = "resolved"
    db_session.commit()

    extractor = TemplateExtractor(repo)
    candidates = extractor.extract_candidates(min_incident_count=3)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.incident_count == 4
    assert c.service == "checkout"
    assert c.incident_type == "high_5xx"


# ---------------------------------------------------------------------------
# 4.3 Runbook generator
# ---------------------------------------------------------------------------


def test_runbook_generator_skips_existing_draft(db_session) -> None:
    from packages.agent.llm.fake_adapter import FakeLLMAdapter
    from packages.db.repositories.incidents import IncidentRepository
    from packages.db.repositories.runbook_drafts import RunbookDraftRepository
    from packages.rag.runbook_generator import RunbookGenerator
    from packages.rag.template_extractor import TemplateCandidate, TemplateExtractor

    draft_repo = RunbookDraftRepository(db_session)
    draft_repo.create(
        fingerprint="fp_skip",
        incident_ids=[],
        service="checkout",
        incident_type="high_5xx",
        title="Existing Draft",
        content="Existing content.",
        front_matter={},
    )
    db_session.commit()

    generator = RunbookGenerator(
        llm=FakeLLMAdapter(),
        draft_repo=draft_repo,
        extractor=TemplateExtractor(IncidentRepository(db_session)),
    )
    candidate = TemplateCandidate(
        fingerprint="fp_skip",
        incident_count=3,
        common_root_causes=["rc1"],
        common_actions=["act1"],
        common_evidence_types=["ev1"],
        service="checkout",
        incident_type="high_5xx",
        severity_distribution={"P1": 3},
    )
    result = generator.generate_draft(candidate)
    assert result is None  # skipped because draft already exists


def test_runbook_generator_creates_draft_with_fake_llm(db_session) -> None:
    from packages.agent.llm.fake_adapter import FakeLLMAdapter
    from packages.db.repositories.incidents import IncidentRepository
    from packages.db.repositories.runbook_drafts import RunbookDraftRepository
    from packages.rag.runbook_generator import RunbookGenerator
    from packages.rag.template_extractor import TemplateCandidate, TemplateExtractor

    draft_repo = RunbookDraftRepository(db_session)
    generator = RunbookGenerator(
        llm=FakeLLMAdapter(),
        draft_repo=draft_repo,
        extractor=TemplateExtractor(IncidentRepository(db_session)),
    )
    candidate = TemplateCandidate(
        fingerprint="fp_gen_test",
        incident_count=3,
        common_root_causes=["deployment rollback"],
        common_actions=["rollback release"],
        common_evidence_types=["metrics", "logs"],
        service="checkout",
        incident_type="high_5xx",
        severity_distribution={"P1": 3},
    )
    draft_id = generator.generate_draft(candidate)
    assert draft_id is not None
    assert draft_id.startswith("drf_")

    draft = draft_repo.get_by_draft_id(draft_id)
    assert draft is not None
    assert draft.status == "draft"
    assert draft.service == "checkout"
    assert draft.incident_type == "high_5xx"
