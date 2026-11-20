"""Indexing and hybrid retrieval for source chunks (stage 5).

Scope rule (spec): user documents are searched only for their owner;
editorial corpus is shared. Retrieval never crosses scope.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from pgvector.django import CosineDistance

from .embeddings import EmbeddingError, embed_texts
from .lexical import TextSearch, scope_vocabulary, text_search
from .locators import format_locator
from .models import Embedding, SourceChunk, SourceDocument, SourceEdition
from .services import text_hash

logger = logging.getLogger(__name__)

# Marker in SourceDocument.metadata_json["chunking"]: the chunks were written
# by the importer together with their locators and must not be rebuilt.
STRUCTURED = "structured"


def index_document(document) -> dict:
    """Chunk + embed an uploaded document. Network calls run outside transactions.

    A structured document (import_shelf_book) already has its chunks with
    locators: they are kept as they are and only the missing embeddings are
    built, so a repeated run after a failure does not pay twice.
    """
    from .services import chunk_text

    structured = (document.metadata_json or {}).get("chunking") == STRUCTURED
    document.processing_state = SourceDocument.Processing.INDEXING
    document.save(update_fields=["processing_state"])

    if not structured:
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
    if structured and not chunks:
        document.processing_state = SourceDocument.Processing.FAILED
        document.save(update_fields=["processing_state"])
        return {"indexed": 0, "state": "failed", "error": "no_chunks", "message": "У документа нет фрагментов: повторите импорт"}
    pending = chunks
    if structured:
        done = set(
            Embedding.objects.filter(source_chunk__document=document, model=model, dimensions=dimensions)
            .values_list("source_chunk_id", "content_hash")
        )
        pending = [chunk for chunk in chunks if (chunk.id, chunk.content_hash) not in done]
    indexed = 0
    try:
        for offset, vectors in _embed_batched([chunk.text for chunk in pending]):
            with transaction.atomic():
                for chunk, vector in zip(pending[offset:offset + len(vectors)], vectors, strict=True):
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
        update_fields = ["processing_state"]
        if _apply_edition_pending(document):
            update_fields.append("metadata_json")
        document.save(update_fields=update_fields)
    return {"indexed": indexed, "state": "ready", "chunks": len(chunks), "kept_chunks": structured}


# Fields of an edition that describe the source file of the document on the
# shelf: where the file is, when it was taken, its hash and the query lexicon
# tuned to its text.
EDITION_SOURCE_FIELDS = ("url", "retrieved_at", "source_sha256", "query_lexicon")
EDITION_PENDING = "edition_pending"


def _apply_edition_pending(document) -> bool:
    """A new version of a book was imported while the previous one was on the
    shelf: the edition kept describing the previous file. Now that this
    document is READY and takes its place, the edition describes this one."""
    pending = (document.metadata_json or {}).get(EDITION_PENDING)
    if not pending or not document.edition_id:
        return False
    edition = document.edition
    newer_on_shelf = edition.documents.filter(
        processing_state=SourceDocument.Processing.READY, version__gt=document.version
    ).exists()
    values = {name: pending[name] for name in EDITION_SOURCE_FIELDS if name in pending}
    if values and not newer_on_shelf:
        if values.get("retrieved_at"):
            values["retrieved_at"] = datetime.fromisoformat(values["retrieved_at"])
        for name, value in values.items():
            setattr(edition, name, value)
        edition.save(update_fields=list(values))
    document.metadata_json = {key: value for key, value in document.metadata_json.items() if key != EDITION_PENDING}
    return True


def _embed_batched(texts):
    """Split into batches here so index_document stays sequential per batch."""
    batch = settings.EMBEDDING_BATCH_SIZE
    for offset in range(0, len(texts), batch):
        yield offset, embed_texts(texts[offset:offset + batch])


def superseded_document_ids() -> set:
    """Documents replaced by a READY successor, directly or through a chain.

    `new.supersedes = old` hides `old` only once `new` is READY: until the
    new edition is indexed the shelf keeps the old one. A successor hides
    only documents of the same owner (editorial replaces editorial).
    """
    links = list(
        SourceDocument.objects.filter(supersedes__isnull=False).values_list(
            "id", "supersedes_id", "processing_state", "owner_id", "supersedes__owner_id"
        )
    )
    older = {document_id: (old_id, old_owner) for document_id, old_id, _, _, old_owner in links}
    hidden = set()
    for document_id, _, state, owner_id, _ in links:
        if state != SourceDocument.Processing.READY:
            continue
        seen = {document_id}
        step = older.get(document_id)
        while step and step[0] not in seen:
            old_id, old_owner = step
            if old_owner != owner_id:
                break
            seen.add(old_id)
            hidden.add(old_id)
            step = older.get(old_id)
    return hidden


def _scope_documents(owner, document_ids=None):
    """Visible documents for retrieval: owner-private + editorial corpus."""
    queryset = SourceDocument.objects.filter(processing_state=SourceDocument.Processing.READY)
    if document_ids is not None:
        queryset = queryset.filter(id__in=document_ids)
    hidden = superseded_document_ids()
    if hidden:
        queryset = queryset.exclude(id__in=hidden)
    editorial = Q(owner__isnull=True, visibility=SourceDocument.Visibility.EDITORIAL)
    if owner is None:
        return queryset.filter(editorial)
    return queryset.filter(Q(owner=owner) | editorial)


@dataclass
class SearchOutcome:
    results: list = field(default_factory=list)  # [(chunk, score_json)]
    vector_available: bool = True
    vector_error: str = ""
    text: TextSearch = field(default_factory=TextSearch)


def _search(documents, query: str, top_n=None) -> SearchOutcome:
    """Deterministic hybrid of the text half (lexical.py) and pgvector."""
    if not top_n or top_n < 0:
        top_n = settings.RETRIEVAL_MAX_CHUNKS
    outcome = SearchOutcome()
    document_ids = list(documents.values_list("id", flat=True))
    if not document_ids or not query.strip():
        return outcome

    editions = SourceEdition.objects.filter(documents__id__in=document_ids).select_related("work").distinct()
    outcome.text = text_search(
        document_ids=document_ids, query=query, vocabulary=scope_vocabulary(editions), limit=top_n * 2
    )
    scores = {chunk_id: {"fts": values["raw"], "coverage": values["coverage"]} for chunk_id, values in outcome.text.scores.items()}

    try:
        query_vectors = embed_texts([query])
    except EmbeddingError as exc:
        query_vectors = []
        outcome.vector_available = False
        outcome.vector_error = exc.code
        # No key is a deliberate dev/test setup; anything else is a failure.
        level = logging.INFO if exc.code == "not_configured" else logging.WARNING
        logger.log(level, "Векторная половина поиска недоступна (%s): %s", exc.code, exc)
    if query_vectors:
        # Annotate distance from the joined embedding table; pgvector's
        # CosineDistance needs (expression, vector).
        vector_hits = (
            SourceChunk.objects.filter(
                document_id__in=document_ids,
                embeddings__model=settings.AI_EMBEDDING_MODEL,
                embeddings__dimensions=settings.AI_EMBEDDING_DIMENSIONS,
            )
            .annotate(distance=CosineDistance("embeddings__vector", query_vectors[0]))
            .order_by("distance")
            .values_list("id", "distance")[:top_n * 2]
        )
        for chunk_id, distance in vector_hits:
            scores.setdefault(chunk_id, {}).setdefault("vec", 1.0 - float(distance))

    max_fts = max((entry.get("fts", 0.0) for entry in scores.values()), default=0.0)
    max_vec = max((entry.get("vec", 0.0) for entry in scores.values()), default=0.0)
    # The caller shows the book of each hit: one query, not three per hit.
    chunks = SourceChunk.objects.select_related("document__edition__work").in_bulk(scores.keys())
    # Deterministic merge: normalize each leg to [0,1] by its max, average.
    for chunk_id, entry in scores.items():
        fts_raw = entry.get("fts", 0.0)
        vector_raw = entry.get("vec")
        fts_norm = fts_raw / max_fts if max_fts > 0 else 0.0
        vec_norm = max(vector_raw or 0.0, 0.0) / max_vec if max_vec > 0 else 0.0
        outcome.results.append(
            (
                chunks[chunk_id],
                {
                    "fts": round(fts_norm, 4),
                    "vector": round(vec_norm, 4),
                    "combined": round((fts_norm + vec_norm) / 2, 4),
                    # Raw values for thresholds: BM25 of the text half, the
                    # idf-weighted share of the question found in the chunk,
                    # cosine similarity (None: the chunk was not a vector hit).
                    "fts_raw": round(fts_raw, 4),
                    "fts_coverage": round(entry.get("coverage", 0.0), 4),
                    "vector_raw": None if vector_raw is None else round(vector_raw, 4),
                },
            )
        )
    outcome.results.sort(key=lambda pair: (-pair[1]["combined"], pair[0].id))
    outcome.results = outcome.results[:top_n]
    return outcome


@dataclass(frozen=True)
class ShelfHit:
    chunk: SourceChunk
    locator: str  # "Politics 1.1252b–1253a", "" if the chunk has no locator
    scores: dict  # fts_raw, fts_coverage, vector_raw, combined, fts, vector


@dataclass
class ShelfSearchResult:
    hits: list
    vector_available: bool
    elapsed_ms: float
    vector_error: str = ""
    # What the text half searched for and what it could not use.
    fts_terms: list = field(default_factory=list)
    missing_terms: list = field(default_factory=list)
    unmapped_terms: list = field(default_factory=list)


def search_scope(*, owner, query: str, document_ids=None, top_n=None) -> ShelfSearchResult:
    """Hybrid search in the scope of `owner` (None: the editorial shelf only).

    Same rules as hybrid_search, but the result also says whether the vector
    half ran and how long the search took. See search_shelf() for the fields.
    """
    started = time.perf_counter()
    outcome = _search(_scope_documents(owner, document_ids), query, top_n)
    return ShelfSearchResult(
        hits=[ShelfHit(chunk, format_locator(chunk.locator_json), scores) for chunk, scores in outcome.results],
        vector_available=outcome.vector_available,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        vector_error=outcome.vector_error,
        fts_terms=outcome.text.terms,
        missing_terms=outcome.text.missing,
        unmapped_terms=outcome.text.unmapped,
    )


def hybrid_search(*, owner, query: str, document_ids=None, top_n=None):
    """Deterministic hybrid FTS + vector search inside scope, dedup by chunk.

    Returns list of (chunk, score_json). If embeddings are unavailable the
    FTS leg still works; if both legs fail the result is empty, not fake.
    score_json: `fts`, `vector`, `combined` (each leg divided by its best
    value, then averaged) and the raw `fts_raw`, `fts_coverage`,
    `vector_raw`. Use search_scope() or search_shelf() to learn whether the
    vector half ran.
    """
    return _search(_scope_documents(owner, document_ids), query, top_n).results


def search_shelf(query: str, top_n=None) -> ShelfSearchResult:
    """Search the editorial shelf. The interface for the reader's question.

    The shelf is the same for everyone: only READY editorial documents that
    no READY document has replaced; no user is involved.

    Returns ShelfSearchResult:
    - `hits`: best first, each a ShelfHit with `chunk` (SourceChunk),
      `locator` (string such as "Politics 1.1252b–1253a") and `scores`:
        `fts_raw`       BM25 of the text half (0 if the chunk came from the
                        vector half only);
        `fts_coverage`  0..1, idf-weighted share of the question's concepts
                        present in the chunk; words the shelf does not know
                        count as concepts that are not there;
        `vector_raw`    cosine similarity, or None if the chunk was not a
                        vector hit or the vector half did not run;
        `combined`      the merged score used for the order;
    - `vector_available`: False when the embedding call failed
      (`vector_error` holds the code). Then `hits` come from the text half
      alone: "search is degraded", which is not the same as "the source has
      nothing";
    - `elapsed_ms`: time of the whole search;
    - `fts_terms`: the words the text half searched for;
    - `missing_terms`: significant words of the question found nowhere on
      the shelf;
    - `unmapped_terms`: Russian words that no book's lexicon translates.
      Both kinds lower `fts_coverage` of every hit: the question asks about
      something the shelf does not have ("Что такое свобода у Сартра?").
    `top_n` of None, 0 or less means the default (RETRIEVAL_MAX_CHUNKS).
    The exact page of a verbatim quotation: locators.locate_quote().
    """
    return search_scope(owner=None, query=query, top_n=top_n)
