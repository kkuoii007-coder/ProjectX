"""Indexing and hybrid retrieval for source chunks (stage 5).

Scope rule (spec): user documents are searched only for their owner;
editorial corpus is shared. Retrieval never crosses scope.
"""

from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import transaction
from django.db.models import Q
from pgvector.django import CosineDistance

from .embeddings import EmbeddingError, embed_texts
from .models import Embedding, SourceChunk, SourceDocument
from .services import text_hash


def index_document(document) -> dict:
    """Chunk + embed an uploaded document. Network calls run outside transactions."""
    from .services import chunk_text

    document.processing_state = SourceDocument.Processing.INDEXING
    document.save(update_fields=["processing_state"])

    # DB write phase: no network. The search_vector column is DB-generated;
    # bulk_create must not include it in INSERT.
    with transaction.atomic():
        document.chunks.all().delete()
        chunk_rows = [
            SourceChunk(
                document=document,
                ordinal=ordinal,
                text=text,
                token_count=token_count,
                content_hash=text_hash(text),
                locator_json={},
            )
            for ordinal, text, token_count in chunk_text(document.normalized_text)
        ]
        SourceChunk.objects.bulk_create(chunk_rows, batch_size=200)

    # Network phase: outside any transaction, sequential batches (memory cap).
    chunks = list(document.chunks.order_by("ordinal"))
    model = settings.AI_EMBEDDING_MODEL
    dimensions = settings.AI_EMBEDDING_DIMENSIONS
    indexed = 0
    try:
        for offset, vectors in _embed_batched([chunk.text for chunk in chunks]):
            with transaction.atomic():
                for chunk, vector in zip(chunks[offset:offset + len(vectors)], vectors, strict=True):
                    Embedding.objects.update_or_create(
                        source_chunk=chunk,
                        model=model,
                        dimensions=dimensions,
                        content_hash=chunk.content_hash,
                        defaults={"vector": vector},
                    )
                indexed += len(vectors)
    except EmbeddingError as exc:
        document.processing_state = SourceDocument.Processing.FAILED
        document.save(update_fields=["processing_state"])
        return {"indexed": indexed, "state": "failed", "error": exc.code, "message": str(exc)}

    with transaction.atomic():
        document.processing_state = SourceDocument.Processing.READY
        document.save(update_fields=["processing_state"])
    return {"indexed": indexed, "state": "ready", "chunks": len(chunks)}


def _embed_batched(texts):
    """Split into batches here so index_document stays sequential per batch."""
    batch = settings.EMBEDDING_BATCH_SIZE
    for offset in range(0, len(texts), batch):
        yield offset, embed_texts(texts[offset:offset + batch])


def _scope_documents(owner, document_ids=None):
    """Visible documents for retrieval: owner-private + editorial corpus."""
    queryset = SourceDocument.objects.filter(processing_state=SourceDocument.Processing.READY)
    if document_ids is not None:
        queryset = queryset.filter(id__in=document_ids)
    return queryset.filter(
        Q(owner=owner) | Q(owner__isnull=True, visibility=SourceDocument.Visibility.EDITORIAL)
    )


def hybrid_search(*, owner, query: str, document_ids=None, top_n=None):
    """Deterministic hybrid FTS + vector search inside scope, dedup by chunk.

    Returns list of (chunk, score_json). If embeddings are unavailable the
    FTS leg still works; if both legs fail the result is empty, not fake.
    """
    top_n = top_n or settings.RETRIEVAL_MAX_CHUNKS
    documents = _scope_documents(owner, document_ids)
    chunk_ids = list(documents.values_list("chunks__id", flat=True))
    if not chunk_ids:
        return []

    scores: dict[int, dict] = {}
    max_fts = 0.0
    search_query = SearchQuery(query, search_type="websearch", config="english")
    if query.strip():
        fts_hits = (
            SourceChunk.objects.filter(id__in=chunk_ids)
            .annotate(rank=SearchRank("search_vector", search_query))
            .filter(search_vector=search_query)
            .order_by("-rank")[:top_n * 2]
        )
        for chunk in fts_hits:
            scores[chunk.id] = {"chunk": chunk, "fts": float(chunk.rank)}
            max_fts = max(max_fts, float(chunk.rank))

    max_vec = 0.0
    try:
        query_vectors = embed_texts([query])
    except EmbeddingError:
        query_vectors = []
    if query_vectors:
        # Annotate distance from the joined embedding table; pgvector's
        # CosineDistance needs (expression, vector).
        vector_hits = (
            SourceChunk.objects.filter(
                id__in=chunk_ids,
                embeddings__model=settings.AI_EMBEDDING_MODEL,
                embeddings__dimensions=settings.AI_EMBEDDING_DIMENSIONS,
            )
            .annotate(distance=CosineDistance("embeddings__vector", query_vectors[0]))
            .order_by("distance")[:top_n * 2]
        )
        for chunk in vector_hits:
            similarity = 1.0 - float(chunk.distance)
            entry = scores.setdefault(chunk.id, {"chunk": chunk, "fts": 0.0})
            entry["vec"] = similarity
            max_vec = max(max_vec, similarity)

    # Deterministic merge: normalize each leg to [0,1] by its max, average.
    results = []
    for entry in scores.values():
        fts_norm = entry["fts"] / max_fts if max_fts else 0.0
        vec_norm = entry.get("vec", 0.0) / max_vec if max_vec else 0.0
        combined = (fts_norm + vec_norm) / 2
        results.append((entry["chunk"], {"fts": round(fts_norm, 4), "vector": round(vec_norm, 4), "combined": round(combined, 4)}))
    results.sort(key=lambda pair: (-pair[1]["combined"], pair[0].id))
    return results[:top_n]
