"""Text half of the hybrid search: question -> concepts -> BM25 over tsvector.

A question is not a search phrase: "Why does Aristotle call man a political
animal?" must not require every word to be present. So the question is
reduced to concepts and a chunk matches if it has any of them (OR); the
order comes from BM25 computed over the search scope only.

Concept = one significant word of the question with its alternatives:
- an English word stands for itself (PostgreSQL drops its own stop words);
- a Russian word is looked up by its beginning in the lexicon of the books
  in scope (`SourceEdition.query_lexicon`, from the edition manifest) and
  becomes the English words listed there: "гражданином" -> citizen;
- the author's name and the title of the books in scope are left out: they
  are not in the text of the book;
- a Russian word that is neither in a lexicon nor in the scope is unknown to
  this half; it is reported in `unmapped` and takes no part in the scores.

Scores of a chunk:
- `raw`: BM25 (k1=1.2, b=0.75), idf and average length from the scope;
- `coverage`: 0..1, share of the question's concepts found in the chunk,
  each weighted by its idf. A concept absent from the whole scope
  ("Heidegger" asked of the Politics) keeps its weight and lowers it.

No dependencies beyond PostgreSQL full-text search; two short queries.
"""

import math
import re
from dataclasses import dataclass, field

from django.db import connection

K1 = 1.2
B = 0.75

_WORD = re.compile(r"[^\W_]+")
_CYRILLIC = re.compile(r"[а-яё]")
_RU_ENDING = re.compile(r"[аеиоуыьюяй]+$")

# Function words of a Russian question. Only bookkeeping: they keep the
# `unmapped` list meaningful, they never reach the query.
RU_STOPWORDS = frozenset(
    """
    а без бы был была были было быть в вам вас во вот все всё вы где да для до его ее её
    если есть ещё же за зачем и из или им их к как какая какие какой какую кем кого когда
    кому которая которые который кто ли между мне мы на над не него неё нет ни них но о об
    он она они оно от по под почему при про с со так такая такие такое такой там то того
    тоже только том у уже чего чем чему что чтобы эта эти это этого этой этом этот я
    """.split()
)


@dataclass(frozen=True)
class Concept:
    source: str
    terms: tuple


@dataclass
class TextSearch:
    """Result of the text half for one question."""

    # chunk id -> {"raw": BM25, "coverage": 0..1}
    scores: dict = field(default_factory=dict)
    # English (or same-language) words that went into the query.
    terms: list = field(default_factory=list)
    # Significant words found nowhere in the scope.
    missing: list = field(default_factory=list)
    # Russian words the text half could not translate.
    unmapped: list = field(default_factory=list)


def _ru_stem(word: str) -> str:
    stem = _RU_ENDING.sub("", word)
    return stem if len(stem) >= 4 else word


def scope_vocabulary(editions) -> dict:
    """Lexicon and ignored names of the editions in the search scope."""
    entries = []
    ignore_exact = set()
    ignore_ru = set()
    for edition in editions:
        entries.extend((edition.query_lexicon or {}).get("ru", []))
        work = edition.work
        ignore_exact.update(_WORD.findall(f"{work.author} {work.title}".lower()))
        ignore_ru.update(_ru_stem(word) for word in _WORD.findall(f"{work.author_ru} {work.title_ru}".lower()))
    return {"entries": entries, "ignore_exact": ignore_exact, "ignore_ru": ignore_ru}


def question_concepts(query: str, vocabulary=None) -> list:
    """Significant words of a question with their query alternatives."""
    vocabulary = vocabulary or {"entries": [], "ignore_exact": set(), "ignore_ru": set()}
    concepts = []
    seen = set()
    for word in _WORD.findall(query.lower().replace("ё", "е")):
        if word in seen:
            continue
        seen.add(word)
        if word in vocabulary["ignore_exact"]:
            continue
        if not _CYRILLIC.search(word):
            concepts.append(Concept(word, (word,)))
            continue
        if word in RU_STOPWORDS or any(word.startswith(stem) for stem in vocabulary["ignore_ru"]):
            continue
        english = []
        for entry in vocabulary["entries"]:
            if word in entry.get("words", ()) or any(word.startswith(stem) for stem in entry.get("stems", ())):
                english.extend(term for term in entry.get("en", ()) if term not in english)
        concepts.append(Concept(word, (word, *english)))
    return concepts


def _lexemes(terms) -> dict:
    """term -> set of lexemes of the 'english' configuration (stop words give none)."""
    result = {term: set() for term in terms}
    if not terms:
        return result
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT t.term, u.lexeme FROM unnest(%s::text[]) AS t(term), "
            "LATERAL unnest(to_tsvector('english', t.term)) AS u",
            [list(terms)],
        )
        for term, lexeme in cursor.fetchall():
            result[term].add(lexeme)
    return result


def _tsquery(lexemes) -> str:
    quoted = ("'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'" for lexeme in sorted(lexemes))
    return " | ".join(quoted)


def text_search(*, document_ids, query: str, vocabulary=None, limit: int = 16) -> TextSearch:
    """Rank the chunks of `document_ids` for a question; best `limit` chunks."""
    result = TextSearch()
    document_ids = list(document_ids)
    concepts = question_concepts(query, vocabulary)
    if not concepts or not document_ids:
        return result
    lexemes_of = _lexemes(sorted({term for concept in concepts for term in concept.terms}))
    groups = []
    for concept in concepts:
        lexemes = frozenset().union(*(lexemes_of[term] for term in concept.terms))
        if lexemes and all(lexemes != other for _, other in groups):
            groups.append((concept, lexemes))
    wanted = sorted(set().union(*(lexemes for _, lexemes in groups))) if groups else []
    if not wanted:
        return result

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*), coalesce(avg(token_count), 0) FROM sources_sourcechunk "
            "WHERE document_id = ANY(%s::uuid[])",
            [document_ids],
        )
        total, average = cursor.fetchone()
        cursor.execute(
            "SELECT c.id, c.token_count, u.lexeme, coalesce(cardinality(u.positions), 1) "
            "FROM sources_sourcechunk c, LATERAL unnest(c.search_vector) AS u "
            "WHERE c.document_id = ANY(%s::uuid[]) AND c.search_vector @@ %s::tsquery "
            "AND u.lexeme = ANY(%s::text[])",
            [document_ids, _tsquery(wanted), wanted],
        )
        rows = cursor.fetchall()
    average = float(average) or 1.0

    frequency = {}
    length = {}
    for chunk_id, token_count, lexeme, count in rows:
        frequency.setdefault(chunk_id, {})[lexeme] = count
        length[chunk_id] = token_count or 0

    weighted = []
    for concept, lexemes in groups:
        found_in = sum(1 for counts in frequency.values() if lexemes & counts.keys())
        is_russian = bool(_CYRILLIC.search(concept.source))
        if not found_in and is_russian:
            result.unmapped.append(concept.source)
            continue
        if not found_in:
            result.missing.append(concept.source)
        idf = math.log(1 + (total - found_in + 0.5) / (found_in + 0.5))
        weighted.append((lexemes, idf))
        result.terms.extend(term for term in concept.terms if lexemes_of[term] and not (is_russian and term == concept.source))
    total_weight = sum(idf for _, idf in weighted)
    if not total_weight:
        return result

    scored = []
    for chunk_id, counts in frequency.items():
        norm = K1 * (1 - B + B * length[chunk_id] / average)
        raw = 0.0
        covered = 0.0
        for lexemes, idf in weighted:
            count = sum(counts.get(lexeme, 0) for lexeme in lexemes)
            if count:
                raw += idf * count * (K1 + 1) / (count + norm)
                covered += idf
        if raw > 0:
            scored.append((raw, chunk_id, covered / total_weight))
    scored.sort(key=lambda item: (-item[0], item[1]))
    result.scores = {chunk_id: {"raw": raw, "coverage": coverage} for raw, chunk_id, coverage in scored[:limit]}
    return result
