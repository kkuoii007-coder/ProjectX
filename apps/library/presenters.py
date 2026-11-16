"""Presentation helpers: reshape stored revision payloads for templates.

Pure functions, no database access and no business rules. The payload is
treated as already validated by the AI pipeline; the helpers only tolerate
missing optional keys so a single odd record cannot break a page.
"""

SENTENCES_PER_PARAGRAPH = 5
SPECIMEN_MAX_CHARS = 160
COVER_LINES = 2
COVER_TONES = ("clay", "sea", "olive")
DEFAULT_TONE = "clay"
FORK_KEYS = ("yes", "no", "not_sure")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _list(value):
    return value if isinstance(value, list) else []


def reader_parts(payload):
    """Parts -> paragraphs of sentences with stable ids for aria-controls."""
    result = []
    payload = payload if isinstance(payload, dict) else {}
    for number, part in enumerate(_list(payload.get("parts")), start=1):
        if not isinstance(part, dict):
            continue
        sentences = [s for s in _list(part.get("sentences")) if isinstance(s, dict) and _text(s.get("en"))]
        paragraphs = []
        for start in range(0, len(sentences), SENTENCES_PER_PARAGRAPH):
            chunk = sentences[start:start + SENTENCES_PER_PARAGRAPH]
            paragraphs.append(
                [
                    {"en": _text(s.get("en")), "ru": _text(s.get("ru")), "id": f"gloss-{number}-{start + offset + 1}"}
                    for offset, s in enumerate(chunk)
                ]
            )
        result.append(
            {
                "number": number,
                "title_en": _text(part.get("title_en")),
                "title_ru": _text(part.get("title_ru")),
                "paragraphs": paragraphs,
                # One sentence per line: the reader shows `sentences`, the
                # paragraphs stay for older callers.
                "sentences": [s for paragraph in paragraphs for s in paragraph],
                "sentence_count": len(sentences),
                "fork": _fork(part.get("fork"), number),
            }
        )
    # A fork leads on to the next part; after the last part there is none.
    for index, part in enumerate(result):
        if part["fork"]:
            part["fork"]["next_part"] = result[index + 1]["number"] if index + 1 < len(result) else None
    return result


def _fork(fork, number):
    """The prepared question after a part, or None if the stored one is unusable.

    Nothing here is scored or saved: the three lines are prepared text and
    the reader only reveals the chosen one.
    """
    if not isinstance(fork, dict):
        return None
    prompt = fork.get("prompt")
    if not isinstance(prompt, dict) or not _text(prompt.get("en")):
        return None
    options = []
    for option in _list(fork.get("options")):
        if not isinstance(option, dict) or option.get("key") not in FORK_KEYS:
            continue
        if any(option["key"] == seen["key"] for seen in options):
            continue  # one line per choice: a repeated key would repeat an id
        if not _text(option.get("label_en")) or not _text(option.get("en")):
            continue
        options.append(
            {
                "key": option["key"],
                "label_en": _text(option.get("label_en")),
                "en": _text(option.get("en")),
                "ru": _text(option.get("ru")),
                "id": f"fork-{number}-{option['key'].replace('_', '-')}",
            }
        )
    if not options:
        return None
    return {
        "title_en": _text(fork.get("title_en")),
        "prompt": {"en": _text(prompt.get("en")), "ru": _text(prompt.get("ru")), "id": f"fork-{number}-prompt"},
        "options": options,
        "next_part": None,
    }


def titles(payload):
    """English title for the reading path, Russian one as its caption.

    Older texts have only a Russian title; then it stands in for both.
    """
    payload = payload if isinstance(payload, dict) else {}
    title_ru = _text(payload.get("title_ru"))
    title_en = _text(payload.get("title_en"))
    if title_en:
        return {"title_en": title_en, "title_ru": title_ru if title_ru != title_en else "", "title_lang": "en"}
    # `title_en` is the name of the slot the page shows; here it holds Russian.
    return {"title_en": title_ru, "title_ru": "", "title_lang": "ru" if title_ru else "en"}


def cover_lines(parts, count=COVER_LINES):
    """The first sentences of the text, shown on the cover exactly as written."""
    lines = []
    for part in parts:
        for sentence in part["sentences"]:
            lines.append({"en": sentence["en"], "ru": sentence["ru"], "id": f"cover-line-{len(lines) + 1}"})
            if len(lines) == count:
                return lines
    return lines


def tone(publication):
    value = getattr(publication, "cover_tone", "")
    return value if value in COVER_TONES else DEFAULT_TONE


def publication_item(publication):
    """One published text as a question: titles, tone and address."""
    from django.urls import reverse

    payload = publication.revision.payload
    payload = payload if isinstance(payload, dict) else {}
    item = {
        "publication": publication,
        **titles(payload),
        "tone": tone(publication),
        "url": reverse("library:read", args=[publication.slug]),
        "part_count": len([p for p in _list(payload.get("parts")) if isinstance(p, dict)]),
    }
    if not item["title_en"]:
        item["title_en"] = publication.slug  # never an empty heading or link
    return item


def about_text(payload):
    """Facts for the "About this text" block. Empty fields are not shown.

    The byline is stated only for texts marked as written by AI; a published
    one has also passed the AI review of the publication gate. Nothing here
    ever claims a human check.
    """
    payload = payload if isinstance(payload, dict) else {}
    notes = [_text(payload.get("simplification_note_ru")), _text(payload.get("note"))]
    return {
        "byline_en": "Written and checked by AI." if payload.get("authorship") == "ai" else "",
        "source_en": _text(payload.get("source_en")).rstrip("."),
        "note_ru": " ".join(note for note in notes if note),
    }


def outline(payload):
    """Small summary used on cards: parts with titles and counts."""
    parts = reader_parts(payload)
    return {
        "parts": [{"number": p["number"], "title_en": p["title_en"], "title_ru": p["title_ru"]} for p in parts],
        "part_count": len(parts),
        "sentence_count": sum(p["sentence_count"] for p in parts),
    }


def pick_specimen(parts):
    """Longest sentence (within a readable length) of the first part that has a translation."""
    for part in parts:
        candidates = [s for paragraph in part["paragraphs"] for s in paragraph if s["ru"] and len(s["en"]) <= SPECIMEN_MAX_CHARS]
        if candidates:
            sentence = max(candidates, key=lambda s: len(s["en"]))
            return {**sentence, "id": "gloss-specimen", "part_title": part["title_en"]}
    return None


def reader_terms(payload):
    """Unify seed-style `terms` and pipeline-style `glossary` into one list."""
    payload = payload if isinstance(payload, dict) else {}
    result = []
    for term in _list(payload.get("terms")):
        if isinstance(term, dict) and _text(term.get("en")):
            result.append({"en": _text(term.get("en")), "ru": _text(term.get("ru")), "note": ""})
    for term in _list(payload.get("glossary")):
        if isinstance(term, dict) and _text(term.get("term_en")):
            result.append({"en": _text(term.get("term_en")), "ru": _text(term.get("translation_ru")), "note": _text(term.get("explanation_en"))})
    return result
