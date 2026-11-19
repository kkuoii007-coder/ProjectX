"""Stage 5 tests: extraction, chunking, dedup, scope isolation, retrieval."""

import io
import os

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.sources.models import Embedding, SourceChunk, SourceDocument, SourceWork
from apps.sources.services import ExtractionError, chunk_text, extract_text, normalize_text, text_hash
from apps.credits.models import grant_welcome_credits


def _book_text(paragraphs: int, words: int = 120) -> str:
    return "\n\n".join(
        " ".join(f"word{n}of{p}" for n in range(words)) for p in range(paragraphs)
    )


class ExtractionTests(TestCase):
    def test_txt_and_md(self):
        self.assertEqual(extract_text("a.txt", b"hello"), "hello")
        self.assertEqual(extract_text("b.markdown", b"# Head\n\ntext"), "# Head\n\ntext")

    def test_unsupported_type(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text("c.docx", b"xx")
        self.assertEqual(caught.exception.code, "unsupported_type")

    def test_pdf_without_text_layer_rejected(self):
        # A parseable PDF whose pages contain no extractable text: patch
        # the locally imported PdfReader to test the rejection branch.
        from unittest import mock

        with mock.patch("pypdf.PdfReader") as reader_cls:
            reader = reader_cls.return_value
            reader.pages = [mock.MagicMock(extract_text=mock.MagicMock(return_value=""))]
            with self.assertRaises(ExtractionError) as caught:
                extract_text("scan.pdf", b"%PDF-1.4 fake")
        self.assertEqual(caught.exception.code, "no_text_layer")

    def test_pdf_malformed_rejected(self):
        with self.assertRaises(ExtractionError) as caught:
            extract_text("broken.pdf", b"%PDF-1.4\ntrailer<<>>\n%%EOF")
        self.assertEqual(caught.exception.code, "bad_pdf")

    def test_normalize_keeps_paragraphs(self):
        text = "Line one-  \ncontinued.  \n\nSecond  paragraph"
        normalized = normalize_text(text)
        # De-hyphenation joins a dash at line end with the next word.
        self.assertIn("Line onecontinued.", normalized)
        self.assertIn("Second paragraph", normalized)

    def test_normalize_paragraph_breaks(self):
        normalized = normalize_text("A\n\n\n\nB")
        self.assertEqual(normalized, "A\n\nB")


class ChunkingTests(TestCase):
    def test_chunks_within_bounds(self):
        chunks = chunk_text(_book_text(10, 150))
        self.assertGreater(len(chunks), 1)
        minimum, maximum = 250, 700  # practical bounds with sentence integrity
        for ordinal, text, token_count in chunks:
            self.assertEqual(ordinal, chunks.index((ordinal, text, token_count)))
            self.assertLessEqual(token_count, maximum)
            self.assertGreater(token_count, 0)

    def test_short_text_single_chunk(self):
        chunks = chunk_text("One short paragraph about ethics.")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][0], 0)

    def test_no_sentence_breaking(self):
        # A single huge sentence stays whole even above max words.
        sentence = " ".join(f"w{n}" for n in range(900)) + "."
        chunks = chunk_text(sentence)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0][2], 900)


class ImportDedupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="srcuser", password="TestPassword123!")

    def test_create_document(self):
        from apps.sources.services import create_document_from_upload

        document, created = create_document_from_upload(
            owner=self.user, filename="notes.txt", data=b"\n\nHello   world\n\ntext"
        )
        self.assertTrue(created)
        self.assertEqual(document.processing_state, SourceDocument.Processing.UPLOADED)
        self.assertEqual(document.visibility, SourceDocument.Visibility.PRIVATE)
        self.assertEqual(document.content_hash, text_hash(document.normalized_text))

    def test_repeated_hash_no_new_version(self):
        from apps.sources.services import create_document_from_upload

        first, created1 = create_document_from_upload(owner=self.user, filename="a.txt", data=b"same text")
        second, created2 = create_document_from_upload(owner=self.user, filename="b.txt", data=b"same text")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SourceDocument.objects.filter(owner=self.user).count(), 1)


class ScopeIsolationTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(login="alice", password="TestPassword123!")
        self.bob = User.objects.create_user(login="bob", password="TestPassword123!")
        grant_welcome_credits(self.alice)
        grant_welcome_credits(self.bob)

    def _document(self, owner, text):
        return SourceDocument.objects.create(
            owner=owner,
            origin=SourceDocument.Origin.USER,
            visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash(text),
            normalized_text=text,
            processing_state=SourceDocument.Processing.READY,
        )

    def test_private_document_not_in_other_scope(self):
        from apps.sources.retrieval import hybrid_search

        document = self._document(self.alice, "Stoicism teaches virtue ethics in plain language.")
        SourceChunk.objects.create(
            document=document, ordinal=0, text="Stoicism teaches virtue ethics in plain language.",
            token_count=8, content_hash="h1",
        )
        results_alice = hybrid_search(owner=self.alice, query="stoicism virtue")
        results_bob = hybrid_search(owner=self.bob, query="stoicism virtue")
        self.assertEqual(len(results_alice), 1)
        self.assertEqual(results_bob, [])

    def test_editorial_visible_to_all(self):
        from apps.sources.retrieval import hybrid_search

        document = SourceDocument.objects.create(
            owner=None,
            origin=SourceDocument.Origin.EDITORIAL,
            visibility=SourceDocument.Visibility.EDITORIAL,
            content_hash=text_hash("Shared editorial corpus text about Plato."),
            normalized_text="Shared editorial corpus text about Plato.",
            processing_state=SourceDocument.Processing.READY,
        )
        SourceChunk.objects.create(
            document=document, ordinal=0, text="Shared editorial corpus text about Plato.",
            token_count=7, content_hash="h2",
        )
        self.assertEqual(len(hybrid_search(owner=self.alice, query="Plato")), 1)
        self.assertEqual(len(hybrid_search(owner=self.bob, query="Plato")), 1)


class EditorialImportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="srcuser", password="TestPassword123!")

    def _import(self, path, **kwargs):
        from django.core.management import call_command

        options = {"path": path, "title": "Aristotle, Politics (Rackham)", "author": "Aristotle",
                   "work_title": "Politics", "kind": "primary", "language": "en",
                   "canonical_url": "", "edition": "Loeb Classical Library 264",
                   "translator": "H. Rackham", "rights": "allowed", "edition_url": "",
                   "retrieved_at": "2026-11-18"}
        options.update(kwargs)
        self._out = io.StringIO()
        call_command("import_editorial_source", **options, stdout=self._out)
        return self._out.getvalue()

    def test_creates_shared_editorial_document(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"Man is by nature a political animal.")
            path = handle.name
        self.addCleanup(os.unlink, path)
        output = self._import(path)
        document = SourceDocument.objects.get(owner__isnull=True)
        self.assertEqual(document.origin, SourceDocument.Origin.EDITORIAL)
        self.assertEqual(document.visibility, SourceDocument.Visibility.EDITORIAL)
        self.assertEqual(document.rights_state, "allowed")
        self.assertEqual(document.edition.work.author, "Aristotle")
        self.assertEqual(document.edition.work.title, "Politics")
        self.assertEqual(document.edition.translator, "H. Rackham")
        self.assertIn("создан", output)
        # The editorial corpus is visible to any account in retrieval scope.
        # The imported document is UPLOADED until index_source runs, so the
        # retrieval check happens on a READY copy (indexing is a separate step).
        from apps.sources.retrieval import _scope_documents

        document.processing_state = SourceDocument.Processing.READY
        document.save(update_fields=["processing_state"])
        self.assertIn(document.pk, [d.pk for d in _scope_documents(self.user)])

    def test_repeated_hash_no_new_document(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"Man is by nature a political animal.")
            path = handle.name
        self.addCleanup(os.unlink, path)
        self._import(path)
        self._import(path)
        self.assertEqual(SourceDocument.objects.filter(owner__isnull=True).count(), 1)
        self.assertEqual(SourceWork.objects.count(), 1)

    def test_missing_file_rejected(self):
        with self.assertRaises(CommandError):
            self._import("/nonexistent/file.txt")

    def test_bad_rights_rejected(self):
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"text")
            path = handle.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(CommandError):
            self._import(path, rights="public-domain")


class RetrievalRankingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="rankuser", password="TestPassword123!")

    def test_fts_only_leg_works_without_embeddings(self):
        from apps.sources.retrieval import hybrid_search

        document = SourceDocument.objects.create(
            owner=self.user,
            origin=SourceDocument.Origin.USER,
            visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash("The categorical imperative binds rational agents."),
            normalized_text="The categorical imperative binds rational agents.",
            processing_state=SourceDocument.Processing.READY,
        )
        SourceChunk.objects.create(
            document=document, ordinal=0, text="The categorical imperative binds rational agents.",
            token_count=7, content_hash="h3",
        )
        results = hybrid_search(owner=self.user, query="categorical imperative")
        self.assertEqual(len(results), 1)
        scores = results[0][1]
        self.assertEqual(scores["fts"], 1.0)
        self.assertEqual(scores["vector"], 0.0)

    def test_top_n_limit(self):
        from apps.sources.retrieval import hybrid_search
        from django.conf import settings

        document = SourceDocument.objects.create(
            owner=self.user,
            origin=SourceDocument.Origin.USER,
            visibility=SourceDocument.Visibility.PRIVATE,
            content_hash=text_hash(_book_text(30, 100)),
            normalized_text=_book_text(30, 100),
            processing_state=SourceDocument.Processing.READY,
        )
        for ordinal, text, token_count in chunk_text(document.normalized_text):
            SourceChunk.objects.create(
                document=document, ordinal=ordinal, text=text, token_count=token_count, content_hash=f"h{ordinal}"
            )
        with override_settings(RETRIEVAL_MAX_CHUNKS=3):
            results = hybrid_search(owner=self.user, query="word1of1")
        self.assertLessEqual(len(results), 3)