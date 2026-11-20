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
- the author's name of the books in scope is left out: it is not in the text
  of the book. Words of the title stay ("What is politics?" is a question);
- words that only frame a question ("называет", "считает", "says") are left
  out: they tell what the author does, not what the question is about;
- a Russian word that is neither in a lexicon nor in the scope cannot be
  searched for. It is reported in `unmapped` and counts against `coverage`
  exactly as an English word found nowhere in the scope does: "Что такое
  свобода у Сартра?" asked of the Politics is mostly not about this book.
A lexicon stem matches the beginning of a word, never its middle.
An English word reached twice (from two Russian words) is counted once.
At most MAX_CONCEPTS significant words are used; the rest of a long query is
dropped.

Scores of a chunk:
- `raw`: BM25 (k1=1.2, b=0.75), idf and average length from the scope;
- `coverage`: 0..1, share of the question's concepts found in the chunk,
  each weighted by its idf. A concept absent from the whole scope
  ("Heidegger" asked of the Politics, an untranslated Russian word) keeps
  its weight and lowers it.

No dependencies beyond PostgreSQL full-text search; two short queries.
"""

import math
import re
from dataclasses import dataclass, field

from django.db import connection

K1 = 1.2
B = 0.75
# A longer query is cut: a tsquery of some twenty thousand words exhausts the stack
# of PostgreSQL ("stack depth limit exceeded").
MAX_CONCEPTS = 64
MAX_WORDS_SCANNED = 4 * MAX_CONCEPTS

_WORD = re.compile(r"[^\W_]+")
_CYRILLIC = re.compile(r"[а-яё]")
_RU_ENDING = re.compile(r"[аеиоуыьюяй]+$")

# Function words of a Russian question. They carry no subject, so they are
# neither searched for nor counted against the coverage. ("ё" is read as "е".)
RU_STOPWORDS = frozenset(
    """
    а без бы был была были было быть в вам вас во вот все вы где да для до его ее
    если есть еще же за зачем и из или им их к как какая какие какой какую каким какими
    каких какое кем кого когда кому которая которые который кто ли между мне мы на над не
    него нее нет ни них но о об он она они оно от по под почему при про с со так такая
    такие такое такой там то того тоже только том у уже чего чем чему что чтобы эта эти
    это этого этой этом этот я свой своя свое свои своей своих своим также именно очень
    """.split()
)
# Words that frame a question: what the author does ("называет", "считает",
# "критикует") or that something is needed or possible. The book does not
# contain them and the question is not about them.
RU_FRAME_WORDS = frozenset(
    """
    называет называют называл говорит говорят говорил сказал считает считают считал
    думает думают думал пишет пишут писал утверждает утверждают утверждал полагает полагал
    понимает понимают понимал объясняет объясняют объяснял критикует критикуют критиковал
    значит означает означают согласно мнению мнение мнения
    нужно нужен нужна нужны надо можно нельзя должен должна должно должны может могут
    """.split()
)
EN_FRAME_WORDS = frozenset(
    """
    say says said think thinks call calls called argue argues argued claim claims claimed
    believe believes believed consider considers considered
    criticise criticises criticised criticize criticizes criticized
    """.split()
)


@dataclass(frozen=True)
class Concept:
    source: str  # the word of the question
    terms: tuple  # what is searched for: the word itself, then its translations
    translated: bool = False  # a Russian word found in a lexicon


@dataclass
class TextSearch:
    """Result of the text half for one question."""

    # chunk id -> {"raw": BM25, "coverage": 0..1}
    scores: dict = field(default_factory=dict)
    # English (or same-language) words that went into the query.
    terms: list = field(default_factory=list)
    # Significant words found nowhere in the scope (they lower the coverage).
    missing: list = field(default_factory=list)
    # Russian words no lexicon translates and the scope does not contain
    # (they lower the coverage too).
    unmapped: list = field(default_factory=list)


def _ru_stem(word: str) -> str:
    stem = _RU_ENDING.sub("", word)
    return stem if len(stem) >= 4 else word


def scope_vocabulary(editions) -> dict:
    """Lexicon and authors' names of the editions in the search scope."""
    entries = []
    ignore_exact = set()
    ignore_ru = set()
    for edition in editions:
        entries.extend((edition.query_lexicon or {}).get("ru", []))
        work = edition.work
        ignore_exact.update(_WORD.findall(work.author.lower()))
        ignore_ru.update(_ru_stem(word) for word in _WORD.findall(work.author_ru.lower().replace("ё", "е")))
    return {"entries": entries, "ignore_exact": ignore_exact, "ignore_ru": ignore_ru}


def _translations(word: str, entries) -> list:
    english = []
    for entry in entries:
        if word in entry.get("words", ()) or any(word.startswith(stem) for stem in entry.get("stems", ())):
            english.extend(term for term in entry.get("en", ()) if term not in english)
    return english


def question_concepts(query: str, vocabulary=None, limit: int = MAX_WORDS_SCANNED) -> list:
    """Significant words of a question with their query alternatives.

    English stop words are still here (PostgreSQL knows them, see
    text_search, which keeps the first MAX_CONCEPTS words that are not), so
    `limit` is only a bound on how far a very long query is read.
    """
    vocabulary = vocabulary or {"entries": [], "ignore_exact": set(), "ignore_ru": set()}
    concepts = []
    seen = set()
    for match in _WORD.finditer(query.lower().replace("ё", "е")):
        if len(concepts) >= limit:
            break
        word = match.group()
        if word in seen:
            continue
        seen.add(word)
        if word in vocabulary["ignore_exact"] or word in EN_FRAME_WORDS:
            continue
        if not _CYRILLIC.search(word):
            concepts.append(Concept(word, (word,)))
            continue
        if word in RU_STOPWORDS or word in RU_FRAME_WORDS:
            continue
        if any(word.startswith(stem) for stem in vocabulary["ignore_ru"]):
            continue
        english = _translations(word, vocabulary["entries"])
        concepts.append(Concept(word, (word, *english), translated=bool(english)))
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
    claimed = set()
    for concept in concepts:
        lexemes = set().union(*(lexemes_of[term] for term in concept.terms))
        if concept.translated:
            # Two Russian words that lead to the same English words are one
            # concept, and a shared English word is weighed once.
            english = set().union(*(lexemes_of[term] for term in concept.terms[1:]))
            if english and not english - claimed:
                continue
        lexemes -= claimed
        if not lexemes:
            continue  # a stop word of PostgreSQL or a word already asked
        claimed |= lexemes
        groups.append((concept, frozenset(lexemes)))
        if len(groups) == MAX_CONCEPTS:
            break
    if not groups:
        return result
    wanted = sorted(claimed)

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
        if not found_in:
            # Absent from the whole scope: nothing to search for, but the
            # question asked about it, so it keeps its (largest) weight.
            (result.unmapped if is_russian and not concept.translated else result.missing).append(concept.source)
        idf = math.log(1 + (total - found_in + 0.5) / (found_in + 0.5))
        weighted.append((lexemes, idf))
        if found_in:
            result.terms.extend(
                term for term in concept.terms
                if lexemes_of[term] & lexemes and not (is_russian and term == concept.source) and term not in result.terms
            )
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
