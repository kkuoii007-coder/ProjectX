"""Chunks with page locators for a book whose text carries its own structure.

A book arrives as clean text plus the offsets where its pages and paragraphs
start (see perseus.py). It is cut on sentence boundaries, never across a
book, with no overlap: one passage belongs to one chunk and so has one
locator. A chunk closes at a paragraph break once it has the minimum size,
otherwise before the sentence that would take it past the maximum.

Locator of a chunk:
    {"work": "Politics", "book": 1, "bekker_start": "1252b",
     "bekker_end": "1253a",
     "segments": [{"bekker": "1252b", "start": 0},
                  {"bekker": "1253a", "start": 812}]}
`segments[i].start` is the offset inside the chunk text at which that page
starts, character-exact, also when the page turns in the middle of a
sentence.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass

# A sentence ends with . ? ! or a lacuna, optional closing marks, a space,
# and the next one opens with a capital (after optional opening marks).
_SENTENCE_BREAK = re.compile(r"[.?!…][’”)\]]*\s+(?=[“‘(\[]*[A-Z])")


@dataclass(frozen=True)
class StructuredChunk:
    ordinal: int
    text: str
    words: int
    locator: dict


def sentence_starts(text: str, paragraph_starts=()) -> list:
    starts = {0}
    starts.update(offset for offset in paragraph_starts if 0 <= offset < len(text))
    starts.update(match.end() for match in _SENTENCE_BREAK.finditer(text))
    return sorted(starts)


def _spans(book, min_words: int, max_words: int) -> list:
    """(start, end) character spans of the chunks of one book."""
    starts = sentence_starts(book.text, book.paragraph_starts)
    bounds = starts + [len(book.text)]
    paragraphs = set(book.paragraph_starts)
    sentences = [
        (bounds[i], bounds[i + 1], len(book.text[bounds[i]:bounds[i + 1]].split()))
        for i in range(len(starts))
    ]
    groups = []
    current = []
    count = 0
    for sentence in sentences:
        start, _, words = sentence
        if current and (
            (count >= min_words and (start in paragraphs or count + words > max_words))
            or (count + words > max_words and words >= min_words)
        ):
            groups.append(current)
            current, count = [], 0
        current.append(sentence)
        count += words
    if current:
        groups.append(current)

    # A short tail joins the previous chunk; if that would be too long the
    # two are divided at the sentence nearest to the middle, preferring a
    # division that leaves both halves at least the minimum size.
    if len(groups) > 1 and sum(words for _, _, words in groups[-1]) < min_words:
        merged = groups[-2] + groups[-1]
        total = sum(words for _, _, words in merged)
        if total <= max_words:
            groups[-2:] = [merged]
        else:
            running = 0
            best_index, best_key = 1, None
            for index, (_, _, words) in enumerate(merged[:-1], start=1):
                running += words
                both_fit = min(running, total - running) >= min_words
                key = (not both_fit, abs(total - 2 * running))
                if best_key is None or key < best_key:
                    best_index, best_key = index, key
            groups[-2:] = [merged[:best_index], merged[best_index:]]
    return [(group[0][0], group[-1][1]) for group in groups]


def _locator(book, start: int, end: int, work: str) -> dict:
    offsets = [offset for _, offset in book.pages]
    first = bisect_right(offsets, start) - 1
    segments = [{"bekker": book.pages[first][0], "start": 0}]
    for name, offset in book.pages[first + 1:]:
        if offset >= end:
            break
        segments.append({"bekker": name, "start": offset - start})
    return {
        "work": work,
        "book": book.number,
        "bekker_start": segments[0]["bekker"],
        "bekker_end": segments[-1]["bekker"],
        "segments": segments,
    }


def build_chunks(books, *, work: str, min_words: int, max_words: int) -> list:
    chunks = []
    for book in books:
        for start, end in _spans(book, min_words, max_words):
            text = book.text[start:end].rstrip()
            chunks.append(
                StructuredChunk(
                    ordinal=len(chunks),
                    text=text,
                    words=len(text.split()),
                    locator=_locator(book, start, start + len(text), work),
                )
            )
    return chunks


def size_distribution(chunks, min_words: int, max_words: int) -> dict:
    """Word-size summary of the chunks for the import report."""
    sizes = sorted(chunk.words for chunk in chunks)
    if not sizes:
        return {"count": 0}
    middle = len(sizes) // 2
    median = sizes[middle] if len(sizes) % 2 else (sizes[middle - 1] + sizes[middle]) // 2
    step = max(1, (max_words - min_words) // 3)
    edges = [min_words + step * i for i in range(3)] + [max_words + 1]
    buckets = {f"<{min_words}": sum(1 for size in sizes if size < min_words)}
    for low, high in zip(edges, edges[1:]):
        buckets[f"{low}–{high - 1}"] = sum(1 for size in sizes if low <= size < high)
    buckets[f">{max_words}"] = sum(1 for size in sizes if size > max_words)
    return {"count": len(sizes), "min": sizes[0], "median": median, "max": sizes[-1], "buckets": buckets}
