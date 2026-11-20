"""Editorial path for a ready-made text (stage B).

A text written outside the workshop becomes an immutable Revision without
any change to its sentences, translations, glossary or fork. It reaches the
library only through the existing gate ``publish_revision``: this module
never creates a Publication or a QualityReport. The report is produced by
the AI worker from a live reviewer call (apps.ai.services).

Network calls never happen here; all functions are DB-only.
"""

import hashlib
import json
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.db import transaction
from django.utils import timezone

from apps.operations.models import AuditEvent
from apps.workshop.models import Project, Revision, RevisionKnowledgeCard

from .models import KnowledgeCard, Publication

TEXT_SCHEMA = "editorial-text-1"
CARD_SCHEMA = "knowledge-card-1"
EDITORIAL_LOGIN = "editorial"
FORK_KEYS = ("yes", "no", "not_sure")

MIN_PARTS, MAX_PARTS = 3, 5
MAX_SENTENCES = 12
MAX_SENTENCE_CHARS = 600
MAX_TITLE_CHARS = 200
MAX_NOTE_CHARS = 1000
MAX_GLOSSARY = 20
# Editorial defaults from docs/spec/docs/content-quality.md. Diagnostic only.
PART_WORDS_ALLOWED = (45, 110)
LONG_SENTENCE_WORDS = 16
# The cover shows the title and the first two sentences on one screen. Longer
# ones still publish, but on a small phone the button may fall below the fold.
COVER_TITLE_CHARS = 48
COVER_LINE_CHARS = 64

# Fork after every part but the last (owner decision, Nov 2026): a closed
# question about a concrete case, the buttons say exactly Yes/No/Not sure and
# the question ends with "?". Applied to new submissions only; already
# published revisions are never re-checked by the gate.
FORK_LABELS = {"yes": "Yes", "no": "No", "not_sure": "Not sure"}
# Vocabulary gates at submission (control, not a promise of level A1):
# at least 90 % A1 words, every word above A2 must be in the text glossary,
# and no sentence longer than LONG_SENTENCE_WORDS words.
MIN_A1_SHARE = 90.0
CEFR_LIST_PATH = "content/wordlists/cefrj-vocabulary-profile-1.5.csv"

PAYLOAD_KEYS = {
    "title_en", "title_ru", "lead_ru", "parts", "glossary", "source_en",
    "simplification_note_ru", "authorship",
}
PART_KEYS = {"title_en", "title_ru", "sentences", "fork"}


class EditorialError(Exception):
    """A ready-made text or card cannot be accepted as submitted."""

    def __init__(self, messages):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__("; ".join(self.messages))


def canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


# --- deterministic checks -----------------------------------------------------


def _is_text(value, limit):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _is_slug(value):
    if not isinstance(value, str) or not 0 < len(value) <= 50:
        return False
    try:
        validate_slug(value)
    except ValidationError:
        return False
    return True


def _check_pair(pair, where, errors):
    if not isinstance(pair, dict) or set(pair) != {"en", "ru"}:
        errors.append(f"{where}: нужна пара en/ru без других полей")
        return
    for key in ("en", "ru"):
        if not _is_text(pair[key], MAX_SENTENCE_CHARS):
            errors.append(f"{where}: поле {key} пустое или длиннее {MAX_SENTENCE_CHARS} символов")


def _check_fork(fork, where, is_last_part, errors):
    if not isinstance(fork, dict) or set(fork) != {"title_en", "prompt", "options"}:
        errors.append(f"{where}: развилка должна содержать title_en, prompt и options")
        return
    if is_last_part:
        errors.append(f"{where}: развилка ведёт к следующей части и не может стоять после последней")
    if not _is_text(fork["title_en"], MAX_TITLE_CHARS):
        errors.append(f"{where}: пустой заголовок развилки")
    _check_pair(fork["prompt"], f"{where}, вопрос", errors)
    if isinstance(fork["prompt"], dict) and isinstance(fork["prompt"].get("en"), str) and not fork["prompt"]["en"].rstrip().endswith("?"):
        errors.append(f"{where}, вопрос: закрытый вопрос должен заканчиваться знаком вопроса")
    options = fork["options"]
    if not isinstance(options, list) or [o.get("key") if isinstance(o, dict) else None for o in options] != list(FORK_KEYS):
        errors.append(f"{where}: нужны ровно три ответа по порядку: yes, no, not_sure")
        return
    for option in options:
        if set(option) != {"key", "label_en", "en", "ru"}:
            errors.append(f"{where}, ответ {option.get('key')}: нужны key, label_en, en, ru")
            continue
        if option["label_en"] != FORK_LABELS[option["key"]]:
            errors.append(f"{where}, ответ {option['key']}: подпись кнопки должна быть ровно {FORK_LABELS[option['key']]!r}")
        if not _is_text(option["label_en"], 40):
            errors.append(f"{where}, ответ {option['key']}: пустая подпись кнопки")
        _check_pair({"en": option["en"], "ru": option["ru"]}, f"{where}, ответ {option['key']}", errors)


def check_editorial_payload(payload):
    """Structural check of a ready-made text.

    Returns (errors, diagnostics). Errors refuse the submission; the
    diagnostics (word counts, long sentences) are stored in the quality
    report and never block on their own: length is a signal, not a verdict
    (content-quality.md).
    """
    errors = []
    diagnostics = {"parts": [], "warnings_ru": []}
    if not isinstance(payload, dict):
        return ["payload должен быть объектом"], diagnostics
    unknown = sorted(set(payload) - PAYLOAD_KEYS)
    if unknown:
        errors.append(f"Неизвестные поля текста: {', '.join(unknown)}")
    for key in ("title_en", "title_ru"):
        if not _is_text(payload.get(key), MAX_TITLE_CHARS):
            errors.append(f"Поле {key} пустое или длиннее {MAX_TITLE_CHARS} символов")
    for key, limit in (("lead_ru", MAX_NOTE_CHARS), ("source_en", MAX_TITLE_CHARS), ("simplification_note_ru", MAX_NOTE_CHARS)):
        if key in payload and not _is_text(payload[key], limit):
            errors.append(f"Поле {key} пустое или длиннее {limit} символов")
    if payload.get("authorship", "ai") != "ai":
        errors.append("authorship: поддерживается только значение ai (текст написан и проверен ИИ)")

    parts = payload.get("parts")
    if not isinstance(parts, list) or not MIN_PARTS <= len(parts) <= MAX_PARTS:
        errors.append(f"Нужно от {MIN_PARTS} до {MAX_PARTS} частей")
        parts = parts if isinstance(parts, list) else []
    for number, part in enumerate(parts, start=1):
        where = f"Часть {number}"
        if not isinstance(part, dict):
            errors.append(f"{where}: должна быть объектом")
            continue
        unknown = sorted(set(part) - PART_KEYS)
        if unknown:
            errors.append(f"{where}: неизвестные поля {', '.join(unknown)}")
        if not _is_text(part.get("title_en"), MAX_TITLE_CHARS):
            errors.append(f"{where}: пустой заголовок title_en")
        if "title_ru" in part and not _is_text(part["title_ru"], MAX_TITLE_CHARS):
            errors.append(f"{where}: пустой заголовок title_ru")
        sentences = part.get("sentences")
        if not isinstance(sentences, list) or not 1 <= len(sentences) <= MAX_SENTENCES:
            errors.append(f"{where}: нужно от 1 до {MAX_SENTENCES} предложений")
            sentences = sentences if isinstance(sentences, list) else []
        words = 0
        long_sentences = 0
        for index, sentence in enumerate(sentences, start=1):
            _check_pair(sentence, f"{where}, предложение {index}", errors)
            if isinstance(sentence, dict) and isinstance(sentence.get("en"), str):
                count = len(sentence["en"].split())
                words += count
                long_sentences += count > LONG_SENTENCE_WORDS
        if number < len(parts) and "fork" not in part:
            errors.append(f"{where}: после непоследней части нужна развилка")
        if "fork" in part:
            _check_fork(part["fork"], f"{where}, развилка", number == len(parts), errors)
        diagnostics["parts"].append({"number": number, "sentences": len(sentences), "words_en": words, "long_sentences": long_sentences})
        if sentences and not PART_WORDS_ALLOWED[0] <= words <= PART_WORDS_ALLOWED[1]:
            diagnostics["warnings_ru"].append(
                f"Часть {number}: {words} английских слов при ориентире {PART_WORDS_ALLOWED[0]}–{PART_WORDS_ALLOWED[1]}"
            )
        if long_sentences:
            diagnostics["warnings_ru"].append(f"Часть {number}: предложений длиннее {LONG_SENTENCE_WORDS} слов — {long_sentences}")

    glossary = payload.get("glossary", [])
    if not isinstance(glossary, list) or len(glossary) > MAX_GLOSSARY:
        errors.append(f"glossary: список не длиннее {MAX_GLOSSARY} слов")
        glossary = []
    for index, term in enumerate(glossary, start=1):
        if not isinstance(term, dict) or set(term) != {"term_en", "explanation_en", "translation_ru"}:
            errors.append(f"Слово {index}: нужны term_en, explanation_en, translation_ru")
            continue
        if not _is_text(term["term_en"], 100) or not _is_text(term["explanation_en"], 400) or not _is_text(term["translation_ru"], 100):
            errors.append(f"Слово {index}: пустое или слишком длинное поле")
    diagnostics["fork_count"] = sum(1 for part in parts if isinstance(part, dict) and "fork" in part)
    title = payload.get("title_en")
    if isinstance(title, str) and len(title) > COVER_TITLE_CHARS:
        diagnostics["warnings_ru"].append(f"Обложка: заголовок длиннее {COVER_TITLE_CHARS} знаков, на небольшом телефоне кнопка может уйти ниже первого экрана")
    first = parts[0].get("sentences") if parts and isinstance(parts[0], dict) else None
    for index, sentence in enumerate(first[:2] if isinstance(first, list) else [], start=1):
        if isinstance(sentence, dict) and isinstance(sentence.get("en"), str) and len(sentence["en"]) > COVER_LINE_CHARS:
            diagnostics["warnings_ru"].append(f"Обложка: предложение {index} длиннее {COVER_LINE_CHARS} знаков и займёт несколько строк")
    return errors, diagnostics


# --- vocabulary profile -------------------------------------------------------

CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")

# Irregular forms the list does not contain; the value is the listed base
# form. Only frequent shapes a simplified text is likely to use.
IRREGULAR_FORMS = {
    "men": "man", "women": "woman", "children": "child", "feet": "foot",
    "teeth": "tooth", "went": "go", "said": "say", "told": "tell",
    "took": "take", "came": "come", "made": "make", "got": "get",
    "knew": "know", "spoke": "speak", "ran": "run", "sat": "sit",
    "stood": "stand", "saw": "see", "fought": "fight", "thought": "think",
    "better": "good", "best": "good", "worse": "bad", "worst": "bad",
    "wrote": "write", "meant": "mean",
}


def _word_keys(word):
    """The look-up keys for a word form: the form itself plus simple
    regular shapes (plural -s, verb -s, -ed, -ing, -er/-est) and the
    irregular map. The CEFR-J list stores base forms, so "asks" must
    reach "ask" and "men" must reach "man".
    """
    keys = [word]
    if word in IRREGULAR_FORMS:
        keys.append(IRREGULAR_FORMS[word])
    if word.endswith("'s"):
        keys.append(word[:-2])
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 2:
        keys.append(word[:-1])          # asks -> ask, names -> name
        if word.endswith("ies"):
            keys.append(word[:-3] + "y")  # carries -> carry
        if word.endswith("es") and len(word) > 3:
            keys.append(word[:-2])        # places -> place
    if word.endswith("ed") and len(word) > 3:
        keys.extend([word[:-2], word[:-1]])  # stayed -> stay/staye
        if word.endswith("ied"):
            keys.append(word[:-3] + "y")
    if word.endswith("n't"):
        keys.append(word[:-3])          # doesn't -> does
    if word.endswith("ing") and len(word) > 4:
        keys.extend([word[:-3], word[:-3] + "e"])  # naming -> nam/name
    if word.endswith("er") and len(word) > 3:
        keys.append(word[:-2])
    if word.endswith("est") and len(word) > 4:
        keys.append(word[:-3])
    seen = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return seen


def load_cefr_list(path=None):
    """The CEFR-J vocabulary profile as {lowercased headword: lowest level}.

    A headword may appear several times (one row per part of speech); the
    level of a word is the lowest among its parts of speech. Proper nouns
    (rows where the headword starts with a capital letter) are not counted
    as vocabulary, so a reader is never refused because of a name.
    """
    import csv
    from pathlib import Path

    path = Path(path) if path else Path(__file__).resolve().parents[2] / CEFR_LIST_PATH
    if not path.is_file():
        raise EditorialError(f"Список словаря не найден: {path}")
    order = {level: index for index, level in enumerate(CEFR_LEVELS)}
    levels = {}
    with open(path, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            headword = (row.get("headword") or "").strip()
            level = (row.get("CEFR") or "").strip()
            if not headword or level not in order or headword[0].isupper():
                continue  # proper nouns are not vocabulary
            key = headword.split("/")[0].lower()
            if key not in levels or order[level] < order[levels[key]]:
                levels[key] = level
    return levels


# A capital letter marks a name only inside a sentence. After the end of a
# sentence, a colon or an opening quote the capital is
# spelling, and the word is checked on the list like any other.
_OPENING_MARKS = "\u201c\u2018\u00ab\u201e\u2039"
# A fork line is cut where a sentence really ends: a full stop, "?" or "!"
# followed by a capital letter. "He says “Why?” and turns back" and
# "and then... turns back" stay one sentence, so the length gate cannot
# be passed by a mark in the middle.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\u2026])[\"'\u201d\u2019\u00bb)]*\s+(?=[\"'\u201c\u2018\u00ab\u201e(]*[A-Z])")


def _starts_a_sentence(text, start, previous_end):
    """True when the word at ``start`` opens the text, a sentence or a quote."""
    if previous_end is None:
        return True
    gap = text[previous_end:start]
    if any(mark in gap for mark in ".!?:\u2026"):
        return True
    if any(mark in gap for mark in _OPENING_MARKS):
        return True
    # A straight quote opens when it stands right before the word and not
    # right after a letter: said "We, ("We, —"We. After a letter it closes.
    # The apostrophe is part of the word pattern, so it sits at ``start``.
    return bool(re.search(r"(^|[^A-Za-z0-9])\"$", gap)) or text[start] == "'"


def reader_lines(payload):
    """Every English line the reader gets, as (kind, text).

    ``sentence`` is one entry of a part: it is one sentence by the rules
    of the text file and is measured whole. ``fork`` is the question or an
    answer line of a fork; such a line is a small scene of several short
    sentences, so its length is measured sentence by sentence.
    """
    for part in payload.get("parts", []) or []:
        if not isinstance(part, dict):
            continue
        for sentence in part.get("sentences", []) or []:
            text = sentence.get("en") if isinstance(sentence, dict) else None
            if isinstance(text, str):
                yield "sentence", text
        fork = part.get("fork")
        if not isinstance(fork, dict):
            continue
        prompt = fork.get("prompt")
        if isinstance(prompt, dict) and isinstance(prompt.get("en"), str):
            yield "fork", prompt["en"]
        for option in fork.get("options", []) or []:
            if isinstance(option, dict) and isinstance(option.get("en"), str):
                yield "fork", option["en"]


def measured_sentences(payload):
    """The sentences the length gate measures, in reading order."""
    for kind, text in reader_lines(payload):
        if kind == "fork":
            for piece in _SENTENCE_SPLIT.split(text):
                if piece.strip():
                    yield piece.strip()
        else:
            yield text


def profile_vocabulary(payload, cefr_levels=None):
    """Word profile of the English lines: counts, shares, details.

    Counted: the sentences of the parts and the lines of the forks (the
    question and the three answer lines; the fixed button words Yes, No
    and Not sure are interface, not text). Words are lowercased and
    stripped of punctuation; contractions keep their apostrophe ("didn't"
    stays one word). Numbers are skipped.

    A capitalised word inside a sentence is a name or a title ("Politics",
    the book) and is treated as A1, so names never fail the gate. At the
    start of a sentence, after a colon and after an opening quote the
    capital is only spelling: the word is looked up on the
    list, and only a word the list does not know is taken for a name.

    Glossary words keep their level in the counts (the glossary is the
    reader's help, it does not hide the word): above_a1 and above_a2 name
    them with ``in_glossary`` so the command and the gates can tell them
    apart.
    """
    if cefr_levels is None:
        cefr_levels = load_cefr_list()
    glossary_terms = {
        term.get("term_en", "").strip().lower()
        for term in payload.get("glossary", []) or []
        if isinstance(term, dict)
    }
    # Words of multiword terms ("political animal") are glossed too: the
    # reader meets them inside the explained phrase.
    glossary_words = set()
    for term in glossary_terms:
        if " " in term:
            glossary_words.update(word for word in term.split() if word)
    order = {level: index for index, level in enumerate(CEFR_LEVELS)}
    counts = {level: 0 for level in CEFR_LEVELS}
    words_by_level = {level: {} for level in CEFR_LEVELS}
    unknown = {}
    total = 0
    fork_words = 0
    part_words = part_a1 = 0
    for kind, text in reader_lines(payload):
        previous_end = None
        first_word = None
        for match in re.finditer(r"[A-Za-z']+", text):
            raw = match.group().strip("'")
            if not raw:
                continue
            opens = _starts_a_sentence(text, match.start(), previous_end)
            previous_end = match.end()
            word = raw.lower()
            if first_word is None:
                first_word = word
            # The word that opens the line is a word wherever it is repeated
            # with a capital ("Abandon ... and then Abandon ..."): this was
            # the rule before, and the check must not get weaker.
            if raw[0].isupper() and not opens and word != first_word:
                # A capital inside a sentence is a name or a title — not
                # vocabulary to learn.
                level = "A1"
            else:
                # A form can list several base words ("means" is a noun
                # B2 and the verb mean A1): take the lowest level, the
                # same way a headword takes the lowest among its parts
                # of speech.
                found = [cefr_levels[key] for key in _word_keys(word) if key in cefr_levels]
                level = min(found, key=lambda item: order[item]) if found else None
                if level is None and raw[0].isupper():
                    level = "A1"  # a name the list does not know
            total += 1
            if kind == "fork":
                fork_words += 1
            else:
                part_words += 1
                part_a1 += level == "A1"
            if level is None:
                unknown[word] = unknown.get(word, 0) + 1
            else:
                counts[level] += 1
                words_by_level[level][word] = words_by_level[level].get(word, 0) + 1
    sentences = sorted(
        ((len(text.split()), text) for text in measured_sentences(payload)),
        key=lambda item: -item[0],
    )
    longest = sentences[0][1] if sentences else ""
    share = {level: (100.0 * count / total if total else 0.0) for level, count in counts.items()}
    above_a1 = {}
    for level in CEFR_LEVELS[1:]:
        for word, count in words_by_level[level].items():
            above_a1[word] = {"level": level, "count": count, "in_glossary": any(key in glossary_terms or key in glossary_words for key in _word_keys(word))}
    for word, count in unknown.items():
        above_a1[word] = {"level": None, "count": count, "in_glossary": any(key in glossary_terms or key in glossary_words for key in _word_keys(word))}
    above_a2 = {
        word: info for word, info in above_a1.items()
        if info["level"] is None or order[info["level"]] > order["A2"]
    }
    return {
        "total_words": total,
        "fork_words": fork_words,
        # The A1 share of the parts alone: the easy fork lines must not
        # pull a hard text over the gate.
        "parts_a1_share": 100.0 * part_a1 / part_words if part_words else 0.0,
        "levels": counts,
        "words_by_level": words_by_level,
        "share": share,
        "unknown_words": unknown,
        "above_a1": above_a1,
        "above_a2": above_a2,
        "longest_sentence": longest,
        "sentences": sentences,
    }


def vocabulary_errors(payload, cefr_levels=None):
    """The three submission gates: control, not a promise of level A1.

    Checked are the sentences of the parts and the fork lines (question
    and answers). Refuses the submission when fewer than MIN_A1_SHARE % of
    the words are A1 (in the parts alone or in the whole text), when a word above A2 is missing from the text glossary, or when a
    sentence is longer than LONG_SENTENCE_WORDS words. Returns Russian
    error messages; an empty list means the text passes.
    """
    profile = profile_vocabulary(payload, cefr_levels=cefr_levels)
    errors = []
    if profile["total_words"]:
        # Two shares, both must hold: the parts alone (as before the fork
        # lines were counted) and the whole text with the forks.
        parts_share, a1_share = profile["parts_a1_share"], profile["share"]["A1"]
        if profile["total_words"] > profile["fork_words"] and parts_share < MIN_A1_SHARE:
            errors.append(f"Словарь: только {parts_share:.0f} % слов уровня A1 в частях, нужно не меньше {MIN_A1_SHARE:.0f} %")
        elif a1_share < MIN_A1_SHARE:
            errors.append(f"Словарь: только {a1_share:.0f} % слов уровня A1 вместе с развилками, нужно не меньше {MIN_A1_SHARE:.0f} %")
    uncovered = sorted(
        word for word, info in profile["above_a2"].items()
        if not info["in_glossary"]
    )
    if uncovered:
        shown = ", ".join(uncovered[:10])
        more = "" if len(uncovered) <= 10 else f" (и ещё {len(uncovered) - 10})"
        errors.append(f"Словарь: слова выше A2 без словарика — {shown}{more}")
    longest = profile["longest_sentence"]
    if len(longest.split()) > LONG_SENTENCE_WORDS:
        errors.append(f"Словарь: предложение длиннее {LONG_SENTENCE_WORDS} слов: {longest}")
    return errors


# --- knowledge cards ----------------------------------------------------------


def card_snapshot(card):
    """What the reviewer receives and what the report is bound to."""
    entries = [entry for entry in card.facts_json or [] if isinstance(entry, dict)]
    return {
        "slug": card.slug,
        "version": card.version,
        "title": card.title,
        "sources": [entry for entry in entries if entry.get("type") == "source"],
        "facts": [entry for entry in entries if entry.get("type") != "source"],
        "pitfalls": card.pitfalls_json,
    }


def card_version_record(card):
    return {"slug": card.slug, "version": card.version, "content_hash": canonical_hash(card_snapshot(card))}


def verified_cards(revision):
    return list(
        KnowledgeCard.objects.filter(
            revisionknowledgecard__revision=revision,
            verification_status=KnowledgeCard.Status.VERIFIED,
        ).distinct().order_by("slug", "version")
    )


def card_version_records(revision):
    return [card_version_record(card) for card in verified_cards(revision)]


def load_card(data, *, actor="cli"):
    """Create one immutable card version from a content file.

    A card version never changes: different content under the same
    slug/version is refused, so a corrected card needs a new version, a new
    link and therefore a new review. The verification recorded in the file
    (who checked, against what) is copied into the audit log.
    """
    if not isinstance(data, dict) or data.get("schema") != CARD_SCHEMA:
        raise EditorialError(f"Карточка: ожидается schema={CARD_SCHEMA}")
    slug, version, title = data.get("slug"), data.get("version"), data.get("title")
    facts, pitfalls = data.get("facts"), data.get("pitfalls", [])
    verification = data.get("verification") or {}
    errors = []
    if not isinstance(verification, dict):
        raise EditorialError("Карточка: verification должен быть объектом")
    if not _is_slug(slug) or type(version) is not int or version < 1:
        errors.append("Карточка: нужны slug (латиница, цифры, дефис; до 50 символов) и целая version >= 1")
    if not _is_text(title, MAX_TITLE_CHARS):
        errors.append("Карточка: пустой title")
    if not isinstance(facts, list) or not facts:
        errors.append("Карточка: нужен непустой список facts")
    else:
        ids = [fact.get("fact_id") if isinstance(fact, dict) else None for fact in facts]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            errors.append("Карточка: у каждого положения должен быть уникальный fact_id")
        if any(not isinstance(fact, dict) or not _is_text(fact.get("text"), 600) or not _is_text(fact.get("locator"), 80) for fact in facts):
            errors.append("Карточка: у каждого положения нужны text и locator (место в первоисточнике)")
        if any(isinstance(fact, dict) and "type" in fact for fact in facts):
            errors.append("Карточка: поле type в положении зарезервировано")
    if not isinstance(pitfalls, list) or any(not _is_text(p, 600) for p in pitfalls):
        errors.append("Карточка: pitfalls — список непустых строк")
    status = verification.get("status", KnowledgeCard.Status.DRAFT)
    if status not in (KnowledgeCard.Status.DRAFT, KnowledgeCard.Status.VERIFIED):
        errors.append("Карточка: verification.status — draft или verified")
    sources = data.get("sources") or []
    if not isinstance(sources, list) or any(not isinstance(source, dict) or not _is_text(source.get("title"), 300) or "type" in source for source in sources):
        errors.append("Карточка: sources — список объектов с title (поле type зарезервировано)")
        sources = []
    if status == KnowledgeCard.Status.VERIFIED and not (verification.get("method") and verification.get("checked_on") and sources):
        errors.append("Карточка: для verified нужны sources, verification.method и verification.checked_on")
    if errors:
        raise EditorialError(errors)

    # The card model has one JSON list: sources and facts are stored in it
    # as typed entries, so the checked URLs stay with the card.
    facts_json = [{"type": "source", **source} for source in sources] + [{"type": "fact", **fact} for fact in facts]
    with transaction.atomic():
        card = KnowledgeCard.objects.select_for_update().filter(slug=slug, version=version).first()
        if card is not None:
            same = card.title == title and card.facts_json == facts_json and card.pitfalls_json == pitfalls
            if not same:
                raise EditorialError(f"Карточка {slug} v{version} уже существует с другим содержимым: увеличьте version")
            if card.verification_status == KnowledgeCard.Status.RETIRED:
                raise EditorialError(f"Карточка {slug} v{version} выведена из употребления")
            if card.verification_status == status or status == KnowledgeCard.Status.DRAFT:
                return card, False
        else:
            card = KnowledgeCard(slug=slug, version=version, title=title, facts_json=facts_json, pitfalls_json=pitfalls)
        card.verification_status = status
        card.verified_at = timezone.now() if status == KnowledgeCard.Status.VERIFIED else None
        card.save()
        AuditEvent.objects.create(
            actor_reference=actor,
            action="knowledge_card_loaded",
            object_reference=f"knowledge_card:{slug}:v{version}",
            reason=(verification.get("note_ru") or "Карточка загружена из файла")[:255],
            sanitized_metadata={
                "status": status,
                "method": verification.get("method", ""),
                "checked_on": verification.get("checked_on", ""),
                "content_hash": card_version_record(card)["content_hash"],
            },
        )
    return card, True


# --- submission ---------------------------------------------------------------


def editorial_owner():
    """Inactive system account that owns editorial projects (as in seed_demo)."""
    User = get_user_model()
    owner = User.objects.filter(login=EDITORIAL_LOGIN).first()
    if owner is None:
        owner = User.objects.create_user(login=EDITORIAL_LOGIN, password=None, is_active=False)
    if owner.is_active or owner.has_usable_password():
        # Never file editorial texts under an account somebody can log into.
        raise EditorialError(f"Учётная запись {EDITORIAL_LOGIN} должна быть неактивной и без пароля")
    return owner


def editorial_project(slug):
    owner = editorial_owner()
    title = f"Editorial: {slug}"[:200]
    project = Project.objects.filter(owner=owner, title=title).order_by("created_at").first()
    if project is None:
        project = Project.objects.create(owner=owner, title=title, mode=Project.Mode.EXPLAIN)
    return project


def submit_text(data, *, actor="cli"):
    """Store a ready-made text as an immutable revision. Returns (revision, created).

    The payload is saved exactly as submitted: no model call, no rewriting,
    no normalisation. Re-submitting the same content returns the existing
    revision; changed content becomes a new revision without a quality
    report, so it cannot be published before a new live review.
    """
    if not isinstance(data, dict) or data.get("schema") != TEXT_SCHEMA:
        raise EditorialError(f"Текст: ожидается schema={TEXT_SCHEMA}")
    slug = data.get("slug")
    if not _is_slug(slug):
        raise EditorialError("Текст: slug — латинские буквы, цифры, дефис и подчёркивание, до 50 символов")
    payload = data.get("payload")
    errors, _ = check_editorial_payload(payload)
    # Vocabulary gates apply at submission only: already stored revisions
    # (and published texts) are never re-checked.
    if isinstance(payload, dict):
        try:
            errors += vocabulary_errors(payload)
        except EditorialError as exc:
            # No word list means no vocabulary check: say so and refuse,
            # instead of letting the text through unchecked.
            errors.append(f"Словарь: проверка невозможна — {exc}")
    if "cover" in data and data["cover"] not in Publication.CoverTone.values:
        errors.append(f"Текст: cover — один из тонов {', '.join(Publication.CoverTone.values)}")
    card_refs = data.get("knowledge_cards") or []
    cards = []
    if not isinstance(card_refs, list) or not card_refs:
        errors.append("Текст: нужна хотя бы одна карточка знаний в knowledge_cards")
    else:
        for ref in card_refs:
            card = KnowledgeCard.objects.filter(slug=ref.get("slug"), version=ref.get("version")).first() if isinstance(ref, dict) else None
            if card is None:
                errors.append(f"Карточка не найдена: {ref}")
            else:
                cards.append(card)
        if len({card.slug for card in cards}) != len(cards):
            errors.append("Текст: одна карточка указана в двух версиях")
    if errors:
        raise EditorialError(errors)

    with transaction.atomic():
        project = Project.objects.select_for_update().get(pk=editorial_project(slug).pk)
        content_hash = canonical_hash(payload)
        revision = project.revisions.filter(content_hash=content_hash).first()
        created = revision is None
        if created:
            revision = Revision.objects.create(project=project, parent=project.head_revision, payload=payload)
        if project.head_revision_id != revision.pk:
            project.head_revision = revision
            project.save(update_fields=["head_revision"])
        # The revision's cards are exactly the ones named in the file. A
        # changed list makes earlier quality reports stale (the gate compares
        # the card versions a report was made with).
        wanted = {card.pk for card in cards}
        stale = RevisionKnowledgeCard.objects.filter(revision=revision).exclude(knowledge_card__in=wanted)
        removed = [f"{link.knowledge_card.slug}:v{link.knowledge_card.version}" for link in stale.select_related("knowledge_card")]
        stale.delete()
        added = []
        for card in cards:
            _, linked = RevisionKnowledgeCard.objects.get_or_create(revision=revision, knowledge_card=card)
            if linked and not created:
                added.append(f"{card.slug}:v{card.version}")
        if removed or added:
            AuditEvent.objects.create(
                actor_reference=actor,
                action="editorial_cards_changed",
                object_reference=f"revision:{revision.pk}",
                reason="Список карточек ревизии изменён при подаче; нужна новая проверка",
                sanitized_metadata={"removed": removed, "added": added},
            )
        if created:
            AuditEvent.objects.create(
                actor_reference=actor,
                action="editorial_submit",
                object_reference=f"revision:{revision.pk}",
                reason="Готовый текст подан без изменений",
                sanitized_metadata={
                    "slug": slug,
                    "revision_hash": revision.content_hash,
                    "cards": [f"{card.slug}:v{card.version}" for card in cards],
                },
            )
    return revision, created


def cover_tone(data):
    """The cover tone named in the text file (outside the payload), default clay.

    The tone is presentation, not content: it is not part of the revision
    hash and changing it needs no new review.
    """
    value = data.get("cover") if isinstance(data, dict) else None
    return value if value in Publication.CoverTone.values else Publication.CoverTone.CLAY


def find_submitted_revision(data):
    """The revision that holds exactly this file's payload, or None."""
    project = Project.objects.filter(owner__login=EDITORIAL_LOGIN, title=f"Editorial: {data.get('slug')}"[:200]).first()
    if project is None:
        return None
    return project.revisions.filter(content_hash=canonical_hash(data.get("payload"))).first()
