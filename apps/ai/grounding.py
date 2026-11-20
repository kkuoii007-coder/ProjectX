"""Answer with a source: the deterministic half.

The search before generation, the support threshold, the structural check
of the model's answer, the snapshot written into the immutable revision and
the links stored in the database. Nothing here calls a model; the order of
the paid calls is in `services.execute_job`.

Words used below:
- fragment — a chunk of a shelf book found for the question (`RetrievalItem`
  with a short id `S1`…`S8`);
- support — a fragment that passed the threshold and was shown to the model;
- source — one `{id, quote}` pair returned by the model for a part of the
  answer. A source is valid when the quote stands word for word in the text
  of its fragment.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from apps.sources.locators import format_place, span_place
from apps.sources.models import SourceDocument
from apps.sources.retrieval import search_shelf
from apps.sources.shelf import edition_links

from .models import RetrievalItem

QUOTE_MIN_WORDS = 5
QUOTE_MAX_WORDS = 60
QUOTED_PHRASE_MIN_WORDS = 3
MAX_SOURCES_PER_PART = 3
MIN_PARTS, MAX_PARTS = 3, 5
MAX_SENTENCES_PER_PART = 12
MAX_SENTENCE_CHARS = 600

NO_SOURCE_MESSAGE = "На нашей полке пока нет источника для этого вопроса. Кредиты не списаны."
SEARCH_UNAVAILABLE_MESSAGE = "Поиск по полке сейчас недоступен. Кредиты не списаны, попробуйте позже."
VALIDATION_FAILED_MESSAGE = (
    "Ответ не удалось подтвердить отрывками из книги: ссылки на источник не прошли проверку. "
    "Текст не создан, кредиты не списаны. Попробуйте задать вопрос иначе."
)


class SearchUnavailable(Exception):
    """The vector half did not run and the text half found no support. This
    is a failure of the search, not a statement about the shelf."""

    code = "search_unavailable"


# --- search and threshold ------------------------------------------------------


def is_support(scores: dict) -> bool:
    """A fragment supports the question if either half of the search says so."""
    vector = scores.get("vector_raw")
    if vector is not None and vector >= settings.SHELF_MIN_VECTOR_SIMILARITY:
        return True
    return (scores.get("fts_coverage") or 0.0) >= settings.SHELF_MIN_FTS_COVERAGE


@dataclass(frozen=True)
class Fragment:
    citation_id: str  # "S1"
    chunk: object  # SourceChunk
    document: object  # SourceDocument with edition and work loaded
    scores: dict
    support: bool

    @property
    def book(self) -> str:
        """`Aristotle, Politics`."""
        edition = self.document.edition
        return edition.work.label if edition else ""

    @property
    def locator(self) -> str:
        """`Aristotle, Politics, 1.1252b–1253a`: the whole fragment."""
        return _label(self.book, format_place(self.chunk.locator_json))


def _label(book: str, place: str) -> str:
    return ", ".join(piece for piece in (book, place) if piece)


@dataclass
class Retrieval:
    fragments: list = field(default_factory=list)
    vector_available: bool = True
    vector_error: str = ""
    elapsed_ms: float = 0.0

    @property
    def supports(self) -> list:
        return [fragment for fragment in self.fragments if fragment.support]

    def model_sources(self) -> list:
        """What the model receives: only the supports."""
        return [as_model_source(fragment) for fragment in self.supports]

    def summary(self) -> dict:
        """Best raw scores of the search: how the threshold worked."""
        vectors = [f.scores["vector_raw"] for f in self.fragments if f.scores.get("vector_raw") is not None]
        coverages = [f.scores.get("fts_coverage") or 0.0 for f in self.fragments]
        return {
            "found": len(self.fragments),
            "supports": len(self.supports),
            "vector_available": self.vector_available,
            "vector_error": self.vector_error,
            "best_vector_raw": max(vectors) if vectors else None,
            "best_fts_coverage": max(coverages) if coverages else None,
            "min_vector_similarity": settings.SHELF_MIN_VECTOR_SIMILARITY,
            "min_fts_coverage": settings.SHELF_MIN_FTS_COVERAGE,
            "search_ms": self.elapsed_ms,
        }


def as_model_source(fragment: Fragment) -> dict:
    return {"id": fragment.citation_id, "locator": fragment.locator, "text": fragment.chunk.text}


def retrieve(job) -> Retrieval:
    """Search the shelf for the job's question and store what was found.

    The search embeds the question (a network call), so it runs outside any
    transaction; only the RetrievalItem rows are written in one. Every found
    fragment is stored with its rank and scores, also those below the
    threshold: the rows show why a question was or was not answered.
    """
    question = job.input_json.get("instruction", "")
    result = search_shelf(question)
    documents = SourceDocument.objects.select_related("edition__work").in_bulk({hit.chunk.document_id for hit in result.hits})
    fragments = [
        Fragment(
            citation_id=f"S{rank}",
            chunk=hit.chunk,
            document=documents[hit.chunk.document_id],
            scores=dict(hit.scores),
            support=is_support(hit.scores),
        )
        for rank, hit in enumerate(result.hits, start=1)
    ]
    with transaction.atomic():
        RetrievalItem.objects.filter(job=job).delete()
        RetrievalItem.objects.bulk_create(
            RetrievalItem(
                job=job,
                source_chunk=fragment.chunk,
                citation_id=fragment.citation_id,
                rank=rank,
                score_json={**fragment.scores, "support": fragment.support},
            )
            for rank, fragment in enumerate(fragments, start=1)
        )
    return Retrieval(
        fragments=fragments,
        vector_available=result.vector_available,
        vector_error=result.vector_error,
        elapsed_ms=result.elapsed_ms,
    )


# --- verbatim comparison -----------------------------------------------------------

_QUOTE_CHARS = "'\"`´‘’‚‛“”„‟«»‹›"
_DASH_CHARS = "-‐‑‒–—―−"
_EDGE_PUNCTUATION = " .,;:!?…"


def _normalize(text: str):
    """Text for the word-for-word comparison plus, for every character of
    the result, its position in the original.

    Not significant: the amount and kind of white space, the look of
    quotation marks and apostrophes, the look of dashes and the spaces
    around them, `…` against `...`. Everything else is: letters, case,
    punctuation and word order.
    """
    chars, positions = [], []
    for index, char in enumerate(text):
        if char.isspace():
            char = " "
        elif char in _QUOTE_CHARS:
            char = "'"
        elif char in _DASH_CHARS:
            char = "-"
        elif char == "…":
            chars.extend("...")
            positions.extend([index] * 3)
            continue
        if char == " " and (not chars or chars[-1] in " -"):
            continue  # leading space, a run of spaces, a space after a dash
        if char == "-" and chars and chars[-1] == " ":
            chars.pop()  # the space before a dash
            positions.pop()
        chars.append(char)
        positions.append(index)
    while chars and chars[-1] == " ":
        chars.pop()
        positions.pop()
    return "".join(chars), positions


def _word_count(text: str) -> int:
    return len(text.split())


def find_verbatim(text: str, quote: str):
    """(start, end) of `quote` inside `text` in characters of `text`, or None."""
    wanted, _ = _normalize(quote)
    if not wanted:
        return None
    normalized, positions = _normalize(text)
    at = normalized.find(wanted)
    if at < 0:
        return None
    return positions[at], positions[at + len(wanted) - 1] + 1


_QUOTED_PATTERNS = [
    re.compile(r"“([^“”]+)”"),
    re.compile(r"„([^„“”]+)[“”]"),
    re.compile(r"«([^«»]+)»"),
    re.compile(r'"([^"]+)"'),
]


def _single_quoted(text: str, opening: str, closing: str):
    """Readings of the phrases in single quotation marks.

    The closing mark looks like an apostrophe, so a phrase with a plural
    possessive inside (‘the sons and sons’ sons live together’) can be read
    two ways. Every reading is returned; the caller accepts the phrase when
    one of them stands in the book. An apostrophe inside a word (it’s,
    man’s) opens and closes nothing.
    """
    straight = opening == closing
    at = 0
    while at < len(text):
        char = text[at]
        opens = char == opening and at + 1 < len(text) and not text[at + 1].isspace()
        if opens and straight and at > 0 and (text[at - 1].isalnum() or text[at - 1] == "_"):
            opens = False
        if not opens:
            at += 1
            continue
        readings, first_end = [], None
        for end in range(at + 1, len(text)):
            if not straight and text[end] == opening:
                break
            followed_by_word = end + 1 < len(text) and (text[end + 1].isalnum() or text[end + 1] == "_")
            if text[end] == closing and not followed_by_word and not text[end - 1].isspace():
                readings.append(text[at + 1 : end])
                first_end = first_end or end
        if readings:
            yield readings
        at = (first_end or at) + 1


def quoted_phrases(text: str) -> list:
    """Phrases of QUOTED_PHRASE_MIN_WORDS words or more that stand in
    quotation marks in `text`: a list of tuples of readings. A phrase in
    double marks has one reading; see `_single_quoted` for the single ones."""
    groups = [[match.group(1)] for pattern in _QUOTED_PATTERNS for match in pattern.finditer(text)]
    groups += list(_single_quoted(text, "‘", "’")) + list(_single_quoted(text, "'", "'"))
    found = []
    for readings in groups:
        cleaned = tuple(
            phrase for phrase in dict.fromkeys(reading.strip(_EDGE_PUNCTUATION) for reading in readings) if _word_count(phrase) >= QUOTED_PHRASE_MIN_WORDS
        )
        if cleaned and cleaned not in found:
            found.append(cleaned)
    return found


def _phrase_in_fragments(phrase: str, normalized_texts: list) -> bool:
    wanted, _ = _normalize(phrase)
    # A quotation that opens a sentence may get a capital letter; nothing
    # else about it may differ from the book.
    variants = {wanted, wanted[:1].lower() + wanted[1:], wanted[:1].upper() + wanted[1:]}
    return any(variant in text for variant in variants for text in normalized_texts)


# --- structural check of the answer -------------------------------------------------


@dataclass(frozen=True)
class ResolvedSource:
    fragment: Fragment
    start: int  # characters of the fragment text
    end: int
    excerpt: str  # the book's own words, as they stand in the fragment

    @property
    def place(self) -> str:
        return span_place(self.fragment.chunk.locator_json, self.start, self.end) or format_place(self.fragment.chunk.locator_json)

    @property
    def label(self) -> str:
        """`Aristotle, Politics, 1.1253a`: the exact page of the excerpt."""
        return _label(self.fragment.book, self.place)

    @property
    def locator(self) -> str:
        """`Politics 1.1253a`, the form used by search_sources and locate_quote."""
        work = (self.fragment.chunk.locator_json or {}).get("work", "")
        return f"{work} {self.place}".strip()


@dataclass
class CheckedAnswer:
    parts: list = field(default_factory=list)  # per part: [ResolvedSource]
    violations: list = field(default_factory=list)  # English, for the model and the log

    @property
    def ok(self) -> bool:
        return not self.violations

    def cited_fragments(self) -> list:
        seen = {}
        for sources in self.parts:
            for source in sources:
                seen.setdefault(source.fragment.citation_id, source.fragment)
        return list(seen.values())


def _shape_errors(series) -> list:
    if not isinstance(series, dict):
        return ["the answer is not an object"]
    parts = series.get("parts")
    if not isinstance(parts, list) or not MIN_PARTS <= len(parts) <= MAX_PARTS:
        return [f"the answer must have {MIN_PARTS} to {MAX_PARTS} parts"]
    errors = []
    for number, part in enumerate(parts, start=1):
        sentences = part.get("sentences") if isinstance(part, dict) else None
        if not isinstance(sentences, list) or not 1 <= len(sentences) <= MAX_SENTENCES_PER_PART:
            errors.append(f"part {number}: 1 to {MAX_SENTENCES_PER_PART} sentences are required")
            continue
        for sentence in sentences:
            if not isinstance(sentence, dict) or any(
                not isinstance(sentence.get(key), str) or not sentence[key].strip() or len(sentence[key]) > MAX_SENTENCE_CHARS
                for key in ("en", "ru")
            ):
                errors.append(f"part {number}: every sentence needs a non-empty en and ru of at most {MAX_SENTENCE_CHARS} characters")
                break
        if not isinstance(part.get("sources"), list):
            errors.append(f"part {number}: sources must be a list")
    return errors


def _english_texts(series: dict, part: dict) -> list:
    return [part.get("title_en") or ""] + [sentence["en"] for sentence in part["sentences"]]


def check_answer(series, supports: list) -> CheckedAnswer:
    """The structural check of a model answer against the supports.

    Rules (each violation rejects the whole answer):
    1. every source id is one of the ids shown to the model;
    2. a quote must stand word for word in the text of its fragment and
       have QUOTE_MIN_WORDS..QUOTE_MAX_WORDS words, otherwise that source is
       not valid; every part keeps at least one valid source;
    3. any phrase of three words or more in quotation marks in the English
       text (titles, sentences, glossary) stands word for word in one of
       the supports.
    """
    checked = CheckedAnswer()
    shape = _shape_errors(series)
    if shape:
        checked.violations = shape
        return checked
    by_id = {fragment.citation_id: fragment for fragment in supports}
    normalized_texts = [_normalize(fragment.chunk.text)[0] for fragment in supports]

    def check_quoted(where: str, texts: list):
        for text in texts:
            for readings in quoted_phrases(text):
                if not any(_phrase_in_fragments(phrase, normalized_texts) for phrase in readings):
                    checked.violations.append(f"{where}: the words in quotation marks are not in the sources word for word: “{readings[-1][:200]}”")

    check_quoted("title", [series.get("title_en") or ""])
    for number, part in enumerate(series["parts"], start=1):
        valid, reasons = [], []
        for item in part["sources"]:
            source_id = item.get("id") if isinstance(item, dict) else None
            quote = item.get("quote") if isinstance(item, dict) else None
            fragment = by_id.get(source_id)
            if fragment is None:
                checked.violations.append(f"part {number}: source id {str(source_id)[:20]!r} is not one of the provided ids")
                continue
            if not isinstance(quote, str) or not quote.strip():
                reasons.append(f"{source_id}: the quote is empty")
                continue
            words = _word_count(quote)
            if words > QUOTE_MAX_WORDS:
                reasons.append(f"{source_id}: the quote has {words} words, the limit is {QUOTE_MAX_WORDS}")
                continue
            if words < QUOTE_MIN_WORDS:
                reasons.append(f"{source_id}: the quote has {words} words, at least {QUOTE_MIN_WORDS} are required")
                continue
            span = find_verbatim(fragment.chunk.text, quote)
            if span is None:
                reasons.append(f"{source_id}: the quote is not in the text of {source_id} word for word: “{quote[:200]}”")
                continue
            excerpt = " ".join(fragment.chunk.text[span[0]:span[1]].split())
            if not any(known.fragment is fragment and known.start == span[0] and known.end == span[1] for known in valid):
                valid.append(ResolvedSource(fragment=fragment, start=span[0], end=span[1], excerpt=excerpt))
        if not valid:
            checked.violations.append(f"part {number}: no valid source" + (" (" + "; ".join(reasons) + ")" if reasons else ""))
        checked.parts.append(valid[:MAX_SOURCES_PER_PART])
        check_quoted(f"part {number}", _english_texts(series, part))
    glossary = series.get("glossary") if isinstance(series.get("glossary"), list) else []
    check_quoted("glossary", [str(entry.get("explanation_en") or "") for entry in glossary if isinstance(entry, dict)])
    return checked


# --- snapshot and links ----------------------------------------------------------------


def edition_record(document) -> dict:
    """What a revision remembers about the edition it rests on."""
    edition = document.edition
    work = edition.work
    return {
        "book": work.label,
        "book_ru": work.label_ru,
        "attribution": edition.attribution(),
        "edition_key": edition.key or "",
        "translator": edition.translator,
        "license": edition.license,
        "license_url": edition.license_url,
        "url": edition.url or "",
        # What the licence of the source asks to show next to the
        # attribution: digital publisher, source file, licence.
        "links": [list(pair) for pair in edition_links(edition)],
        "source_sha256": edition.source_sha256,
        "document_id": str(document.pk),
        "document_version": document.version,
        "document_content_hash": document.content_hash,
    }


def _documents(checked: CheckedAnswer) -> list:
    seen = {}
    for fragment in checked.cited_fragments():
        seen.setdefault(fragment.document.pk, fragment.document)
    return list(seen.values())


def build_payload(series: dict, checked: CheckedAnswer) -> dict:
    """The immutable revision payload: the text plus, for every part, the
    lines of its sources. The page is rendered from this snapshot alone, so
    re-indexing the book later cannot break or change it."""
    editions = [edition_record(document) for document in _documents(checked)]
    glossary = series.get("glossary") if isinstance(series.get("glossary"), list) else []
    return {
        "title_en": str(series.get("title_en") or "")[:200],
        "title_ru": str(series.get("title_ru") or "")[:200],
        "parts": [
            {
                "title_en": str(part.get("title_en") or "")[:200],
                "sentences": [{"en": sentence["en"], "ru": sentence["ru"]} for sentence in part["sentences"]],
                "sources": [
                    {
                        "citation_id": source.fragment.citation_id,
                        "label": source.label,
                        "locator": source.locator,
                        "excerpt": source.excerpt,
                        "book": source.fragment.book,
                    }
                    for source in sources
                ],
            }
            for part, sources in zip(series["parts"], checked.parts)
        ],
        "glossary": [
            {key: str(entry.get(key) or "")[:400] for key in ("term_en", "explanation_en", "translation_ru")}
            for entry in glossary[:20]
            if isinstance(entry, dict)
        ],
        "terms": [],
        "based_on": [edition["book"] for edition in editions],
        # What the page shows about each book; the full record is in RevisionSource.
        "source_editions": [
            {key: edition[key] for key in ("book", "book_ru", "attribution", "links")} for edition in editions
        ],
    }


def save_links(revision, checked: CheckedAnswer):
    """EvidenceLink per valid source and RevisionSource per book."""
    from apps.workshop.models import EvidenceLink, RevisionSource

    for part_index, sources in enumerate(checked.parts):
        for source in sources:
            EvidenceLink.objects.create(
                revision=revision,
                kind="claim_support",
                support_type="quote",
                citation_id=source.fragment.citation_id,
                source_chunk=source.fragment.chunk,
                claim_text=source.excerpt,
                # "part" is the index in payload["parts"], from zero.
                locator_json={"part": part_index, "label": source.label, "locator": source.locator, "start": source.start, "end": source.end},
            )
    for document in _documents(checked):
        RevisionSource.objects.create(revision=revision, document=document, role="primary", provenance_json=edition_record(document))


def has_sources(payload) -> bool:
    """True for a revision made as an answer with sources."""
    return isinstance(payload, dict) and any(isinstance(part, dict) and part.get("sources") for part in payload.get("parts") or [])
