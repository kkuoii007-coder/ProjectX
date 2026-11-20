"""PR 2 «книга на полке с указателями»: clean text of a shelf book, chunks
with Bekker locators, replacing an edition, the shelf as data, and a Russian
question that finds English text. No network and no paid call anywhere:
embeddings are a patched `embed_texts`."""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import uuid
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.sources import lexical, retrieval
from apps.sources.embeddings import EmbeddingError
from apps.sources.locators import format_locator, locate_quote, page_at
from apps.sources.models import Embedding, SourceChunk, SourceDocument, SourceEdition, SourceWork
from apps.sources.perseus import SourceFormatError, extract_perseus_tei
from apps.sources.retrieval import hybrid_search, index_document, search_shelf
from apps.sources.services import text_hash
from apps.sources.shelf import book_for_text, shelf_books
from apps.sources.shelf_import import ShelfImportError, import_shelf_book
from apps.sources.structured import build_chunks

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "shelf")
FIXTURE_MANIFEST = os.path.join(FIXTURES, "edition.json")
FIXTURE_XML = os.path.join(FIXTURES, "politics-1252b-1253a.xml")
BOOK_DIR = os.path.join(settings.BASE_DIR, "content", "sources", "aristotle-politics-rackham")
BOOK_MANIFEST = os.path.join(BOOK_DIR, "edition.json")
BOOK_XML = os.path.join(BOOK_DIR, "tlg0086.tlg035.perseus-eng2.xml")

ATTRIBUTION = (
    "Aristotle, Politics. Translated by H. Rackham. Loeb Classical Library (first published 1932). "
    "Digital text: Perseus Digital Library, from the 1944 printing. CC BY-SA 4.0."
)
GREEK = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")

# The eight control questions of the product owner, word for word, with the
# place in the book that answers each (checked against the text: the phrase
# stands on that page of the full file, see FullBookTests).
QUESTIONS = [
    ("Почему Аристотель называет человека политическим животным?", 1, {"1253a"}),
    ("Чем, по Аристотелю, государство отличается от семьи?", 1, {"1252a", "1252b"}),
    ("Что Аристотель говорит о рабстве «по природе»?", 1, {"1254a", "1254b", "1255a"}),
    ("Кого Аристотель считает гражданином?", 3, {"1275a", "1275b"}),
    ("Какие виды государственного устройства он различает и какие считает неправильными?", 3, {"1279a", "1279b"}),
    ("Почему Аристотель критикует общность имущества у Платона?", 2, {"1261a", "1261b", "1262a", "1262b", "1263a", "1263b"}),
    ("Почему Аристотель считает сильный средний слой полезным для государства?", 4, {"1295b", "1296a"}),
    ("Зачем, по Аристотелю, государству нужно общее воспитание?", 8, {"1337a"}),
]
OFF_SHELF_QUESTIONS = [
    "Что такое заброшенность у Хайдеггера?",
    "Что такое категорический императив у Канта?",
]
# Phrases that anchor the expected places above: (phrase, exact locator).
ANCHORS = [
    ("man is by nature a political animal", "Politics 1.1253a"),
    ("no difference between a large household and a small city", "Politics 1.1252a"),
    ("is by nature a slave", "Politics 1.1254a"),
    ("citizen pure and simple", "Politics 3.1275a"),
    ("tyranny corresponding to kingship", "Politics 3.1279b"),
    ("community of wives", "Politics 2.1262a"),
    ("the middle class", "Politics 4.1295b"),
    ("superintendence of this must be public", "Politics 8.1337a"),
]


def _no_embeddings(texts):
    raise EmbeddingError("not_configured", "нет ключа")


def _fake_vectors(texts):
    """Deterministic vectors: no network, the same text gives the same vector."""
    vectors = []
    for text in texts:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        vectors.append([seed[index % 32] / 255.0 - 0.5 for index in range(settings.AI_EMBEDDING_DIMENSIONS)])
    return vectors


def _tei(body: str) -> bytes:
    return (
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>'
        '<div type="textpart" subtype="book" n="1">' + body + "</div></body></text></TEI>"
    ).encode("utf-8")


def _page(name: str, inner: str) -> str:
    return f'<div type="textpart" subtype="bekker_page" n="{name}">{inner}</div>'


def _run(*args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    call_command(*args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def _copy_manifest(testcase, **changes):
    """A copy of the fixture book in a temp dir with a changed manifest."""
    folder = tempfile.mkdtemp()
    testcase.addCleanup(shutil.rmtree, folder, True)
    shutil.copy(FIXTURE_XML, folder)
    with open(FIXTURE_MANIFEST, encoding="utf-8") as handle:
        manifest = json.load(handle)
    for path, value in changes.items():
        target = manifest
        keys = path.split(".")
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value
    path = os.path.join(folder, "edition.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False)
    return path


def _make_ready(document):
    document.processing_state = SourceDocument.Processing.READY
    document.save(update_fields=["processing_state"])


def _editorial_document(text: str, state=SourceDocument.Processing.READY, supersedes=None):
    document = SourceDocument.objects.create(
        owner=None,
        origin=SourceDocument.Origin.EDITORIAL,
        visibility=SourceDocument.Visibility.EDITORIAL,
        content_hash=text_hash(text),
        normalized_text=text,
        processing_state=state,
        supersedes=supersedes,
    )
    SourceChunk.objects.create(
        document=document, ordinal=0, text=text, token_count=len(text.split()), content_hash=text_hash(text)
    )
    return document


class FixtureExtractionTests(SimpleTestCase):
    """«Готово, когда» 1: a cut of the real file, pages 1252b–1253a."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(FIXTURE_XML, "rb") as handle:
            cls.extraction = extract_perseus_tei(handle.read())
        cls.text = cls.extraction.text
        cls.chunks = build_chunks(cls.extraction.books, work="Politics", min_words=300, max_words=600)

    def test_notes_are_removed_whole(self):
        self.assertEqual(self.extraction.stats["notes_removed"], 10)
        for note_text in (
            "Uncertain: possibly a dagger",
            "Eur. IA 1400",
            "Hes. WD 405",
            "A law-giver of",
            "smoke-sharers",
            "hearth-fellows",
            "probably an interpolated note",
            "Hom. Il. 9.63",
            "the passage goes on",
        ):
            self.assertNotIn(note_text, self.text)

    def test_greek_is_removed(self):
        self.assertEqual(self.extraction.stats["greek_removed"], 2)
        self.assertEqual(self.extraction.stats["greek_in_text"], 0)
        self.assertIsNone(GREEK.search(self.text))

    def test_text_after_a_note_is_kept(self):
        # The tail of a removed <note> belongs to the translation.
        self.assertIn("the cutlers make the Delphic knife, in a niggardly way,", self.text)
        self.assertIn("Charondas speaks of as ‘meal-tub-fellows’ and the Cretan Epimenides as ‘manger-fellows.’", self.text)
        self.assertIn("man reviled by Homer, for one by nature unsocial is also ‘a lover of war’)", self.text)

    def test_verse_and_quoted_words_stay_as_text(self):
        self.assertIn("’Tis meet that Greeks should rule barbarians,—", self.text)
        self.assertIn("First and foremost a house and a wife and an ox for the ploughing—", self.text)
        self.assertIn("And each one giveth law To sons and eke to spouses—", self.text)
        self.assertIn("is the ‘house,’ the persons", self.text)

    def test_spaces_and_punctuation_are_normalised(self):
        self.assertNotIn("  ", self.text)
        self.assertIsNone(re.search(r"\s[,;.?!)]", self.text))
        self.assertIsNone(re.search(r"\(\s", self.text))
        self.assertNotIn("()", self.text)
        self.assertEqual(self.text, self.text.strip())
        # A line milestone between two words is the only word boundary there.
        self.assertEqual(self.extraction.stats["milestones_as_spaces"], 2)
        self.assertIn("foreign races are so still, because they were made up", self.text)
        self.assertIn("to each of us individually. For the whole must", self.text)

    def test_sentence_runs_across_the_page_turn(self):
        self.assertIn("its end, is its chief good; and self-sufficiency is an end, and a chief good.", self.text)
        book = self.extraction.books[0]
        self.assertEqual([name for name, _ in book.pages], ["1252b", "1253a"])
        offset = dict(book.pages)["1253a"]
        self.assertTrue(book.text[offset:].startswith("and self-sufficiency is an end"))
        self.assertTrue(book.text[:offset].endswith("its chief good; "))

    def test_every_chunk_has_a_locator(self):
        self.assertEqual(len(self.chunks), 3)
        for chunk in self.chunks:
            locator = chunk.locator
            self.assertEqual(locator["work"], "Politics")
            self.assertEqual(locator["book"], 1)
            self.assertEqual(locator["segments"][0], {"bekker": locator["bekker_start"], "start": 0})
            self.assertEqual(locator["segments"][-1]["bekker"], locator["bekker_end"])
            self.assertTrue(format_locator(locator).startswith("Politics 1.125"))
            self.assertLessEqual(chunk.words, 600)
            self.assertEqual(chunk.words, len(chunk.text.split()))
        self.assertEqual(
            [format_locator(chunk.locator) for chunk in self.chunks],
            ["Politics 1.1252b", "Politics 1.1252b–1253a", "Politics 1.1253a"],
        )

    def test_chunks_cut_on_sentences_without_overlap(self):
        self.assertEqual(" ".join(chunk.text for chunk in self.chunks), self.extraction.books[0].text)
        for chunk in self.chunks:
            self.assertRegex(chunk.text[0], r"[A-Z‘“(]")
            self.assertRegex(chunk.text, r"[.?!…][’”)]*$")

    def test_segment_offset_is_where_the_page_really_starts(self):
        chunk = self.chunks[1]
        start = chunk.locator["segments"][1]["start"]
        # In the middle of a sentence, not at its beginning.
        self.assertTrue(chunk.text[start:].startswith("and self-sufficiency is an end"))
        self.assertEqual(page_at(chunk.locator, start - 1), "1252b")
        self.assertEqual(page_at(chunk.locator, start), "1253a")

    def test_exact_page_of_a_quotation(self):
        chunk = self.chunks[1]
        self.assertEqual(
            locate_quote(chunk.text, chunk.locator, "while it comes into existence for the sake of life"),
            "Politics 1.1252b",
        )
        self.assertEqual(
            locate_quote(chunk.text, chunk.locator, "man is by nature a political animal"), "Politics 1.1253a"
        )
        self.assertEqual(
            locate_quote(chunk.text, chunk.locator, "its chief good; and\nself-sufficiency"), "Politics 1.1252b–1253a"
        )
        self.assertEqual(locate_quote(chunk.text, chunk.locator, "man is by nature a social animal"), "")


class ExtractionRulesTests(SimpleTestCase):
    """Rules of the extractor on small hand-made markup."""

    def test_dtd_and_entities_are_refused(self):
        data = b'<!DOCTYPE TEI [<!ENTITY x "boom">]>' + _tei(_page("1252a", "<p>Text &x;.</p>"))
        with self.assertRaises(SourceFormatError):
            extract_perseus_tei(data)

    def test_greek_in_the_translation_leaves_no_brackets(self):
        data = _tei(_page(
            "1252a",
            '<p>The citizens (<foreign xml:lang="grc">πολῖται</foreign>) rule , and the <quote xml:lang="grc">δῆμος</quote> people obey.</p>',
        ))
        extraction = extract_perseus_tei(data)
        self.assertEqual(extraction.text, "The citizens rule, and the people obey.")
        self.assertEqual(extraction.stats["greek_in_text"], 2)

    def test_gap_becomes_an_ellipsis(self):
        extraction = extract_perseus_tei(_tei(_page("1252a", "<p>One part<gap/>another part. And . . . more.</p>")))
        self.assertEqual(extraction.text, "One part … another part. And … more.")
        self.assertEqual(extraction.stats["gaps"], 1)

    def test_page_starts_at_its_milestone_not_at_its_div(self):
        data = _tei(
            _page("1285a", '<p><milestone unit="page" n="1285a"/>First page text, with a tail</p>')
            + _page("1285b", '<p>that belongs to it. <milestone unit="page" n="1285b"/>Second page.</p>')
        )
        extraction = extract_perseus_tei(data)
        book = extraction.books[0]
        self.assertEqual(book.text, "First page text, with a tail that belongs to it. Second page.")
        self.assertEqual(book.text[dict(book.pages)["1285b"]:], "Second page.")
        self.assertEqual(extraction.stats["pages_moved_from_div"], ["1.1285b"])

    def test_page_without_a_milestone_starts_with_its_div(self):
        data = _tei(
            _page("1252a", '<p><milestone unit="page" n="1252a"/>First.</p>') + _page("1252b", "<p>Second.</p>")
        )
        extraction = extract_perseus_tei(data)
        book = extraction.books[0]
        self.assertEqual(book.text[dict(book.pages)["1252b"]:], "Second.")
        self.assertEqual(extraction.stats["pages_without_milestone"], ["1.1252b"])

    def test_bekker_milestone_marked_as_section_is_a_page(self):
        # In the real file 1254b is marked unit="section": the name decides.
        data = _tei(
            _page("1254a", '<p><milestone unit="page" n="1254a"/>First, and</p>')
            + _page("1254b", '<p>so on. <milestone unit="section" n="1254b"/>Second.</p>')
        )
        book = extract_perseus_tei(data).books[0]
        self.assertEqual(book.text[dict(book.pages)["1254b"]:], "Second.")

    def test_chunk_never_crosses_a_book(self):
        sentence = "This is one plain sentence of the book. "
        body = (
            '<div type="textpart" subtype="book" n="1">' + _page("1252a", f"<p>{sentence * 12}</p>") + "</div>"
            '<div type="textpart" subtype="book" n="2">' + _page("1260b", f"<p>{sentence * 12}</p>") + "</div>"
        )
        data = f'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>{body}</body></text></TEI>'.encode()
        chunks = build_chunks(extract_perseus_tei(data).books, work="Politics", min_words=300, max_words=600)
        self.assertEqual([chunk.locator["book"] for chunk in chunks], [1, 2])
        self.assertEqual([chunk.ordinal for chunk in chunks], [0, 1])


class FullBookTests(SimpleTestCase):
    """«Готово, когда» 2: control places on the full file from the repository."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(BOOK_XML, "rb") as handle:
            cls.data = handle.read()
        with open(BOOK_MANIFEST, encoding="utf-8") as handle:
            cls.manifest = json.load(handle)
        cls.extraction = extract_perseus_tei(cls.data)
        cls.chunks = build_chunks(
            cls.extraction.books,
            work=cls.manifest["locator"]["work"],
            min_words=settings.SOURCE_CHUNK_WORDS_MIN,
            max_words=settings.SOURCE_CHUNK_WORDS_MAX,
        )

    def _places(self, quote):
        return [locate_quote(chunk.text, chunk.locator, quote) for chunk in self.chunks if quote in chunk.text]

    def test_file_is_the_one_named_in_the_manifest(self):
        self.assertEqual(hashlib.sha256(self.data).hexdigest(), self.manifest["source"]["sha256"])
        self.assertIn(self.manifest["source"]["commit"], self.manifest["edition"]["url"])

    def test_counts_of_the_real_file(self):
        stats = self.extraction.stats
        self.assertEqual(stats["notes_removed"], 587)
        self.assertEqual(stats["greek_removed"], 69)
        self.assertEqual(stats["greek_in_text"], 0)
        self.assertEqual(stats["gaps"], 5)
        self.assertEqual(stats["books"], 8)
        self.assertEqual(stats["pages"], 189)
        self.assertEqual(stats["pages_without_milestone"], [])
        self.assertEqual(len(stats["pages_moved_from_div"]), 22)
        self.assertEqual(stats["unknown_elements"], {})

    def test_clean_text(self):
        text = self.extraction.text
        self.assertIsNone(GREEK.search(text))
        self.assertNotIn("  ", text)
        self.assertIsNone(re.search(r"\s[,;.?!)]", text))
        self.assertNotIn("()", text)
        # 154 places of the file have a milestone and no space between words.
        self.assertEqual(self.extraction.stats["milestones_as_spaces"], 154)
        self.assertIn("that wealth", text)
        self.assertNotIn("thatwealth", text)

    def test_control_places(self):
        self.assertEqual(self._places("while it comes into existence for the sake of life"), ["Politics 1.1252b"])
        self.assertEqual(self._places("Etruscans and Carthaginians"), ["Politics 3.1280a"])
        self.assertEqual(self._places("any state that is truly so called"), ["Politics 3.1280b"])
        # The formula is repeated in book 3; the control place is the first.
        self.assertEqual(
            self._places("man is by nature a political animal"), ["Politics 1.1253a", "Politics 3.1278b"]
        )

    def test_page_boundary_follows_the_milestone(self):
        # In 22 places the div of a page is cut at a clause break near the
        # page milestone. The milestone is the page; these two are fixed here.
        self.assertEqual(self._places("As tyrant of the meek and"), ["Politics 3.1285a"])
        self.assertEqual(self._places("luckless city, And all did greatly praise him."), ["Politics 3.1285b"])
        self.assertEqual(
            self._places("As tyrant of the meek and luckless city"), ["Politics 3.1285a–1285b"]
        )
        self.assertEqual(self._places("the fundamental principle of the lawgiver"), ["Politics 2.1271a"])
        self.assertEqual(self._places("is one that Plato has made in the Laws."), ["Politics 2.1271b"])
        # 1254b is the page marked unit="section" in the file.
        self.assertEqual(self._places("since in those that are bad or in a bad condition"), ["Politics 1.1254b"])

    def test_anchors_of_the_control_questions(self):
        for quote, place in ANCHORS:
            with self.subTest(quote=quote):
                self.assertIn(place, self._places(quote))

    def test_chunks(self):
        low, high = settings.SOURCE_CHUNK_WORDS_MIN, settings.SOURCE_CHUNK_WORDS_MAX
        self.assertEqual([chunk.ordinal for chunk in self.chunks], list(range(len(self.chunks))))
        self.assertEqual(max(chunk.words for chunk in self.chunks), high)
        # Shorter than the minimum only where a book ends or one very long
        # sentence follows.
        self.assertLessEqual(sum(1 for chunk in self.chunks if chunk.words < low), 2)
        for book in self.extraction.books:
            own = [chunk for chunk in self.chunks if chunk.locator["book"] == book.number]
            self.assertEqual(" ".join(chunk.text for chunk in own), book.text)
            pages = [name for name, _ in book.pages]
            covered = [segment["bekker"] for chunk in own for segment in chunk.locator["segments"]]
            self.assertEqual([name for index, name in enumerate(covered) if index == 0 or covered[index - 1] != name], pages)
        for chunk in self.chunks:
            starts = [segment["start"] for segment in chunk.locator["segments"]]
            self.assertEqual(starts[0], 0)
            self.assertEqual(starts, sorted(set(starts)))
            self.assertLess(starts[-1], len(chunk.text))

    def test_lexicon_is_data_of_the_book(self):
        entries = self.manifest["query_lexicon"]["ru"]
        self.assertGreaterEqual(len(entries), 40)
        self.assertLessEqual(len(entries), 70)
        words = {form for entry in entries for form in entry.get("stems", []) + entry.get("words", [])}
        for name in ("хайдеггер", "кант", "платон"):
            self.assertFalse(any(form.startswith(name[:4]) for form in words), name)
        text = self.extraction.text.lower()
        for entry in entries:
            for term in entry["en"]:
                with self.subTest(term=term):
                    self.assertIn(term.lower(), text)
        self.assertEqual(self.manifest["related_texts"], ["why-do-people-live-in-cities"])


class LocatorFormatTests(SimpleTestCase):
    def test_one_page(self):
        locator = {"work": "Politics", "book": 1, "bekker_start": "1253a", "bekker_end": "1253a"}
        self.assertEqual(format_locator(locator), "Politics 1.1253a")

    def test_range_uses_an_en_dash(self):
        locator = {"work": "Politics", "book": 1, "bekker_start": "1252b", "bekker_end": "1253a"}
        self.assertEqual(format_locator(locator), "Politics 1.1252b–1253a")

    def test_empty_locator_gives_an_empty_string(self):
        self.assertEqual(format_locator({}), "")
        self.assertEqual(format_locator(None), "")
        self.assertEqual(locate_quote("Some text.", {}, "Some"), "")

    def test_attribution_line(self):
        work = SourceWork(author="Aristotle", title="Politics", author_ru="Аристотель", title_ru="Политика")
        edition = SourceEdition(
            work=work, translator="H. Rackham", series="Loeb Classical Library", first_published_year=1932,
            printing_year=1944, digital_publisher="Perseus Digital Library", license="CC BY-SA 4.0",
        )
        self.assertEqual(edition.attribution(), ATTRIBUTION)
        self.assertEqual(work.label, "Aristotle, Politics")
        self.assertEqual(work.label_ru, "Аристотель, «Политика»")

    def test_attribution_with_sparse_data(self):
        work = SourceWork(author="Plato", title="Republic")
        self.assertEqual(SourceEdition(work=work).attribution(), "Plato, Republic.")
        self.assertEqual(work.label_ru, "")
        self.assertEqual(
            SourceEdition(work=work, translator="B. Jowett", edition="Oxford, 1888").attribution(),
            "Plato, Republic. Translated by B. Jowett. Oxford, 1888.",
        )


@mock.patch.object(retrieval, "embed_texts", _no_embeddings)
class ShelfImportTests(TestCase):
    def _counts(self):
        return (
            SourceWork.objects.count(),
            SourceEdition.objects.count(),
            SourceDocument.objects.count(),
            SourceChunk.objects.count(),
            Embedding.objects.count(),
        )

    def test_import_writes_document_and_chunks_with_locators(self):
        out, _ = _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        self.assertIn(f"Документ создан: {document.pk}", out)
        self.assertIn("Фрагментов: 3", out)
        self.assertIn("Вырезано сносок: 10, греческих вставок: 2", out)
        self.assertIsNone(document.owner)
        self.assertEqual(document.origin, SourceDocument.Origin.EDITORIAL)
        self.assertEqual(document.visibility, SourceDocument.Visibility.EDITORIAL)
        self.assertEqual(document.rights_state, "allowed")
        self.assertEqual(document.processing_state, SourceDocument.Processing.UPLOADED)
        self.assertEqual(document.metadata_json["chunking"], "structured")
        self.assertEqual(document.metadata_json["stats"]["notes_removed"], 10)
        self.assertIsNone(GREEK.search(document.normalized_text))
        chunks = list(document.chunks.order_by("ordinal"))
        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertTrue(chunk.locator_json["segments"])
            self.assertEqual(chunk.content_hash, text_hash(chunk.text))
            self.assertEqual(chunk.token_count, len(chunk.text.split()))
        edition = document.edition
        self.assertEqual(edition.key, "fixture-politics-1252b-1253a")
        self.assertEqual(edition.attribution(), ATTRIBUTION)
        self.assertEqual(edition.license_url, "https://creativecommons.org/licenses/by-sa/4.0/")
        self.assertEqual(len(edition.source_sha256), 64)
        self.assertEqual(edition.related_texts, ["why-do-people-live-in-cities"])
        self.assertEqual(edition.work.key, "fixture-aristotle-politics")
        self.assertEqual(edition.work.title_ru, "Политика")

    def test_repeated_import_changes_nothing(self):
        """«Готово, когда» 3."""
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        with mock.patch.object(retrieval, "embed_texts", _fake_vectors):
            self.assertEqual(index_document(document)["state"], "ready")
        before = self._counts()
        self.assertEqual(before, (1, 1, 1, 3, 3))
        chunk_ids = list(SourceChunk.objects.order_by("id").values_list("id", "content_hash"))
        embeddings = [(row.id, row.source_chunk_id, list(row.vector)) for row in Embedding.objects.order_by("id")]

        out, _ = _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        self.assertIn(f"уже импортировано: {document.pk}", out)
        self.assertNotIn("Метаданные издания обновлены", out)
        self.assertEqual(self._counts(), before)
        self.assertEqual(list(SourceChunk.objects.order_by("id").values_list("id", "content_hash")), chunk_ids)
        self.assertEqual(
            [(row.id, row.source_chunk_id, list(row.vector)) for row in Embedding.objects.order_by("id")], embeddings
        )
        document.refresh_from_db()
        self.assertEqual(document.processing_state, SourceDocument.Processing.READY)

    def test_changed_manifest_updates_the_edition_only(self):
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        with mock.patch.object(retrieval, "embed_texts", _fake_vectors):
            index_document(document)
        before = self._counts()
        chunk_ids = list(SourceChunk.objects.order_by("id").values_list("id", flat=True))
        embedding_ids = list(Embedding.objects.order_by("id").values_list("id", flat=True))
        lexicon = {"ru": [{"stems": ["граждан"], "en": ["citizen"]}]}
        changed = _copy_manifest(
            self,
            **{
                "edition.translator": "Harris Rackham",
                "work.title_ru": "Политика (книга первая)",
                "related_texts": ["why-do-people-live-in-cities", "another-text"],
                "query_lexicon": lexicon,
            },
        )
        out, _ = _run("import_shelf_book", manifest=changed)
        self.assertIn(f"уже импортировано: {document.pk}", out)
        self.assertIn("Метаданные издания обновлены", out)
        for name in ("work.title_ru", "translator", "query_lexicon", "related_texts"):
            self.assertIn(name, out)
        self.assertEqual(self._counts(), before)
        self.assertEqual(list(SourceChunk.objects.order_by("id").values_list("id", flat=True)), chunk_ids)
        self.assertEqual(list(Embedding.objects.order_by("id").values_list("id", flat=True)), embedding_ids)
        edition = SourceEdition.objects.get()
        self.assertEqual(edition.translator, "Harris Rackham")
        self.assertIn("Translated by Harris Rackham.", edition.attribution())
        self.assertEqual(edition.related_texts, ["why-do-people-live-in-cities", "another-text"])
        self.assertEqual(edition.query_lexicon, lexicon)
        self.assertEqual(edition.work.title_ru, "Политика (книга первая)")
        document.refresh_from_db()
        self.assertEqual(document.processing_state, SourceDocument.Processing.READY)

    def test_file_that_differs_from_the_manifest_is_refused(self):
        wrong = _copy_manifest(self, **{"source.sha256": "0" * 64})
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=wrong)
        self.assertIn("sha256 файла не совпал с манифестом", str(caught.exception))
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))

    def test_missing_manifest_and_incomplete_manifest(self):
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=os.path.join(FIXTURES, "absent.json"))
        self.assertIn("Манифест не найден", str(caught.exception))
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=_copy_manifest(self, **{"edition.license": ""}))
        self.assertIn("license", str(caught.exception))

    def test_dry_run_writes_nothing(self):
        out, _ = _run("import_shelf_book", manifest=FIXTURE_MANIFEST, dry_run=True)
        self.assertIn("в базу ничего не записано", out)
        self.assertIn("Фрагментов: 3", out)
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))

    def test_print_id_leaves_only_the_uuid_on_stdout(self):
        out, err = _run("import_shelf_book", manifest=FIXTURE_MANIFEST, print_id=True)
        document = SourceDocument.objects.get()
        self.assertEqual(out.strip(), str(document.pk))
        self.assertIn("Документ создан", err)
        out, err = _run("import_shelf_book", manifest=FIXTURE_MANIFEST, print_id=True)
        self.assertEqual(out.strip(), str(document.pk))
        self.assertIn("уже импортировано", err)

    def test_supersedes_unknown_uuid_is_a_clear_error(self):
        missing = uuid.uuid4()
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(missing))
        self.assertEqual(str(caught.exception), f"--supersedes: документ {missing} не найден")
        self.assertEqual(self._counts(), (0, 0, 0, 0, 0))
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes="not-a-uuid")
        self.assertIn("некорректный UUID", str(caught.exception))

    def test_supersedes_refuses_a_private_document(self):
        user = User.objects.create_user(login="shelfowner", password="TestPassword123!")
        private = SourceDocument.objects.create(
            owner=user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("private"), normalized_text="private",
        )
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(private.pk))
        self.assertIn("не редакционный", str(caught.exception))
        self.assertEqual(SourceDocument.objects.count(), 1)

    def test_supersedes_marks_the_old_document_and_repeats_quietly(self):
        old = _editorial_document("Man is by nature a political animal, says the old PDF text.")
        out, _ = _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(old.pk))
        new = SourceDocument.objects.exclude(pk=old.pk).get()
        self.assertEqual(new.supersedes_id, old.pk)
        self.assertIn(f"Документ {old.pk} помечен заменённым", out)
        before = self._counts()
        out, _ = _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(old.pk))
        self.assertIn(f"уже импортировано: {new.pk}", out)
        self.assertIn(f"Документ {old.pk} уже заменён этим изданием", out)
        self.assertEqual(self._counts(), before)
        new.refresh_from_db()
        self.assertEqual(new.supersedes_id, old.pk)
        # Nothing is deleted: projects may refer to the old document.
        self.assertTrue(SourceDocument.objects.filter(pk=old.pk).exists())
        self.assertEqual(old.chunks.count(), 1)

    def test_supersedes_can_be_added_by_a_later_run(self):
        old = _editorial_document("Old text of the same book.")
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        new = SourceDocument.objects.exclude(pk=old.pk).get()
        self.assertIsNone(new.supersedes_id)
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(old.pk))
        new.refresh_from_db()
        self.assertEqual(new.supersedes_id, old.pk)
        other = _editorial_document("Another old text.")
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(other.pk))
        self.assertIn("второй заменяемый документ", str(caught.exception))
        with self.assertRaises(CommandError) as caught:
            _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(new.pk))
        self.assertIn("не может заменять сам себя", str(caught.exception))

    def test_same_text_with_other_chunk_sizes_is_a_clear_error(self):
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        with override_settings(SOURCE_CHUNK_WORDS_MIN=100, SOURCE_CHUNK_WORDS_MAX=200):
            with self.assertRaises(ShelfImportError) as caught:
                import_shelf_book(FIXTURE_MANIFEST)
        self.assertIn("настройки извлечения другие", str(caught.exception))
        self.assertEqual(self._counts()[:4], (1, 1, 1, 3))

    def test_indexing_keeps_chunks_and_embeds_only_what_is_missing(self):
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        before = list(document.chunks.order_by("ordinal").values_list("id", "text", "locator_json"))
        calls = []

        def fail_on_second_batch(texts):
            calls.append(len(texts))
            if len(calls) == 2:
                raise EmbeddingError("network_error", "обрыв")
            return _fake_vectors(texts)

        with override_settings(EMBEDDING_BATCH_SIZE=2):
            with mock.patch.object(retrieval, "embed_texts", fail_on_second_batch):
                result = index_document(document)
            self.assertEqual((result["state"], result["indexed"]), ("failed", 2))
            document.refresh_from_db()
            self.assertEqual(document.processing_state, SourceDocument.Processing.FAILED)
            self.assertEqual(Embedding.objects.count(), 2)

            calls.clear()
            with mock.patch.object(retrieval, "embed_texts", lambda texts: calls.append(len(texts)) or _fake_vectors(texts)):
                result = index_document(document)
            # Only the one chunk that had no embedding is paid for again.
            self.assertEqual(calls, [1])
            self.assertEqual((result["state"], result["indexed"], result["chunks"]), ("ready", 1, 3))

            calls.clear()
            with mock.patch.object(retrieval, "embed_texts", lambda texts: calls.append(len(texts)) or _fake_vectors(texts)):
                out, _ = _run("index_source", document=str(document.pk), reindex=True)
            self.assertEqual(calls, [])
            self.assertIn("Проиндексировано фрагментов: 0", out)
            self.assertIn("Фрагменты с указателями сохранены: 3", out)
        self.assertEqual(list(document.chunks.order_by("ordinal").values_list("id", "text", "locator_json")), before)
        self.assertEqual(Embedding.objects.count(), 3)

    def test_other_documents_are_chunked_as_before(self):
        user = User.objects.create_user(login="plainowner", password="TestPassword123!")
        text = " ".join(f"word{n}" for n in range(700))
        document = SourceDocument.objects.create(
            owner=user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash(text), normalized_text=text,
        )
        SourceChunk.objects.create(document=document, ordinal=0, text="stale", token_count=1, content_hash="stale")
        with mock.patch.object(retrieval, "embed_texts", _fake_vectors):
            result = index_document(document)
        self.assertEqual(result["state"], "ready")
        self.assertFalse(document.chunks.filter(text="stale").exists())
        self.assertTrue(all(chunk.locator_json == {} for chunk in document.chunks.all()))
        self.assertEqual(Embedding.objects.count(), document.chunks.count())


@mock.patch.object(retrieval, "embed_texts", _no_embeddings)
class SupersedeSearchTests(TestCase):
    """«Готово, когда» 4: a replaced document leaves the search only when the
    new one is ready."""

    def setUp(self):
        self.user = User.objects.create_user(login="shelfreader", password="TestPassword123!")
        self.old = _editorial_document("Man is by nature a political animal, says the old PDF text.")
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(self.old.pk))
        self.new = SourceDocument.objects.exclude(pk=self.old.pk).get()

    def _documents(self, results):
        return {chunk.document_id for chunk, _ in results}

    def test_old_document_is_searched_until_the_new_one_is_ready(self):
        results = hybrid_search(owner=self.user, query="political animal")
        self.assertEqual(self._documents(results), {self.old.pk})
        self.assertEqual({hit.chunk.document_id for hit in search_shelf("political animal").hits}, {self.old.pk})

    def test_old_document_is_hidden_once_the_new_one_is_ready(self):
        _make_ready(self.new)
        results = hybrid_search(owner=self.user, query="political animal")
        self.assertEqual(self._documents(results), {self.new.pk})
        self.assertEqual({hit.chunk.document_id for hit in search_shelf("political animal").hits}, {self.new.pk})
        # Asked for by id, the replaced document still gives nothing.
        self.assertEqual(hybrid_search(owner=self.user, query="political animal", document_ids=[self.old.pk]), [])

    def test_failed_indexing_keeps_the_old_document(self):
        self.new.processing_state = SourceDocument.Processing.FAILED
        self.new.save(update_fields=["processing_state"])
        self.assertEqual(self._documents(hybrid_search(owner=self.user, query="political animal")), {self.old.pk})

    def test_vector_half_also_skips_the_replaced_document(self):
        with mock.patch.object(retrieval, "embed_texts", _fake_vectors):
            index_document(self.old)
            index_document(self.new)
            result = search_shelf("political animal")
        self.assertTrue(result.vector_available)
        self.assertEqual({hit.chunk.document_id for hit in result.hits}, {self.new.pk})
        self.assertTrue(any(hit.scores["vector_raw"] is not None for hit in result.hits))

    def test_replacement_is_transitive(self):
        _make_ready(self.new)
        newest = _editorial_document("Third extraction: political animal.", supersedes=self.new)
        self.assertEqual(retrieval.superseded_document_ids(), {self.old.pk, self.new.pk})
        self.assertEqual(self._documents(hybrid_search(owner=self.user, query="political animal")), {newest.pk})
        # A link through a document that is not ready still hides the oldest.
        self.new.processing_state = SourceDocument.Processing.UPLOADED
        self.new.save(update_fields=["processing_state"])
        self.assertEqual(retrieval.superseded_document_ids(), {self.old.pk, self.new.pk})

    def test_private_document_is_not_hidden_by_an_editorial_one(self):
        private = SourceDocument.objects.create(
            owner=self.user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("mine"), normalized_text="mine", processing_state=SourceDocument.Processing.READY,
        )
        _editorial_document("Editorial text.", supersedes=private)
        self.assertNotIn(private.pk, retrieval.superseded_document_ids())


@mock.patch.object(retrieval, "embed_texts", _no_embeddings)
class ShelfListTests(TestCase):
    """The shelf is data: nothing about it is written in a template."""

    def setUp(self):
        self.user = User.objects.create_user(login="shelfuser", password="TestPassword123!")
        self.client.force_login(self.user)
        self.url = reverse("workshop:create_project")

    def test_empty_shelf(self):
        self.assertEqual(shelf_books(), [])
        self.assertIsNone(book_for_text("why-do-people-live-in-cities"))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "On the shelf")
        self.assertNotContains(response, "data-shelf-book")
        # PR 3: with an empty shelf the page does not offer to ask about a
        # book: no field, no examples, an honest line instead.
        self.assertNotContains(response, 'name="topic"')
        self.assertNotContains(response, "Ask about a book on our shelf.")
        self.assertContains(response, "There are no books on our shelf yet.")
        self.assertContains(response, "На полке пока нет книг")

    def test_book_appears_when_its_document_is_ready(self):
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        self.assertEqual(shelf_books(), [])
        self.assertNotContains(self.client.get(self.url), "On the shelf")

        _make_ready(document)
        books = shelf_books()
        self.assertEqual(len(books), 1)
        book = books[0]
        self.assertEqual(book.document, document)
        self.assertEqual((book.author, book.title, book.label), ("Aristotle", "Politics", "Aristotle, Politics"))
        self.assertEqual(book.attribution, ATTRIBUTION)
        self.assertEqual((book.author_ru, book.title_ru), ("Аристотель", "Политика"))
        self.assertEqual(book.label_ru, "Аристотель, «Политика»")
        self.assertEqual(book.related_texts, ("why-do-people-live-in-cities",))

        response = self.client.get(self.url)
        self.assertContains(response, f"On the shelf: {ATTRIBUTION}")
        self.assertContains(response, '<span class="ru-ui" lang="ru">На полке: Аристотель, «Политика»</span>', html=True)
        self.assertContains(response, "data-shelf-book", count=1)

    def test_book_for_text(self):
        _run("import_shelf_book", manifest=FIXTURE_MANIFEST)
        document = SourceDocument.objects.get()
        self.assertIsNone(book_for_text("why-do-people-live-in-cities"))
        _make_ready(document)
        self.assertEqual(book_for_text("why-do-people-live-in-cities").document, document)
        self.assertIsNone(book_for_text("socrates-questions"))

    def test_replaced_document_is_not_on_the_shelf(self):
        work = SourceWork.objects.create(author="Aristotle", title="Politics")
        old_edition = SourceEdition.objects.create(work=work, edition="Loeb Classical Library 264", translator="H. Rackham")
        old = _editorial_document("The old PDF text.")
        old.edition = old_edition
        old.save(update_fields=["edition"])
        self.assertEqual([book.document for book in shelf_books()], [old])

        _run("import_shelf_book", manifest=FIXTURE_MANIFEST, supersedes=str(old.pk))
        new = SourceDocument.objects.exclude(pk=old.pk).get()
        # Not indexed yet: the shelf keeps the old edition.
        self.assertEqual([book.document for book in shelf_books()], [old])
        _make_ready(new)
        self.assertEqual([book.document for book in shelf_books()], [new])
        self.assertContains(self.client.get(self.url), "data-shelf-book", count=1)

    def test_private_and_unattributed_documents_are_not_books(self):
        SourceDocument.objects.create(
            owner=self.user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("mine"), normalized_text="mine", processing_state=SourceDocument.Processing.READY,
        )
        _editorial_document("Editorial text without an edition.")
        self.assertEqual(shelf_books(), [])


@mock.patch.object(retrieval, "embed_texts", _no_embeddings)
class RussianQuestionTests(TestCase):
    """«Готово, когда» 4: a Russian question finds the English text by the
    text half alone, embeddings unavailable; a question about another
    philosopher finds nothing."""

    @classmethod
    def setUpTestData(cls):
        report = import_shelf_book(BOOK_MANIFEST)
        cls.document = SourceDocument.objects.get(pk=report.document_id)
        _make_ready(cls.document)

    def _expected_in_top3(self, result, book, pages):
        for hit in result.hits[:3]:
            locator = hit.chunk.locator_json
            if locator["book"] == book and pages & {segment["bekker"] for segment in locator["segments"]}:
                return True
        return False

    def test_control_questions_find_their_place(self):
        for question, book, pages in QUESTIONS:
            with self.subTest(question=question):
                result = search_shelf(question)
                self.assertFalse(result.vector_available)
                self.assertEqual(result.vector_error, "not_configured")
                self.assertTrue(result.hits)
                self.assertTrue(
                    self._expected_in_top3(result, book, pages),
                    [hit.locator for hit in result.hits[:3]],
                )
                for hit in result.hits:
                    self.assertTrue(hit.locator.startswith("Politics "))
                    self.assertGreater(hit.scores["fts_raw"], 0)
                    self.assertIsNone(hit.scores["vector_raw"])

    def test_questions_about_other_philosophers_find_nothing(self):
        for question in OFF_SHELF_QUESTIONS:
            with self.subTest(question=question):
                result = search_shelf(question)
                self.assertEqual(result.hits, [])
                self.assertEqual(result.fts_terms, [])
                self.assertFalse(result.vector_available)
        result = search_shelf(OFF_SHELF_QUESTIONS[0])
        self.assertEqual(result.unmapped_terms, ["заброшенность", "хайдеггера"])
        result = search_shelf(OFF_SHELF_QUESTIONS[1])
        self.assertEqual(result.unmapped_terms, ["категорический", "императив", "канта"])

    def test_two_named_questions_reach_the_exact_passages(self):
        result = search_shelf("Почему Аристотель называет человека политическим животным?")
        self.assertEqual(result.fts_terms, ["man", "human", "mankind", "political", "animal"])
        self.assertEqual(result.unmapped_terms, ["называет"])
        top = result.hits[0]
        self.assertIn("man is by nature a political animal", top.chunk.text)
        self.assertEqual(
            locate_quote(top.chunk.text, top.chunk.locator_json, "man is by nature a political animal"),
            "Politics 1.1253a",
        )
        result = search_shelf("Кого Аристотель считает гражданином?")
        self.assertEqual(result.fts_terms, ["citizen", "citizenship"])
        self.assertTrue(all(hit.chunk.locator_json["book"] == 3 for hit in result.hits[:3]))
        self.assertTrue(any("citizen pure and simple" in hit.chunk.text for hit in result.hits[:3]))

    def test_english_question_is_not_an_and_phrase(self):
        # "Aristotle" is not in the book: with AND this question found nothing.
        result = search_shelf("Why does Aristotle call man a political animal?")
        self.assertNotIn("aristotle", result.fts_terms)
        self.assertTrue(self._expected_in_top3(result, 1, {"1253a"}), [hit.locator for hit in result.hits[:3]])
        self.assertIn("man is by nature a political animal", result.hits[0].chunk.text)

    def test_english_question_about_another_philosopher(self):
        # English words are searched as they are, and the stem of
        # "thrownness" (thrown) does occur in the book. The name does not:
        # the hits are weak and the coverage says so. The threshold that
        # turns this into "no support" belongs to the answering step.
        result = search_shelf("What is thrownness in Heidegger?")
        self.assertEqual(result.missing_terms, ["heidegger"])
        self.assertTrue(all(hit.scores["fts_coverage"] < 0.5 for hit in result.hits))
        result = search_shelf("What is the categorical imperative in Kant?")
        self.assertEqual(result.hits, [])
        self.assertEqual(result.missing_terms, ["categorical", "imperative", "kant"])

    def test_author_and_title_are_left_out_of_the_query(self):
        result = search_shelf("Аристотель Политика")
        self.assertEqual((result.hits, result.fts_terms, result.unmapped_terms), ([], [], []))
        result = search_shelf("Aristotle Politics")
        self.assertEqual((result.hits, result.fts_terms), ([], []))

    def test_search_command_prints_locator_attribution_and_timing(self):
        out, _ = _run(
            "search_sources", query="Почему Аристотель называет человека политическим животным?", top=3, timing=True
        )
        self.assertIn("Векторная половина недоступна (not_configured)", out)
        self.assertIn("Слова текстового запроса: man, human, mankind, political, animal", out)
        self.assertIn("1. Politics 1.", out)
        self.assertEqual(out.count(ATTRIBUTION), 3)
        self.assertRegex(out, r"Время поиска: [\d.]+ мс; пик памяти процесса: [\d.]+ МБ")
        out, _ = _run("search_sources", query=OFF_SHELF_QUESTIONS[1], timing=True)
        self.assertIn("Ничего не найдено", out)
        self.assertIn("Русские слова без перевода в словаре полки: категорический, императив, канта", out)

    def test_locate_quote_command_gives_the_exact_page(self):
        quotes = [
            "man is by nature a political animal",
            "while it comes into existence for the sake of life",
            "Etruscans and Carthaginians",
            "any state that is truly so called",
        ]
        out, _ = _run("locate_quote", *[part for quote in quotes for part in ("--quote", quote)])
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("«man is by nature a political animal»: Politics 1.1253a (", lines[0])
        self.assertIn("; Politics 3.1278b (", lines[0])
        self.assertIn(": Politics 1.1252b (", lines[1])
        self.assertIn(": Politics 3.1280a (", lines[2])
        self.assertIn(": Politics 3.1280b (", lines[3])
        out, _ = _run("locate_quote", "--quote", quotes[0], "--document", str(self.document.pk))
        self.assertIn("Politics 1.1253a", out)
        with self.assertRaises(CommandError) as caught:
            _run("locate_quote", "--quote", "man is by nature a social animal")
        self.assertIn("Не найдено цитат: 1 из 1", str(caught.exception))

    def test_search_command_with_a_login_still_works(self):
        User.objects.create_user(login="cmduser", password="TestPassword123!")
        out, _ = _run("search_sources", login="cmduser", query="political animal", top=2)
        self.assertIn("1. Politics ", out)
        with self.assertRaises(CommandError):
            _run("search_sources", login="nobody", query="political animal")

    def test_vector_half_failure_is_reported_not_hidden(self):
        def broken(texts):
            raise EmbeddingError("network_error", "нет сети")

        with mock.patch.object(retrieval, "embed_texts", broken):
            with self.assertLogs("apps.sources.retrieval", level="WARNING") as logs:
                result = search_shelf("Кого Аристотель считает гражданином?")
        self.assertFalse(result.vector_available)
        self.assertEqual(result.vector_error, "network_error")
        self.assertTrue(result.hits)
        self.assertIn("network_error", logs.output[0])
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_vector_half_gives_raw_cosine(self):
        with mock.patch.object(retrieval, "embed_texts", _fake_vectors):
            index_document(self.document)
            result = search_shelf("Кого Аристотель считает гражданином?", top_n=5)
        self.assertTrue(result.vector_available)
        self.assertEqual(result.vector_error, "")
        self.assertEqual(len(result.hits), 5)
        for hit in result.hits:
            self.assertEqual(
                set(hit.scores), {"fts", "vector", "combined", "fts_raw", "fts_coverage", "vector_raw"}
            )
        raw = [hit.scores["vector_raw"] for hit in result.hits if hit.scores["vector_raw"] is not None]
        self.assertTrue(raw)
        self.assertTrue(all(-1.0 <= value <= 1.0 for value in raw))

    def test_shelf_search_ignores_private_documents(self):
        user = User.objects.create_user(login="privateowner", password="TestPassword123!")
        text = "Thrownness is a word of Heidegger."
        private = SourceDocument.objects.create(
            owner=user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash(text), normalized_text=text, processing_state=SourceDocument.Processing.READY,
        )
        SourceChunk.objects.create(document=private, ordinal=0, text=text, token_count=6, content_hash=text_hash(text))
        shelf_hits = search_shelf("What is thrownness in Heidegger?").hits
        self.assertNotIn(private.pk, {hit.chunk.document_id for hit in shelf_hits})
        owner_results = hybrid_search(owner=user, query="What is thrownness in Heidegger?")
        self.assertEqual(owner_results[0][0].document_id, private.pk)


class QuestionConceptsTests(SimpleTestCase):
    VOCABULARY = {
        "entries": [
            {"words": ["раб", "раба", "рабы"], "stems": ["рабств"], "en": ["slave", "slavery"]},
            {"stems": ["граждан"], "en": ["citizen"]},
            {"stems": ["общ"], "en": ["common"]},
            {"stems": ["общен"], "en": ["partnership"]},
        ],
        "ignore_exact": {"aristotle", "politics"},
        "ignore_ru": {"аристотел", "политик"},
    }

    def _terms(self, query):
        return [concept.terms for concept in lexical.question_concepts(query, self.VOCABULARY)]

    def test_stems_match_the_beginning_of_a_word(self):
        self.assertEqual(self._terms("гражданином"), [("гражданином", "citizen")])
        self.assertEqual(self._terms("Рабство"), [("рабство", "slave", "slavery")])

    def test_exact_words_do_not_match_longer_words(self):
        self.assertEqual(self._terms("раб"), [("раб", "slave", "slavery")])
        self.assertEqual(self._terms("работа"), [("работа",)])

    def test_several_entries_add_up(self):
        self.assertEqual(self._terms("общение"), [("общение", "common", "partnership")])

    def test_function_words_author_and_title_are_dropped(self):
        self.assertEqual(self._terms("Что такое у по Аристотелю в Политике?"), [])
        self.assertEqual(self._terms("Aristotle's Politics"), [("s",)])

    def test_yo_is_read_as_ye(self):
        vocabulary = {"entries": [{"stems": ["жен"], "en": ["wives"]}], "ignore_exact": set(), "ignore_ru": set()}
        self.assertEqual(
            [concept.terms for concept in lexical.question_concepts("жёны", vocabulary)], [("жены", "wives")]
        )


@mock.patch.object(retrieval, "embed_texts", _no_embeddings)
class TextHalfTests(TestCase):
    """BM25 over the scope and the coverage of the question."""

    def setUp(self):
        self.user = User.objects.create_user(login="bm25user", password="TestPassword123!")
        self.document = SourceDocument.objects.create(
            owner=self.user, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("bm25"), normalized_text="bm25", processing_state=SourceDocument.Processing.READY,
        )
        texts = [
            "The citizen rules the city. The citizen obeys the law. A citizen judges.",
            "The city has walls and a harbour, and a citizen guards them.",
            "The harbour is full of ships from far away.",
            "Music and gymnastics train the young.",
        ]
        self.chunks = [
            SourceChunk.objects.create(
                document=self.document, ordinal=ordinal, text=text, token_count=len(text.split()),
                content_hash=text_hash(text),
            )
            for ordinal, text in enumerate(texts)
        ]

    def _search(self, query):
        return hybrid_search(owner=self.user, query=query)

    def test_any_word_is_enough_and_more_occurrences_rank_higher(self):
        results = self._search("Who is a citizen of the philosophers?")
        self.assertEqual([chunk.ordinal for chunk, _ in results], [0, 1])
        self.assertGreater(results[0][1]["fts_raw"], results[1][1]["fts_raw"])
        self.assertEqual(results[0][1]["fts"], 1.0)
        # "philosophers" is nowhere in the scope: no chunk covers the question.
        self.assertLess(results[0][1]["fts_coverage"], 1.0)
        self.assertGreater(results[0][1]["fts_coverage"], 0.0)

    def test_coverage_is_full_when_every_concept_is_present(self):
        results = self._search("citizen harbour")
        by_ordinal = {chunk.ordinal: scores for chunk, scores in results}
        self.assertEqual(set(by_ordinal), {0, 1, 2})
        self.assertEqual(by_ordinal[1]["fts_coverage"], 1.0)
        self.assertLess(by_ordinal[0]["fts_coverage"], 1.0)
        self.assertLess(by_ordinal[2]["fts_coverage"], 1.0)
        self.assertEqual(results[0][0].ordinal, 1)

    def test_rare_word_weighs_more_than_a_common_one(self):
        results = self._search("city gymnastics")
        self.assertEqual(results[0][0].ordinal, 3)

    def test_stop_words_alone_find_nothing(self):
        self.assertEqual(self._search("what is the"), [])
        self.assertEqual(self._search("   "), [])

    def test_scores_depend_on_the_scope_only(self):
        before = self._search("citizen")[0][1]["fts_raw"]
        other = User.objects.create_user(login="bm25other", password="TestPassword123!")
        foreign = SourceDocument.objects.create(
            owner=other, origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("foreign"), normalized_text="foreign",
            processing_state=SourceDocument.Processing.READY,
        )
        for ordinal in range(5):
            text = f"A citizen and a citizen and a citizen, number {ordinal}."
            SourceChunk.objects.create(
                document=foreign, ordinal=ordinal, text=text, token_count=10, content_hash=text_hash(text)
            )
        self.assertEqual(self._search("citizen")[0][1]["fts_raw"], before)
        self.assertEqual({chunk.document_id for chunk, _ in self._search("citizen")}, {self.document.pk})

    def test_russian_text_is_found_by_its_own_words(self):
        text = "Человек по природе есть существо политическое."
        SourceChunk.objects.create(
            document=self.document, ordinal=10, text=text, token_count=6, content_hash=text_hash(text)
        )
        results = self._search("существо политическое")
        self.assertEqual([chunk.ordinal for chunk, _ in results], [10])
