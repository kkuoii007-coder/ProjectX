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
    # Words of the question the text half could not look for: an English
    # word that is nowhere on the shelf, a Russian word the lexicon of the
    # book does not translate. They lower `fts_coverage`.
    missing_terms: list = field(default_factory=list)
    unmapped_terms: list = field(default_factory=list)

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
            "missing_terms": list(self.missing_terms),
            "unmapped_terms": list(self.unmapped_terms),
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
        missing_terms=list(result.missing_terms),
        unmapped_terms=list(result.unmapped_terms),
    )


# --- verbatim comparison -----------------------------------------------------------

# Marks that look like an apostrophe; inside a word they are one.
_APOSTROPHE_LIKE = "'’‘‚‛＇`´"
# Every other quotation mark, of any language and direction.
_DOUBLE_LIKE = "\"“”„‟«»‹›＂「」『』〝〞〟"
_QUOTE_CHARS = _APOSTROPHE_LIKE + _DOUBLE_LIKE
_DASH_CHARS = "-‐‑‒–—―−"
_EDGE_PUNCTUATION = " .,;:!?…"
# An apostrophe or a hyphen between two letters is a part of the word.
_WORD_JOINERS = "'’＇-‐‑"


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


def _wordish(text: str, at: int) -> bool:
    return 0 <= at < len(text) and (text[at].isalnum() or text[at] == "_")


def _cuts_a_word(text: str, start: int, end: int) -> bool:
    """True when text[start:end] begins or ends inside a word: «an is by
    nature a political anim» is not a passage of the book."""
    def joined(at, neighbour):
        return 0 <= at < len(text) and text[at] in _WORD_JOINERS and _wordish(text, neighbour)

    if _wordish(text, start) and (_wordish(text, start - 1) or joined(start - 1, start - 2)):
        return True
    return _wordish(text, end - 1) and (_wordish(text, end) or joined(end, end + 1))


class _Prepared:
    """A text ready for the word-for-word search."""

    def __init__(self, text: str):
        self.text = text
        self.normalized, self.positions = _normalize(text)

    def find(self, wanted: str):
        """(start, end) in characters of the text for the first place where
        the normalized `wanted` stands as whole words, or None."""
        if not wanted:
            return None
        at = self.normalized.find(wanted)
        while at >= 0:
            start, end = self.positions[at], self.positions[at + len(wanted) - 1] + 1
            if not _cuts_a_word(self.text, start, end):
                return start, end
            at = self.normalized.find(wanted, at + 1)
        return None


def find_verbatim(text: str, quote: str):
    """(start, end) of `quote` inside `text` in characters of `text`, or
    None. The quote starts and ends at the border of a word."""
    wanted, _ = _normalize(quote)
    return _Prepared(text).find(wanted)


# --- words in quotation marks ------------------------------------------------------
#
# Whatever a reader would take for a quotation is checked. Marks of every
# kind pair with each other (“…", »…«, „…“, ‘…', backticks, full-width and
# CJK marks); a mark without its pair does not move the pairs of the others,
# and the words it leaves hanging are treated as quoted. When in doubt the
# phrase is a quotation: the cost of a false alarm is one correction by the
# model, the cost of a miss is an invented quotation on the page.

_OPENING_SHAPE = "“„‟«‹「『〝‘‚‛`"
_CLOSING_SHAPE = "”»›」』〞〟’´"
_OPENING_PUNCTUATION = "([{" + _DASH_CHARS
_CLOSING_PUNCTUATION = ".,;:!?…)]}" + _DASH_CHARS


def _mark_role(text: str, at: int):
    """What the quotation mark at `at` does: "open", "close", "" (cannot be
    told from the text) or None for an apostrophe inside a word."""
    char = text[at]
    before = text[at - 1] if at else ""
    after = text[at + 1] if at + 1 < len(text) else ""
    if char in _APOSTROPHE_LIKE and _wordish(text, at - 1) and _wordish(text, at + 1):
        return None  # it’s, man’s
    space_before, space_after = not before or before.isspace(), not after or after.isspace()
    if space_before != space_after:
        return "open" if space_before else "close"
    if not space_before:
        # No space on either side: ("word"), “‘word’”, word"word.
        left_edge = before in _OPENING_PUNCTUATION or before in _QUOTE_CHARS
        right_edge = after in _CLOSING_PUNCTUATION or after in _QUOTE_CHARS
        if left_edge != right_edge:
            return "open" if left_edge else "close"
    # « word », a mark that stands alone: only its shape is left.
    if char in _OPENING_SHAPE:
        return "open"
    if char in _CLOSING_SHAPE:
        return "close"
    return ""


def _double_quoted(text: str):
    """Phrases between marks that are not apostrophes."""
    opened, after_last = [], 0
    for at, char in enumerate(text):
        if char not in _DOUBLE_LIKE:
            continue
        role = _mark_role(text, at) or ("close" if opened else "open")
        if role == "open":
            opened.append(at)
        elif opened:
            yield [text[opened.pop() + 1 : at]]
            after_last = at + 1
        else:
            # A closing mark without an opening one: the words before it.
            yield [text[after_last:at]]
            after_last = at + 1
    for at in opened:
        yield [text[at + 1 :]]  # never closed: everything up to the end


def _possessive(text: str, at: int) -> bool:
    """`sons’ sons`, `the citizens’ rights`: this mark may be the apostrophe
    of a plural possessive and not the end of a quotation."""
    return at > 0 and text[at - 1] in "sSxXzZ" and at + 2 < len(text) and text[at + 1].isspace() and text[at + 2].isalpha()


def _single_quoted(text: str):
    """Phrases between marks that look like an apostrophe: lists of readings.

    The closing mark is also the apostrophe of a plural possessive, so
    ‘the sons and sons’ sons live together’ can be read two ways. A mark
    that cannot be a possessive ends the phrase for certain, and the whole
    phrase up to it is the quotation. If only possible possessives follow
    the opening mark, each of them is a reading and the caller accepts the
    phrase when one reading stands in the book.
    """
    opened, readings, after_last = None, [], 0
    for at, char in enumerate(text):
        if char not in _APOSTROPHE_LIKE:
            continue
        role = _mark_role(text, at)
        if role is None:
            continue
        if opened is None:
            if role == "close":
                if not _possessive(text, at):
                    yield [text[after_last:at]]  # a closing mark without an opening one
                    after_last = at + 1
                continue
            opened, readings = at, []
        elif role == "open":
            yield readings or [text[opened + 1 : at]]
            opened, readings = at, []
        elif _possessive(text, at):
            readings.append(text[opened + 1 : at])
        else:
            yield [text[opened + 1 : at]]
            opened, after_last = None, at + 1
    if opened is not None:
        yield readings or [text[opened + 1 :]]


def quoted_phrases(text: str) -> list:
    """Phrases of QUOTED_PHRASE_MIN_WORDS words or more that stand in
    quotation marks in `text`: a list of tuples of readings. A phrase has
    one reading unless its closing mark may be an apostrophe (see
    `_single_quoted`)."""
    found = []
    for readings in [*_double_quoted(text), *_single_quoted(text)]:
        cleaned = tuple(
            phrase
            for phrase in dict.fromkeys(reading.strip(_EDGE_PUNCTUATION + _QUOTE_CHARS) for reading in readings)
            if _word_count(phrase) >= QUOTED_PHRASE_MIN_WORDS
        )
        if cleaned and cleaned not in found:
            found.append(cleaned)
    return found


def _phrase_in_fragments(phrase: str, prepared: list) -> bool:
    wanted, _ = _normalize(phrase)
    # A quotation that opens a sentence may get a capital letter; nothing
    # else about it may differ from the book.
    variants = {wanted, wanted[:1].lower() + wanted[1:], wanted[:1].upper() + wanted[1:]}
    return any(text.find(variant) is not None for variant in variants for text in prepared)


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


def _is_text(value, limit=None) -> bool:
    return isinstance(value, str) and (limit is None or len(value) <= limit)


def _shape_errors(series) -> list:
    """What makes the answer unreadable for the rest of the check. The
    model's answer is untrusted data: a value of the wrong kind is an
    ordinary violation (the model gets one correction), never an exception."""
    if not isinstance(series, dict):
        return ["the answer is not an object"]
    errors = [f"{key} must be a string" for key in ("title_en", "title_ru") if not _is_text(series.get(key, ""))]
    parts = series.get("parts")
    if not isinstance(parts, list) or not MIN_PARTS <= len(parts) <= MAX_PARTS:
        return errors + [f"the answer must have {MIN_PARTS} to {MAX_PARTS} parts"]
    for number, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            errors.append(f"part {number}: must be an object")
            continue
        if not _is_text(part.get("title_en", "")):
            errors.append(f"part {number}: title_en must be a string")
        sentences = part.get("sentences")
        if not isinstance(sentences, list) or not 1 <= len(sentences) <= MAX_SENTENCES_PER_PART:
            errors.append(f"part {number}: 1 to {MAX_SENTENCES_PER_PART} sentences are required")
        elif any(
            not isinstance(sentence, dict) or any(not _is_text(sentence.get(key), MAX_SENTENCE_CHARS) or not sentence[key].strip() for key in ("en", "ru"))
            for sentence in sentences
        ):
            errors.append(f"part {number}: every sentence needs a non-empty en and ru of at most {MAX_SENTENCE_CHARS} characters")
        sources = part.get("sources")
        if not isinstance(sources, list):
            errors.append(f"part {number}: sources must be a list")
        elif any(not isinstance(item, dict) or not _is_text(item.get("id")) or not _is_text(item.get("quote")) for item in sources):
            errors.append(f"part {number}: every source must be an object with a string id and a string quote")
    glossary = series.get("glossary", [])
    if not isinstance(glossary, list) or any(
        not isinstance(entry, dict) or any(not _is_text(value) for value in entry.values()) for entry in glossary
    ):
        errors.append("glossary must be a list of objects with string values")
    return errors


def check_answer(series, supports: list) -> CheckedAnswer:
    """The structural check of a model answer against the supports.

    Rules (each violation rejects the whole answer):
    1. every source id is one of the ids shown to the model;
    2. a quote must stand word for word in the text of its fragment, start
       and end at the border of a word and have QUOTE_MIN_WORDS..
       QUOTE_MAX_WORDS words, otherwise that source is not valid; every
       part keeps at least one valid source;
    3. any phrase of three words or more in quotation marks in an English
       sentence stands word for word in one of the supports;
    4. a Russian sentence may have such a phrase only when its English
       sentence has a quotation that passed rule 3 (the Russian line is its
       translation and cannot be compared with the book);
    5. the titles and every field of the glossary have no such phrase at
       all: nothing there is compared with the book.
    """
    checked = CheckedAnswer()
    shape = _shape_errors(series)
    if shape:
        checked.violations = shape
        return checked
    by_id = {fragment.citation_id: fragment for fragment in supports}
    prepared = [_Prepared(fragment.chunk.text) for fragment in supports]

    def no_quotation(where: str, text: str):
        for readings in quoted_phrases(text):
            checked.violations.append(f"{where}: a phrase in quotation marks is not allowed here: “{readings[-1][:200]}”")

    def quotation_from_the_book(where: str, text: str) -> bool:
        """True when `text` has a phrase in quotation marks and every such
        phrase stands in the supports."""
        phrases = quoted_phrases(text)
        missing = [readings for readings in phrases if not any(_phrase_in_fragments(phrase, prepared) for phrase in readings)]
        for readings in missing:
            checked.violations.append(f"{where}: the words in quotation marks are not in the sources word for word: “{readings[-1][:200]}”")
        return bool(phrases) and not missing

    no_quotation("title_en", series.get("title_en", ""))
    no_quotation("title_ru", series.get("title_ru", ""))
    for number, part in enumerate(series["parts"], start=1):
        valid, reasons = [], []
        for item in part["sources"]:
            source_id, quote = item["id"], item["quote"]
            fragment = by_id.get(source_id)
            if fragment is None:
                checked.violations.append(f"part {number}: source id {source_id[:20]!r} is not one of the provided ids")
                continue
            if not quote.strip():
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
                reasons.append(f"{source_id}: the quote is not in the text of {source_id} word for word, as whole words: “{quote[:200]}”")
                continue
            excerpt = " ".join(fragment.chunk.text[span[0]:span[1]].split())
            if not any(known.fragment is fragment and known.start == span[0] and known.end == span[1] for known in valid):
                valid.append(ResolvedSource(fragment=fragment, start=span[0], end=span[1], excerpt=excerpt))
        if not valid:
            checked.violations.append(f"part {number}: no valid source" + (" (" + "; ".join(reasons) + ")" if reasons else ""))
        checked.parts.append(valid[:MAX_SOURCES_PER_PART])
        for key in ("title_en", "title_ru"):
            no_quotation(f"part {number} {key}", part.get(key, "") if _is_text(part.get(key, "")) else "")
        for position, sentence in enumerate(part["sentences"], start=1):
            where = f"part {number}, sentence {position}"
            from_the_book = quotation_from_the_book(where, sentence["en"])
            if not from_the_book:
                for readings in quoted_phrases(sentence["ru"]):
                    checked.violations.append(
                        f"{where}: the Russian line has words in quotation marks, but the English sentence has no quotation "
                        f"that stands in the sources: “{readings[-1][:200]}”"
                    )
    for entry in series.get("glossary", []):
        for key, value in entry.items():
            no_quotation(f"glossary {str(key)[:40]}", value)
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
