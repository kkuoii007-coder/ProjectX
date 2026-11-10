"""Text extraction, normalization and chunking for source documents.

Implements docs/spec/docs/sources-and-rag.md: user files are untrusted
data; extraction never executes embedded content, and chunking follows
paragraph boundaries with 300-600 word target size.
"""

import hashlib
import re

from django.conf import settings

Chunk = tuple  # (ordinal, text, token_count)


class ExtractionError(Exception):
    """Raised when a document has no usable text layer."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- extraction -------------------------------------------------------------

_PDF_CODE = ".pdf"
_TEXT_SUFFIXES = (".txt", ".md", ".markdown")


def extract_text(filename: str, data: bytes) -> str:
    """Extract normalized text from TXT/MD/PDF bytes without executing content."""
    name = filename.lower()
    if name.endswith(_PDF_CODE):
        return _extract_pdf(data)
    if name.endswith(_TEXT_SUFFIXES):
        return normalize_text(data.decode("utf-8"))
    raise ExtractionError("unsupported_type", "Поддерживаются только TXT, MD и PDF")


def _extract_pdf(data: bytes) -> str:
    import io

    from pypdf import PdfReader  # patched in tests

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:  # pypdf raises many malformed-PDF types
        raise ExtractionError("bad_pdf", f"PDF не разобран: {exc}") from exc
    if pages > settings.SOURCE_MAX_PAGES:
        raise ExtractionError("too_many_pages", f"PDF содержит {pages} страниц, лимит {settings.SOURCE_MAX_PAGES}")
    parts = []
    for index, page in enumerate(reader.pages):
        page_text = page.extract_text() or ""
        if page_text.strip():
            parts.append(f"\n\n[page {index + 1}]\n{page_text}")
    text = "".join(parts)
    if not text.strip():
        raise ExtractionError("no_text_layer", "В PDF нет текстового слоя (скан без OCR не поддерживается)")
    return normalize_text(text)


# --- normalization ----------------------------------------------------------

_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


def normalize_text(text: str) -> str:
    """Collapse whitespace but keep paragraph and page-marker structure."""
    text = text.replace("\u00ad", "")  # soft hyphen
    text = _WS_RE.sub(" ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)
    # De-hyphenate a word split at a line break; runs after whitespace
    # collapse so a trailing space cannot block the join.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = _BLANK_RE.sub("\n\n", text)
    return text.strip()


# --- chunking ---------------------------------------------------------------

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


def _split_paragraphs(text: str):
    for block in text.split("\n\n"):
        block = block.strip()
        if block:
            yield block


def _split_long_paragraph(paragraph: str, max_words: int):
    """Split an over-long paragraph on sentence boundaries, never mid-sentence."""
    sentences = _SENTENCE_END.split(paragraph)
    current = []
    count = 0
    for sentence in sentences:
        words = sentence.split()
        if count + len(words) > max_words and current:
            yield " ".join(current)
            current, count = [], 0
        # A single sentence longer than max_words is kept whole (spec:
        # never break a sentence for fixed size).
        current.append(sentence)
        count += len(words)
    if current:
        yield " ".join(current)


def chunk_text(text: str) -> list[Chunk]:
    """Split normalized text into 300-600 word chunks with small overlap."""
    min_words = settings.SOURCE_CHUNK_WORDS_MIN
    max_words = settings.SOURCE_CHUNK_WORDS_MAX
    overlap_words = min(50, max_words // 12)

    raw_blocks = []
    for paragraph in _split_paragraphs(text):
        if len(paragraph.split()) > max_words:
            raw_blocks.extend(_split_long_paragraph(paragraph, max_words))
        else:
            raw_blocks.append(paragraph)

    chunks = []
    current: list[str] = []
    count = 0
    for block in raw_blocks:
        words = len(block.split())
        if count >= min_words and count + words > max_words:
            chunks.append(" ".join(current))
            tail_words = " ".join(current).split()[-overlap_words:] if overlap_words else []
            current = [" ".join(tail_words)] if tail_words else []
            count = len(tail_words)
        current.append(block)
        count += words
    if current:
        chunks.append(" ".join(current))

    result = []
    for ordinal, body in enumerate(chunks):
        token_count = len(body.split())
        result.append((ordinal, body, token_count))
    return result


# --- import pipeline (DB writes live here; no network) ----------------------


def create_document_from_upload(*, owner, filename: str, data: bytes, title: str = ""):
    """Create a private user document with normalized text, hash-deduplicated.

    Returns (document, created). A repeated hash does not create a new
    version of the document (spec: unique per lineage+hash).
    """
    import uuid

    from .models import SourceDocument

    normalized = extract_text(filename, data)
    content_hash = text_hash(normalized)
    existing = SourceDocument.objects.filter(
        owner=owner, content_hash=content_hash, origin=SourceDocument.Origin.USER
    ).first()
    if existing:
        return existing, False
    document = SourceDocument.objects.create(
        owner=owner,
        lineage_id=uuid.uuid4(),
        origin=SourceDocument.Origin.USER,
        visibility=SourceDocument.Visibility.PRIVATE,
        content_hash=content_hash,
        normalized_text=normalized,
        processing_state=SourceDocument.Processing.EXTRACTING,
        metadata_json={"filename": filename[:255], "title": title or filename},
    )
    document.processing_state = SourceDocument.Processing.UPLOADED
    document.save(update_fields=["processing_state"])
    return document, True