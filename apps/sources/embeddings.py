"""Embedding client for OpenRouter (stage 5).

Embedding calls are network operations: they must run OUTSIDE database
transactions (spec rule). Callers index in batches after chunk rows are
committed.
"""

import json
import urllib.error
import urllib.request

from django.conf import settings


class EmbeddingError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _request(payload: dict) -> dict:
    api_key = settings.OPENROUTER_API_KEY
    if not api_key:
        raise EmbeddingError("not_configured", "OPENROUTER_API_KEY не задан")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/embeddings",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.AI_HTTP_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise EmbeddingError(f"http_{exc.code}", f"OpenRouter embeddings: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise EmbeddingError("network_error", f"OpenRouter embeddings недоступен: {exc}") from exc


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts; returns vectors in input order.

    Validates that the returned dimensionality matches AI_EMBEDDING_DIMENSIONS:
    a model change must not silently mix incompatible embeddings (spec).
    """
    if not texts:
        return []
    if len(texts) > settings.EMBEDDING_BATCH_SIZE:
        raise EmbeddingError("batch_too_large", f"Батч больше {settings.EMBEDDING_BATCH_SIZE} текстов")
    data = _request({"model": settings.AI_EMBEDDING_MODEL, "input": texts})
    try:
        items = sorted(data["data"], key=lambda item: item["index"])
        vectors = [item["embedding"] for item in items]
    except (KeyError, TypeError) as exc:
        raise EmbeddingError("bad_response", "Некорректный ответ embeddings") from exc
    if len(vectors) != len(texts):
        raise EmbeddingError("bad_response", "Число векторов не совпадает с числом текстов")
    dimensions = settings.AI_EMBEDDING_DIMENSIONS
    for vector in vectors:
        if len(vector) != dimensions:
            raise EmbeddingError(
                "dimension_mismatch",
                f"Модель вернула {len(vector)} измерений, ожидается {dimensions}",
            )
    return vectors


def iter_embed(chunks_text: list[str]):
    """Yield (offset, vectors) batches for sequential, memory-bounded indexing."""
    batch = settings.EMBEDDING_BATCH_SIZE
    for offset in range(0, len(chunks_text), batch):
        yield offset, embed_texts(chunks_text[offset:offset + batch])