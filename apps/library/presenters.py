"""Presentation helpers: reshape stored revision payloads for templates.

Pure functions, no database access and no business rules. The payload is
treated as already validated by the AI pipeline; the helpers only tolerate
missing optional keys so a single odd record cannot break a page.
"""

SENTENCES_PER_PARAGRAPH = 5
SPECIMEN_MAX_CHARS = 160


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def reader_parts(payload):
    """Parts -> paragraphs of sentences with stable ids for aria-controls."""
    result = []
    for number, part in enumerate((payload or {}).get("parts") or [], start=1):
        if not isinstance(part, dict):
            continue
        sentences = [s for s in (part.get("sentences") or []) if isinstance(s, dict) and _text(s.get("en"))]
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
                "sentence_count": len(sentences),
            }
        )
    return result


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
    payload = payload or {}
    result = []
    for term in payload.get("terms") or []:
        if isinstance(term, dict) and _text(term.get("en")):
            result.append({"en": _text(term.get("en")), "ru": _text(term.get("ru")), "note": ""})
    for term in payload.get("glossary") or []:
        if isinstance(term, dict) and _text(term.get("term_en")):
            result.append({"en": _text(term.get("term_en")), "ru": _text(term.get("translation_ru")), "note": _text(term.get("explanation_en"))})
    return result
