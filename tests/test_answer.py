"""PR 3 «ответ с источником»: a question about a book on the shelf gets a
short text where every part has a source line with the place in the book
and an excerpt, or a refusal that costs the reader nothing.

No network and no paid call anywhere: the provider is FakeProvider (or the
real OpenRouterProvider with `_chat_completion` patched), embeddings are a
patched `embed_texts` (tests.helpers.shelf_vectors)."""

import json
import os
import re
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.ai import grounding
from apps.ai.grounding import Fragment, check_answer, find_verbatim, quoted_phrases
from apps.ai.models import AIJob, ProviderAttempt, QualityReport, RetrievalItem
from apps.ai.services import (
    AskDisabled,
    EditNotSupported,
    FakeProvider,
    claim_next_job,
    create_job,
    execute_job,
)
from apps.credits.models import BudgetBucket, Wallet, grant_welcome_credits
from apps.library.models import Publication
from apps.sources import retrieval
from apps.sources.locators import locate_quote, span_place
from apps.sources.models import SourceDocument
from apps.workshop.models import EvidenceLink, Message, Project, Revision, RevisionSource

from .helpers import no_embeddings, publish_demo_fixture, put_book_on_shelf, shelf_search
from .test_editorial_path import LIVE, PASS_REVIEW, PRICES, fake_completion

ATTRIBUTION = (
    "Aristotle, Politics. Translated by H. Rackham. Loeb Classical Library (first published 1932). "
    "Digital text: Perseus Digital Library, from the 1944 printing. CC BY-SA 4.0."
)
POLITICAL_ANIMAL = "Почему Аристотель называет человека политическим животным?"
KANT = "Что такое категорический императив у Канта?"
HEIDEGGER = "Что такое заброшенность у Хайдеггера?"
# A question about the book with a name the lexicon of the book does not
# know: the text half finds the place but with a low coverage (control
# question 6 of the owner behaves the same way on the full book).
WITH_PLATO = "Почему человек — политическое животное у Аристотеля, а не у Платона?"
RELATED_TEXT = "why-do-people-live-in-cities"  # named in the fixture manifest (`related_texts`)
# What the licence of the source asks to show next to the attribution
# (addresses from the manifest): digital publisher, source file, licence.
EDITION_LINKS = (
    ' <span class="edition-links" data-shelf-links>'
    '<a href="https://www.perseus.tufts.edu/" rel="noopener noreferrer">Perseus Digital Library</a> · '
    '<a href="https://github.com/PerseusDL/canonical-greekLit/blob/8375d5b7f542fd28790d05895c380d9c2620ca9d/data/tlg0086/tlg035/tlg0086.tlg035.perseus-eng2.xml" rel="noopener noreferrer">Source file</a> · '
    '<a href="https://creativecommons.org/licenses/by-sa/4.0/" rel="noopener noreferrer">CC BY-SA 4.0</a></span>'
)
SOURCE_LINE = re.compile(r"Source: Aristotle, Politics, 1\.125[23][ab](–125[23][ab])?<")


def page(response) -> str:
    return response.content.decode("utf-8")


class AnswerBase(TestCase):
    """A signed-in reader with the welcome credits and one book on the shelf.
    The search works and uses no network (a class decorator would not reach
    the tests of the subclasses, so the patch starts here)."""

    def setUp(self):
        patcher = shelf_search()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.user = User.objects.create_user(login="asker", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.client.force_login(self.user)
        self.document = put_book_on_shelf()
        self.create_url = reverse("workshop:create_project")

    def wallet(self):
        return Wallet.objects.get(user=self.user)

    def ask(self, question, run=True):
        """The way of the site: the form creates the project and the job,
        the worker executes it. Returns (project, job)."""
        response = self.client.post(self.create_url, {"mode": "explain_topic", "topic": question})
        self.assertEqual(response.status_code, 302, page(response)[:300])
        project = Project.objects.filter(owner=self.user).latest("created_at")
        job = AIJob.objects.get(project=project, kind=AIJob.Kind.GENERATE)
        if run:
            execute_job(claim_next_job())
            job.refresh_from_db()
            project.refresh_from_db()
        return project, job

    def project_page(self, project):
        response = self.client.get(reverse("workshop:project", args=[project.pk]))
        self.assertEqual(response.status_code, 200)
        return page(response)

    def assert_nothing_was_made_or_charged(self, project, job, before):
        self.assertFalse(Revision.objects.filter(project=project).exists())
        self.assertIsNone(project.head_revision)
        self.assertFalse(EvidenceLink.objects.exists())
        after = self.wallet()
        self.assertEqual((after.total_credits, after.reserved_credits), (before.total_credits, 0))


class AnswerWithSourcesTests(AnswerBase):
    """«Готово, когда» 1: every part has a source line from the found set,
    on the page and in the database."""

    def test_every_part_has_a_source_line_on_the_page_and_in_the_database(self):
        project, job = self.ask(POLITICAL_ANIMAL)
        self.assertEqual((job.status, job.outcome), (AIJob.Status.SUCCEEDED, AIJob.Outcome.DELIVERED), job.result_reference)
        self.assertEqual(self.wallet().total_credits, 30 - settings.CREDIT_COSTS["generate"])
        self.assertEqual(self.wallet().reserved_credits, 0)

        # The search ran before the model and its result is stored.
        items = {item.citation_id: item for item in RetrievalItem.objects.filter(job=job)}
        self.assertTrue(items)
        self.assertEqual(sorted(items, key=lambda value: int(value[1:])), [f"S{rank}" for rank in range(1, len(items) + 1)])
        self.assertEqual(sorted(item.rank for item in items.values()), list(range(1, len(items) + 1)))
        self.assertTrue(all("vector_raw" in item.score_json and "fts_coverage" in item.score_json for item in items.values()))
        self.assertTrue(job.result_reference["search"]["vector_available"])

        revision = project.head_revision
        payload = revision.payload
        self.assertEqual(len(payload["parts"]), 3)
        self.assertEqual(payload["based_on"], ["Aristotle, Politics"])
        self.assertEqual(payload["source_editions"][0]["attribution"], ATTRIBUTION)
        for index, part in enumerate(payload["parts"]):
            self.assertTrue(part["sources"], f"part {index} has no source")
            links = list(EvidenceLink.objects.filter(revision=revision, locator_json__part=index))
            self.assertEqual(len(links), len(part["sources"]))
            for source, link in zip(part["sources"], links):
                # From the found set, and the excerpt is the book's own words.
                chunk = items[source["citation_id"]].source_chunk
                self.assertTrue(items[source["citation_id"]].score_json["support"])
                self.assertEqual(chunk.document_id, self.document.pk)
                self.assertIn(source["excerpt"], " ".join(chunk.text.split()))
                self.assertLessEqual(len(source["excerpt"].split()), 60)
                # The place is the exact page of the excerpt, counted from the fragment's page marks.
                self.assertEqual(source["locator"], locate_quote(chunk.text, chunk.locator_json, source["excerpt"]))
                self.assertEqual(source["label"], "Aristotle, " + source["locator"].replace("Politics ", "Politics, ", 1))
                self.assertEqual(
                    (link.kind, link.support_type, link.citation_id, link.source_chunk_id, link.claim_text),
                    ("claim_support", "quote", source["citation_id"], chunk.pk, source["excerpt"]),
                )
                self.assertEqual(link.locator_json["label"], source["label"])
        self.assertEqual(EvidenceLink.objects.filter(revision=revision).count(), sum(len(part["sources"]) for part in payload["parts"]))
        link = RevisionSource.objects.get(revision=revision)
        self.assertEqual((link.document, link.role), (self.document, "primary"))
        self.assertEqual(link.provenance_json["attribution"], ATTRIBUTION)
        self.assertEqual(link.provenance_json["document_content_hash"], self.document.content_hash)
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.deterministic_json["source_check"], "passed")
        self.assertEqual(report.review_json["verdict"], "pass")

        content = self.project_page(project)
        self.assertEqual(len(SOURCE_LINE.findall(content)), 3)
        self.assertEqual(content.count('<details class="source">'), 3)
        self.assertEqual(content.count('<span class="ru-ui" lang="ru">Источник</span>'), 3)
        for part in payload["parts"]:
            for source in part["sources"]:
                self.assertContains(self.client.get(reverse("workshop:project", args=[project.pk])), source["excerpt"])
        self.assertIn(ATTRIBUTION, content)
        # Next to the attribution in every opened source: where the text
        # comes from and its licence, as links.
        self.assertEqual(content.count(ATTRIBUTION + EDITION_LINKS), 3)
        self.assertIn("AI draft. Based on: Aristotle, Politics.", content)
        self.assertIn("Черновик ИИ. Опирается на: Аристотель, «Политика».", content)
        # No edit forms for an answer with sources; the versions table stays.
        self.assertNotIn("workshop-edit", content)
        self.assertNotIn('name="kind"', content)
        self.assertIn("Версии", content)

    def test_page_is_rendered_from_the_snapshot_not_from_the_shelf(self):
        project, _ = self.ask(POLITICAL_ANIMAL)
        before = self.project_page(project)
        # The book leaves the shelf (as on re-indexing): the page of the answer does not change.
        SourceDocument.objects.filter(pk=self.document.pk).update(processing_state=SourceDocument.Processing.FAILED)
        after = self.project_page(project)
        self.assertEqual(SOURCE_LINE.findall(before), SOURCE_LINE.findall(after))
        self.assertIn(ATTRIBUTION, after)
        self.assertEqual(after.count(ATTRIBUTION + EDITION_LINKS), 3)
        self.assertIn("AI draft. Based on: Aristotle, Politics.", after)

    def test_russian_and_english_questions_are_answered(self):
        for question in (POLITICAL_ANIMAL, "Why does Aristotle call man a political animal?"):
            with self.subTest(question=question):
                Wallet.objects.filter(user=self.user).update(total_credits=30)
                project, job = self.ask(question)
                self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
                self.assertEqual(job.input_json["instruction"], question)
                self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)

    def test_the_whole_question_goes_to_the_job(self):
        question = "Почему Аристотель называет человека политическим животным? " + "И ещё одно уточнение к вопросу. " * 12
        question = question.strip()[:480]
        self.assertGreater(len(question), 200)
        project, job = self.ask(question, run=False)
        self.assertEqual(job.input_json["instruction"], question)
        self.assertEqual(project.title, question[:200])
        with mock.patch.object(FakeProvider, "generate", autospec=True, side_effect=FakeProvider.generate) as generate:
            execute_job(claim_next_job())
        self.assertEqual(generate.call_args.args[1].input_json["instruction"], question)

    def test_only_supports_are_shown_to_the_model(self):
        """A fragment below both thresholds is stored but not sent."""
        with override_settings(SHELF_MIN_VECTOR_SIMILARITY=2.0), mock.patch.object(
            FakeProvider, "generate", autospec=True, side_effect=FakeProvider.generate
        ) as generate:
            _, job = self.ask(POLITICAL_ANIMAL)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        supports = {item.citation_id for item in RetrievalItem.objects.filter(job=job) if item.score_json["support"]}
        self.assertTrue(supports)
        self.assertLess(len(supports), RetrievalItem.objects.filter(job=job).count())
        self.assertEqual({source["id"] for source in generate.call_args.kwargs["sources"]}, supports)
        for source in generate.call_args.kwargs["sources"]:
            self.assertEqual(set(source), {"id", "locator", "text"})
            self.assertRegex(source["locator"], r"^Aristotle, Politics, 1\.125")

    def test_waiting_state_is_as_before(self):
        project, _ = self.ask(POLITICAL_ANIMAL, run=False)
        content = self.project_page(project)
        self.assertIn("Текст ещё не создан. Проверьте статус задачи в кабинете.", content)
        self.assertNotIn("We have no source for this yet.", content)
        self.assertNotIn("No credits were taken.", content)


class RejectedAnswerTests(AnswerBase):
    """«Готово, когда» 2 and 4: an answer whose sources do not pass the
    check is not saved and not charged."""

    def run_rejected(self, question):
        before = self.wallet()
        with mock.patch.object(FakeProvider, "generate", autospec=True, side_effect=FakeProvider.generate) as generate:
            project, job = self.ask(question)
        self.assertEqual(generate.call_count, 2)  # the answer and one correction attempt
        correction = generate.call_args.kwargs["correction"]
        self.assertTrue(correction["violations"])
        self.assertIn("parts", correction["candidate"])
        self.assertEqual(
            (job.status, job.outcome, job.error_code),
            (AIJob.Status.SUCCEEDED, AIJob.Outcome.NO_CHARGE_FAILURE, "source_validation_failed"),
            job.result_reference,
        )
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assertFalse(QualityReport.objects.exists())
        content = self.project_page(project)
        self.assertIn("The text was not made.", content)
        self.assertIn("No credits were taken.", content)
        self.assertIn("ссылки на источник не прошли проверку", content)
        self.assertNotIn("We have no source for this yet.", content)
        self.assertNotIn("Версии", content)
        return job

    def test_source_outside_the_found_set_is_rejected(self):
        job = self.run_rejected(POLITICAL_ANIMAL + " cite-outside")
        self.assertIn("'S99' is not one of the provided ids", " ".join(job.result_reference["violations"]))
        self.assertFalse(RetrievalItem.objects.filter(job=job, citation_id="S99").exists())

    def test_quote_that_is_not_in_the_fragment_is_rejected(self):
        job = self.run_rejected(POLITICAL_ANIMAL + " misquote")
        self.assertIn("part 2: no valid source", " ".join(job.result_reference["violations"]))
        self.assertIn("is not in the text of", " ".join(job.result_reference["violations"]))

    def test_words_in_quotation_marks_that_are_in_no_fragment_are_rejected(self):
        job = self.run_rejected(POLITICAL_ANIMAL + " invent-quote")
        self.assertIn("the moon is made of green cheese", " ".join(job.result_reference["violations"]))

    def test_one_correction_attempt_can_save_the_answer(self):
        for word in ("cite-outside-once", "misquote-once", "invent-quote-once"):
            with self.subTest(word=word):
                Wallet.objects.filter(user=self.user).update(total_credits=30)
                with mock.patch.object(FakeProvider, "generate", autospec=True, side_effect=FakeProvider.generate) as generate:
                    project, job = self.ask(f"{POLITICAL_ANIMAL} {word}")
                self.assertEqual(generate.call_count, 2)
                self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
                self.assertEqual(self.wallet().total_credits, 20)
                self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)
                self.assertNotIn("green cheese", json.dumps(project.head_revision.payload))


class NoSourceTests(AnswerBase):
    """«Готово, когда» 3: both lines of refusal, the balance does not change."""

    def assert_refusal_screen(self, project):
        content = self.project_page(project)
        self.assertIn("We have no source for this yet.", content)
        self.assertIn("У нас пока нет источника для этого вопроса.", content)
        self.assertIn("No credits were taken.", content)
        self.assertIn("Кредиты не списаны.", content)
        self.assertIn(ATTRIBUTION, content)  # the books on the shelf
        self.assertEqual(content.count(ATTRIBUTION + EDITION_LINKS), 1)
        self.assertIn("data-shelf-book", content)
        self.assertIn(f'href="{self.create_url}"', content)
        self.assertIn("Ask another question", content)
        # No revision, no edit forms, no versions table.
        for absent in ("workshop-reading", "workshop-edit", "Версии", "<table", "Search is not available now."):
            self.assertNotIn(absent, content)

    def test_threshold_refuses_before_any_model_call(self):
        for question in (KANT, HEIDEGGER):
            with self.subTest(question=question):
                before = self.wallet()
                with mock.patch.object(FakeProvider, "generate", side_effect=AssertionError("the model must not be called")), mock.patch.object(
                    FakeProvider, "review", side_effect=AssertionError("the reviewer must not be called")
                ):
                    project, job = self.ask(question)
                self.assertEqual((job.status, job.outcome, job.error_code), (AIJob.Status.SUCCEEDED, AIJob.Outcome.NO_SOURCE, ""), job.result_reference)
                self.assertEqual(job.result_reference["refusal_line"], "threshold")
                search = job.result_reference["search"]
                self.assertEqual((search["vector_available"], search["supports"]), (True, 0))
                self.assertLess(search["best_vector_raw"], settings.SHELF_MIN_VECTOR_SIMILARITY)
                # What was found is stored, and none of it is a support.
                self.assertFalse(any(item.score_json["support"] for item in RetrievalItem.objects.filter(job=job)))
                self.assert_nothing_was_made_or_charged(project, job, before)
                self.assertFalse(ProviderAttempt.objects.filter(job=job).exists())
                self.assertTrue(Message.objects.filter(project=project, role="assistant", kind="explanation").exists())
                self.assert_refusal_screen(project)

    def test_model_refuses_when_the_fragments_do_not_answer(self):
        """The second line: the threshold let the question through."""
        before = self.wallet()
        with mock.patch.object(FakeProvider, "generate", autospec=True, side_effect=FakeProvider.generate) as generate:
            project, job = self.ask("Что Аристотель говорит о человеке? no-source")
        self.assertEqual(generate.call_count, 1)  # a refusal is not "corrected"
        self.assertEqual((job.status, job.outcome), (AIJob.Status.SUCCEEDED, AIJob.Outcome.NO_SOURCE), job.result_reference)
        self.assertEqual(job.result_reference["refusal_line"], "model")
        self.assertGreater(job.result_reference["search"]["supports"], 0)
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assert_refusal_screen(project)

    def test_empty_shelf_is_refused_by_the_form(self):
        SourceDocument.objects.filter(pk=self.document.pk).update(processing_state=SourceDocument.Processing.FAILED)
        response = self.client.post(self.create_url, {"mode": "explain_topic", "topic": POLITICAL_ANIMAL})
        self.assertEqual(response.status_code, 409)
        self.assertIn("На полке пока нет книг", page(response))
        self.assertNotIn('name="topic"', page(response))
        self.assertFalse(Project.objects.filter(owner=self.user).exists())
        self.assertFalse(AIJob.objects.exists())
        self.assertEqual(self.wallet().reserved_credits, 0)


class SearchUnavailableTests(AnswerBase):
    """«Поиск недоступен» is not «источника нет»."""

    def test_search_down_is_not_reported_as_no_source(self):
        before = self.wallet()
        with mock.patch.object(retrieval, "embed_texts", no_embeddings), mock.patch.object(
            FakeProvider, "generate", side_effect=AssertionError("the model must not be called")
        ):
            project, job = self.ask(KANT)
        self.assertEqual((job.status, job.outcome, job.error_code), (AIJob.Status.FAILED, None, "search_unavailable"), job.result_reference)
        self.assertFalse(job.result_reference["search"]["vector_available"])
        self.assert_nothing_was_made_or_charged(project, job, before)
        content = self.project_page(project)
        self.assertIn("Search is not available now. Try again later.", content)
        self.assertIn("Поиск по книгам сейчас недоступен. Попробуйте позже.", content)
        self.assertIn("No credits were taken.", content)
        self.assertNotIn("We have no source for this yet.", content)
        self.assertNotIn("Версии", content)

    def test_text_half_alone_still_answers_a_question_about_the_book(self):
        """Degraded search: the vector half is down, the words of the
        question are in the book."""
        with mock.patch.object(retrieval, "embed_texts", no_embeddings):
            project, job = self.ask(POLITICAL_ANIMAL)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        search = job.result_reference["search"]
        self.assertFalse(search["vector_available"])
        self.assertIsNone(search["best_vector_raw"])
        self.assertGreaterEqual(search["best_fts_coverage"], settings.SHELF_MIN_FTS_COVERAGE)
        self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)

    def test_question_with_a_name_outside_the_lexicon_needs_the_vector_half(self):
        """The text coverage is under the threshold because of one unknown
        name: the question is answered by the vector half."""
        project, job = self.ask(WITH_PLATO)
        search = job.result_reference["search"]
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertEqual(search["unmapped_terms"], ["платона"])
        self.assertLess(search["best_fts_coverage"], settings.SHELF_MIN_FTS_COVERAGE)
        self.assertGreaterEqual(search["best_vector_raw"], settings.SHELF_MIN_VECTOR_SIMILARITY)
        self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)

    def test_question_with_a_name_outside_the_lexicon_and_no_vector_half(self):
        before = self.wallet()
        with mock.patch.object(retrieval, "embed_texts", no_embeddings), mock.patch.object(
            FakeProvider, "generate", side_effect=AssertionError("the model must not be called")
        ):
            project, job = self.ask(WITH_PLATO)
        self.assertEqual((job.status, job.outcome, job.error_code), (AIJob.Status.FAILED, None, "search_unavailable"), job.result_reference)
        # Without the vector half: «поиск недоступен», never «источника нет».
        search = job.result_reference["search"]
        self.assertEqual((search["vector_available"], search["supports"], search["unmapped_terms"]), (False, 0, ["платона"]))
        self.assertGreater(search["found"], 0)  # the place was found, but it is not a support
        self.assertLess(search["best_fts_coverage"], settings.SHELF_MIN_FTS_COVERAGE)
        self.assert_nothing_was_made_or_charged(project, job, before)
        content = self.project_page(project)
        self.assertIn("Search is not available now. Try again later.", content)
        self.assertNotIn("We have no source for this yet.", content)


class QuestionFormTests(AnswerBase):
    def test_form_asks_about_a_book_on_the_shelf(self):
        content = page(self.client.get(self.create_url))
        self.assertIn('Ask about a book on our shelf.<span class="ru-ui" lang="ru">', content)
        self.assertIn(f"On the shelf: {ATTRIBUTION}", content)
        self.assertEqual(content.count(ATTRIBUTION + EDITION_LINKS), 1)
        self.assertIn('maxlength="500"', content)
        examples = [
            "Почему Аристотель называет человека политическим животным?",
            "Кого Аристотель считает гражданином?",
            "Какие виды государственного устройства различает Аристотель?",
        ]
        # Without JavaScript the first example is the placeholder and the only visible line.
        self.assertIn(f'placeholder="{examples[0]}"', content)
        self.assertIn(f'<span data-example="{examples[0]}">Например: {examples[0]}</span>', content)
        for example in examples[1:]:
            self.assertIn(f'<span data-example="{example}" hidden>Например: {example}</span>', content)
        self.assertIn('data-topic-examples="topic"', content)
        for gone in ("Plato", "Socrates", "For example:"):
            self.assertNotIn(gone, content)

    def test_question_with_control_characters_is_refused(self):
        for bad in ("Who is a citizen?\x00", "Who is\x1b a citizen?", "Who is a\x7f citizen?"):
            response = self.client.post(self.create_url, {"mode": "explain_topic", "topic": bad})
            self.assertEqual(response.status_code, 400, repr(bad))
            self.assertIn("В вопросе есть служебные символы", page(response))
        self.assertFalse(Project.objects.filter(owner=self.user).exists())
        self.assertFalse(AIJob.objects.filter(owner=self.user).exists())

    def test_question_longer_than_500_characters_is_refused_not_cut(self):
        self.assertEqual(settings.MAX_QUESTION_CHARS, 500)
        question = ("Почему Аристотель называет человека политическим животным? " * 9)[:501]
        response = self.client.post(self.create_url, {"mode": "explain_topic", "topic": question})
        self.assertEqual(response.status_code, 400)
        content = page(response)
        self.assertIn("Вопрос слишком длинный: 501 символов при пределе 500", content)
        self.assertIn(question, content)  # the reader keeps what he typed
        self.assertFalse(Project.objects.filter(owner=self.user).exists())
        self.assertFalse(AIJob.objects.exists())
        self.assertEqual(self.wallet().reserved_credits, 0)

    def test_question_of_exactly_500_characters_is_accepted(self):
        question = ("Почему Аристотель называет человека политическим животным? " * 9)[:499].rstrip().ljust(500, "?")
        self.assertEqual(len(question), 500)
        _, job = self.ask(question, run=False)
        self.assertEqual(job.input_json["instruction"], question)

    def test_line_breaks_are_counted_as_the_reader_saw_them(self):
        question = ("строка вопроса\r\n" * 40).strip()  # 40 lines: 599 bytes with \r\n, 599 - 39 = 560 as typed
        self.assertEqual(self.client.post(self.create_url, {"mode": "explain_topic", "topic": question}).status_code, 400)
        short = ("Почему человек —\r\nполитическое животное?\r\n" * 10).strip()
        self.assertLess(len(short.replace("\r\n", "\n")), 500)
        _, job = self.ask(short, run=False)
        self.assertNotIn("\r", job.input_json["instruction"])


class EditNotSupportedTests(AnswerBase):
    def test_edit_of_an_answer_with_sources_is_refused(self):
        project, _ = self.ask(POLITICAL_ANIMAL)
        revision = project.head_revision
        before = self.wallet()
        jobs = AIJob.objects.count()
        url = reverse("workshop:edit_fragment", args=[project.pk])
        for data in (
            {"kind": "edit_sentence", "target": "0:0", "instruction": "проще"},
            {"kind": "edit_part", "target": "0", "instruction": "короче"},
        ):
            with self.subTest(kind=data["kind"]):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 409)
                self.assertIn("Правка ответа с источниками пока не поддерживается", page(response))
                self.assertEqual(len(SOURCE_LINE.findall(page(response))), 3)  # the page stays whole
        # The service refuses any caller, before a job or a reservation.
        for kind, target in ((AIJob.Kind.EDIT_SENTENCE, "0:0"), (AIJob.Kind.EDIT_PART, "0"), (AIJob.Kind.EDIT_SERIES, "")):
            with self.subTest(kind=kind), self.assertRaises(EditNotSupported) as raised:
                create_job(
                    owner=self.user, kind=kind, idempotency_key=f"edit-{kind}", instruction="проще",
                    project=project, base_revision=revision, target_id=target,
                )
            self.assertEqual(raised.exception.code, "edit_not_supported")
        self.assertEqual(AIJob.objects.count(), jobs)
        after = self.wallet()
        self.assertEqual((after.total_credits, after.reserved_credits), (before.total_credits, 0))
        self.assertEqual(Revision.objects.filter(project=project).count(), 1)


class ReviewOfAnAnswerTests(AnswerBase):
    def test_reviewer_gets_the_candidate_and_the_cited_fragments(self):
        with mock.patch.object(FakeProvider, "review", autospec=True, side_effect=FakeProvider.review) as review:
            project, job = self.ask(POLITICAL_ANIMAL)
        self.assertEqual(review.call_count, 1)
        candidate = review.call_args.args[1]
        sent = review.call_args.kwargs["sources"]
        cited = {source["citation_id"] for part in candidate["parts"] for source in part["sources"]}
        self.assertEqual({source["id"] for source in sent}, cited)
        self.assertTrue(all(source["text"] for source in sent))

    def test_failed_review_delivers_the_text_with_a_note(self):
        """The owner's decision stands for the review of content."""
        project, job = self.ask(POLITICAL_ANIMAL + " fail-review")
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertEqual(self.wallet().total_credits, 20)
        self.assertIn("Замечания проверки", project.head_revision.payload["note"])
        self.assertEqual(QualityReport.objects.get(revision=project.head_revision).review_json["verdict"], "fail")
        self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)

    def test_repaired_text_passes_the_source_check_again(self):
        project, job = self.ask(POLITICAL_ANIMAL + " repair-review")
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        payload = project.head_revision.payload
        self.assertTrue(any("repaired" in sentence["en"] for sentence in payload["parts"][0]["sentences"]))
        self.assertNotIn("note", payload)
        self.assertTrue(all(part["sources"] for part in payload["parts"]))
        self.assertEqual(EvidenceLink.objects.filter(revision=project.head_revision).count(), 3)

    def test_repair_that_breaks_the_sources_is_dropped(self):
        project, job = self.ask(POLITICAL_ANIMAL + " repair-review break-repair")
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        payload = project.head_revision.payload
        # The candidate from before the repair, with the note.
        self.assertFalse(any("repaired" in sentence["en"] for part in payload["parts"] for sentence in part["sentences"]))
        self.assertIn("Исправление не прошло проверку источников", payload["note"])
        self.assertTrue(all(part["sources"] for part in payload["parts"]))
        self.assertEqual(QualityReport.objects.get(revision=project.head_revision).review_json["verdict"], "repair")
        self.assertEqual(Revision.objects.filter(project=project).count(), 1)

    def test_no_repair_after_a_correction_attempt(self):
        """Answer, correction, review: a repair and a fresh review would not
        fit into AI_MAX_PROVIDER_CALLS_PER_JOB, so there is no repair."""
        with mock.patch.object(FakeProvider, "repair", autospec=True, side_effect=FakeProvider.repair) as repair:
            project, job = self.ask(POLITICAL_ANIMAL + " misquote-once repair-review")
        repair.assert_not_called()
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertIn("Замечания проверки", project.head_revision.payload["note"])


class AskAboutThisBookTests(TestCase):
    """The link on the page of a text: data, not a slug in a template."""

    def setUp(self):
        self.publication = publish_demo_fixture()
        self.user = User.objects.create_user(login="linkreader", password="TestPassword123!")

    def read(self, slug, signed_in):
        if signed_in:
            self.client.force_login(self.user)
        else:
            self.client.logout()
        return page(self.client.get(reverse("library:read", args=[slug])))

    def test_link_is_shown_to_a_signed_in_reader_of_a_text_with_a_book_on_the_shelf(self):
        other = self.publication.slug  # socrates-questions: no book retells it
        Publication.objects.filter(pk=self.publication.pk).update(slug=RELATED_TEXT)
        # The book is not on the shelf yet: nobody sees the link.
        self.assertNotIn("Ask about this book", self.read(RELATED_TEXT, signed_in=True))
        put_book_on_shelf()
        content = self.read(RELATED_TEXT, signed_in=True)
        self.assertIn(f'<a href="{reverse("workshop:create_project")}">Ask about this book</a>', content)
        self.assertIn('<span class="ru-ui" lang="ru">Задать вопрос об этой книге: Аристотель, «Политика»</span>', content)
        # A guest does not see it.
        guest = self.read(RELATED_TEXT, signed_in=False)
        self.assertNotIn("Ask about this book", guest)
        self.assertNotIn(reverse("workshop:create_project"), guest)
        # A text that no book on the shelf retells has no link.
        Publication.objects.filter(pk=self.publication.pk).update(slug=other)
        self.assertNotIn("Ask about this book", self.read(other, signed_in=True))

    def test_slug_is_not_written_in_code_or_templates(self):
        base = settings.BASE_DIR
        for path in ("templates/library/read.html", "apps/library/views.py", "apps/sources/shelf.py", "apps/workshop/views.py", "templates/workshop/create.html"):
            with open(os.path.join(base, path), encoding="utf-8") as handle:
                self.assertNotIn(RELATED_TEXT, handle.read(), path)


class AskStaysOffTests(AnswerBase):
    def test_ask_is_still_switched_off(self):
        self.assertFalse(settings.ASK_ENABLED)
        project, _ = self.ask(POLITICAL_ANIMAL)
        content = self.project_page(project)
        self.assertIn("Questions are not ready yet.", content)
        self.assertNotIn('name="instruction"', content)
        with self.assertRaises(AskDisabled):
            create_job(owner=self.user, kind=AIJob.Kind.ASK, idempotency_key="ask-off", instruction="?", project=project, base_revision=project.head_revision)


# --- the structural check, without a database ---------------------------------------------------

BOOK = (
    "From these things therefore it is clear that the city-state is a natural growth, and that man is by nature "
    "a political animal, and a man that is by nature and not merely by fortune citiless is either low in the scale "
    "of humanity or above it (like the ‘clanless, lawless, hearthless’ man reviled by Homer)—for one by nature "
    "unsocial is also ‘a lover of war’ inasmuch as he is solitary, like an isolated piece at draughts."
)
OTHER = "And why man is a political animal in a greater measure than any bee or any gregarious animal is clear. For nature, as we declare, does nothing without purpose."


def fragment(citation_id, text, segments=None):
    locator = {"work": "Politics", "book": "1", "bekker_start": "1252b", "bekker_end": "1253a", "segments": segments or [{"start": 0, "bekker": "1253a"}]}
    document = SimpleNamespace(pk=1, edition=SimpleNamespace(work=SimpleNamespace(label="Aristotle, Politics")))
    return Fragment(citation_id=citation_id, chunk=SimpleNamespace(text=text, locator_json=locator), document=document, scores={}, support=True)


def answer(sources_per_part, extra_sentence=None):
    parts = []
    for index, sources in enumerate(sources_per_part):
        sentences = [{"en": "Aristotle says that man lives in a city.", "ru": "Аристотель говорит, что человек живёт в городе."}]
        if extra_sentence and index == 0:
            sentences.append({"en": extra_sentence, "ru": "Перевод."})
        parts.append({"title_en": f"Part {index + 1}", "sentences": sentences, "sources": sources})
    return {"title_en": "A", "title_ru": "А", "parts": parts, "glossary": []}


GOOD = {"id": "S1", "quote": "man is by nature a political animal"}


class VerbatimTests(SimpleTestCase):
    def test_spaces_quotes_and_dashes_do_not_matter(self):
        for quote in (
            "man is by nature a political animal",
            "man is by nature\n   a political  animal",
            "like the 'clanless, lawless, hearthless' man reviled by Homer",
            "like the “clanless, lawless, hearthless” man reviled by Homer",
            "reviled by Homer) - for one by nature unsocial",
            "reviled by Homer)–for one by nature unsocial",
        ):
            with self.subTest(quote=quote):
                span = find_verbatim(BOOK, quote)
                self.assertIsNotNone(span)
                self.assertEqual(BOOK[span[0]:span[1]].split()[0], quote.split()[0])
                self.assertEqual(BOOK[span[0]:span[1]].split()[-1].strip("’”'"), quote.split()[-1].strip("’”'"))

    def test_words_case_punctuation_and_order_do_matter(self):
        for quote in (
            "man is by nature a social animal",  # another word
            "Man is by nature a political animal",  # another case
            "man is, by nature, a political animal",  # added punctuation
            "man is a political animal by nature",  # another order
            "man is by nature a political animal and a man that is",  # a dropped comma
            "man is by nature ... a political animal",  # an omission mark
            "",
            "   ",
        ):
            with self.subTest(quote=quote):
                self.assertIsNone(find_verbatim(BOOK, quote))

    def test_span_points_into_the_original_text(self):
        text = "First   line\nsecond ‘line’ — third line."
        start, end = find_verbatim(text, "second 'line' - third")
        self.assertEqual(text[start:end], "second ‘line’ — third")

    def test_quoted_phrases(self):
        self.assertEqual(quoted_phrases('He says: "man is by nature a political animal".'), [("man is by nature a political animal",)])
        self.assertEqual(quoted_phrases("He says: “man is a political animal.”"), [("man is a political animal",)])
        self.assertEqual(quoted_phrases("He says: ‘a lover of war’."), [("a lover of war",)])
        self.assertEqual(quoted_phrases("He says: 'a lover of war'."), [("a lover of war",)])
        self.assertEqual(quoted_phrases("По-русски: «человек по природе есть»."), [("человек по природе есть",)])
        self.assertEqual(
            quoted_phrases("First “a lover of war”, then ‘clanless, lawless, hearthless’."),
            [("a lover of war",), ("clanless, lawless, hearthless",)],
        )
        # Shorter than three words: a term, not a quotation.
        self.assertEqual(quoted_phrases("The word “city-state” and the words “political animal”."), [])
        # An apostrophe is not a quotation mark.
        self.assertEqual(quoted_phrases("Aristotle's book says that it's man's nature, and the poets' words agree."), [])
        self.assertEqual(quoted_phrases("Aristotle’s book says that it’s man’s nature, and the poets’ words agree."), [])
        self.assertEqual(quoted_phrases("He says: ‘it’s a lover of war’."), [("it’s a lover of war",)])
        # A closing mark looks like the apostrophe of a plural possessive.
        for marks in ("‘’", "''"):
            with self.subTest(marks=marks):
                left, right = marks
                # A mark that cannot be a possessive ends the phrase: the whole of it is the quotation.
                self.assertEqual(
                    quoted_phrases(f"He says: {left}the sons and sons{right} sons live together{right}."),
                    [(f"the sons and sons{right} sons live together",)],
                )
                self.assertEqual(quoted_phrases(f"He says {left}a lover of war{right} about the poets{right} man."), [("a lover of war",)])
                # Only possible possessives after the opening mark: each is a reading.
                self.assertEqual(
                    quoted_phrases(f"He says {left}the rule of the sons{right} and the poets{right} man agrees."),
                    [("the rule of the sons", f"the rule of the sons{right} and the poets")],
                )


class CheckAnswerTests(SimpleTestCase):
    def setUp(self):
        self.supports = [fragment("S1", BOOK), fragment("S2", OTHER)]

    def test_good_answer_passes_and_keeps_the_books_own_words(self):
        checked = check_answer(answer([[GOOD], [{"id": "S2", "quote": "nature, as we declare, does nothing without purpose"}], [GOOD]]), self.supports)
        self.assertTrue(checked.ok, checked.violations)
        self.assertEqual([len(sources) for sources in checked.parts], [1, 1, 1])
        source = checked.parts[0][0]
        self.assertEqual((source.excerpt, source.label, source.locator), ("man is by nature a political animal", "Aristotle, Politics, 1.1253a", "Politics 1.1253a"))
        self.assertEqual([item.citation_id for item in checked.cited_fragments()], ["S1", "S2"])

    def test_excerpt_is_taken_from_the_book_not_from_the_model(self):
        checked = check_answer(answer([[{"id": "S1", "quote": "like the 'clanless, lawless, hearthless' man reviled by Homer"}], [GOOD], [GOOD]]), self.supports)
        self.assertTrue(checked.ok, checked.violations)
        self.assertEqual(checked.parts[0][0].excerpt, "like the ‘clanless, lawless, hearthless’ man reviled by Homer")

    def test_exact_page_of_the_excerpt(self):
        turn = BOOK.index("and a man that is")
        supports = [fragment("S1", BOOK, segments=[{"start": 0, "bekker": "1252b"}, {"start": turn, "bekker": "1253a"}])]
        checked = check_answer(
            answer([
                [{"id": "S1", "quote": "the city-state is a natural growth"}],
                [{"id": "S1", "quote": "low in the scale of humanity or above it"}],
                [{"id": "S1", "quote": "a political animal, and a man that is by nature"}],
            ]),
            supports,
        )
        self.assertTrue(checked.ok, checked.violations)
        self.assertEqual(
            [sources[0].label for sources in checked.parts],
            ["Aristotle, Politics, 1.1252b", "Aristotle, Politics, 1.1253a", "Aristotle, Politics, 1.1252b–1253a"],
        )
        self.assertEqual(span_place(supports[0].chunk.locator_json, 0, 5), "1.1252b")

    def test_id_outside_the_set_rejects_the_answer(self):
        checked = check_answer(answer([[GOOD, {"id": "S7", "quote": GOOD["quote"]}], [GOOD], [GOOD]]), self.supports)
        self.assertFalse(checked.ok)
        self.assertIn("part 1: source id 'S7' is not one of the provided ids", checked.violations)

    def test_invalid_quote_is_dropped_but_a_part_needs_one_valid_source(self):
        bad = {"id": "S1", "quote": "man is by nature a social animal"}
        # Another valid source in the same part: the bad one is just not kept.
        checked = check_answer(answer([[bad, GOOD], [GOOD], [GOOD]]), self.supports)
        self.assertTrue(checked.ok, checked.violations)
        self.assertEqual(len(checked.parts[0]), 1)
        # The only source of the part: the answer is rejected.
        checked = check_answer(answer([[bad], [GOOD], [GOOD]]), self.supports)
        self.assertFalse(checked.ok)
        self.assertIn("part 1: no valid source", checked.violations[0])

    def test_quote_from_another_fragment_is_not_valid(self):
        checked = check_answer(answer([[{"id": "S2", "quote": GOOD["quote"]}], [GOOD], [GOOD]]), self.supports)
        self.assertFalse(checked.ok)

    def test_quote_length_limits(self):
        long_text = " ".join(f"word{index}" for index in range(80))
        supports = [fragment("S1", long_text)]
        sixty = " ".join(long_text.split()[:60])
        self.assertTrue(check_answer(answer([[{"id": "S1", "quote": sixty}]] * 3), supports).ok)
        sixty_one = " ".join(long_text.split()[:61])
        checked = check_answer(answer([[{"id": "S1", "quote": sixty_one}]] * 3), supports)
        self.assertIn("the quote has 61 words, the limit is 60", checked.violations[0])
        checked = check_answer(answer([[{"id": "S1", "quote": "word1 word2 word3 word4"}]] * 3), supports)
        self.assertIn("at least 5 are required", checked.violations[0])

    def test_words_in_quotation_marks_must_stand_in_a_fragment(self):
        ok = check_answer(answer([[GOOD]] * 3, extra_sentence="Aristotle says: “Man is by nature a political animal.”"), self.supports)
        self.assertTrue(ok.ok, ok.violations)  # a capital letter at the start of a sentence is allowed
        ok = check_answer(answer([[GOOD]] * 3, extra_sentence="He is “a lover of war”, Aristotle says."), self.supports)
        self.assertTrue(ok.ok, ok.violations)
        bad = check_answer(answer([[GOOD]] * 3, extra_sentence="Aristotle says: “man is a social animal”."), self.supports)
        self.assertFalse(bad.ok)
        self.assertIn("part 1, sentence 2: the words in quotation marks are not in the sources word for word: “man is a social animal”", bad.violations)
        # The rule covers titles too.
        series = answer([[GOOD]] * 3)
        series["title_en"] = "“Man is a social animal”"
        self.assertFalse(check_answer(series, self.supports).ok)

    def test_shape(self):
        self.assertFalse(check_answer(answer([[GOOD]] * 2), self.supports).ok)  # two parts
        self.assertFalse(check_answer(answer([[GOOD]] * 6), self.supports).ok)
        self.assertFalse(check_answer({"parts": "x"}, self.supports).ok)
        self.assertFalse(check_answer(None, self.supports).ok)
        series = answer([[GOOD]] * 3)
        series["parts"][1]["sentences"][0]["ru"] = " "
        self.assertFalse(check_answer(series, self.supports).ok)
        series = answer([[GOOD]] * 3)
        del series["parts"][2]["sources"]
        self.assertFalse(check_answer(series, self.supports).ok)

    def test_threshold_either_half_is_enough(self):
        with override_settings(SHELF_MIN_VECTOR_SIMILARITY=0.2, SHELF_MIN_FTS_COVERAGE=0.5):
            self.assertTrue(grounding.is_support({"vector_raw": 0.2, "fts_coverage": 0.0}))
            self.assertTrue(grounding.is_support({"vector_raw": None, "fts_coverage": 0.5}))
            self.assertTrue(grounding.is_support({"vector_raw": 0.01, "fts_coverage": 0.9}))
            self.assertFalse(grounding.is_support({"vector_raw": 0.19, "fts_coverage": 0.49}))
            self.assertFalse(grounding.is_support({"vector_raw": None, "fts_coverage": 0.0}))
            self.assertFalse(grounding.is_support({}))


# --- the live provider, with the HTTP layer patched ---------------------------------------------


def grounded(parts=None, outcome="series", message=""):
    series = {"title_en": "Man and the city", "title_ru": "Человек и город", "parts": parts or [], "glossary": []}
    return {"outcome": outcome, "series": series, "message_ru": message}


class LiveBase(AnswerBase):
    """The real OpenRouterProvider with `_chat_completion` and the prices
    patched. Subclasses set the live settings (`override_settings(**LIVE)`)."""

    def parts_from(self, job_sources, quote_words=12):
        parts = []
        for index in range(3):
            source = job_sources[index % len(job_sources)]
            words = source["text"].split()
            parts.append({
                "title_en": f"Step {index + 1}",
                "sentences": [{"en": "Aristotle says that man lives in a city.", "ru": "Аристотель говорит, что человек живёт в городе."}],
                "sources": [{"id": source["id"], "quote": " ".join(words[index * 5:index * 5 + quote_words])}],
            })
        return parts

    def supports_of(self, job):
        items = RetrievalItem.objects.filter(job=job).select_related("source_chunk").order_by("rank")
        return [{"id": item.citation_id, "text": item.source_chunk.text} for item in items if item.score_json["support"]]

    def run_live(self, question, answers):
        """The model's answers may depend on the fragments, which are known
        only after the search: `answers` is a function of them."""
        project, job = self.ask(question, run=False)
        calls = []
        self.call_options = []  # the keyword arguments of each call (max_tokens)

        def chat(model, system, user_payload, schema, **options):
            calls.append((model, system, user_payload, schema))
            self.call_options.append(options)
            reply = answers(user_payload, len(calls))
            if isinstance(reply, Exception):
                raise reply
            return fake_completion(reply)

        with mock.patch("apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]), mock.patch("apps.ai.openrouter._chat_completion", side_effect=chat):
            execute_job(claim_next_job())
        job.refresh_from_db()
        project.refresh_from_db()
        return project, job, calls


@override_settings(**LIVE)
class LiveGroundedContractTests(LiveBase):
    """The real OpenRouterProvider: what is sent and what the calls cost."""

    def live(self, *answers):
        replies = [fake_completion(answer) for answer in answers]
        return mock.patch("apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]), mock.patch(
            "apps.ai.openrouter._chat_completion", side_effect=replies
        )

    def test_generation_sends_question_and_fragments_and_gets_sources_back(self):
        def answers(user_payload, number):
            return grounded(self.parts_from(user_payload["sources"])) if number == 1 else PASS_REVIEW

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual((job.status, job.outcome), (AIJob.Status.SUCCEEDED, AIJob.Outcome.DELIVERED), job.result_reference)
        self.assertEqual(len(calls), 2)  # the answer and its review

        model, system, user_payload, schema = calls[0]
        self.assertEqual(model, "anthropic/claude-sonnet-5.5")
        for rule in (
            "Answer only from the fragments",
            "Do\n   not attribute to the author anything that is not in the fragments",
            "Quotation marks in your English text are only for words copied exactly",
            "Do not approve of it, do not soften it",
            'return outcome "no_source"',
            "CEFR A1-A2",
            "untrusted data",
        ):
            self.assertIn(rule, system)
        self.assertEqual(user_payload["operation"], "answer_from_sources")
        self.assertEqual(user_payload["question"], POLITICAL_ANIMAL)
        supports = self.supports_of(job)
        self.assertEqual([source["id"] for source in user_payload["sources"]], [source["id"] for source in supports])
        self.assertEqual(set(user_payload["sources"][0]), {"id", "locator", "text"})
        self.assertNotIn("correction", user_payload)
        # The schema is built for this request: an id is one of the ids that were sent.
        self.assertEqual(schema["name"], "grounded_answer")
        part_schema = schema["schema"]["properties"]["series"]["properties"]["parts"]["items"]
        self.assertIn("sources", part_schema["required"])
        self.assertEqual(part_schema["properties"]["sources"]["items"]["properties"]["id"]["enum"], [source["id"] for source in supports])
        self.assertEqual((part_schema["properties"]["sources"]["minItems"], part_schema["properties"]["sources"]["maxItems"]), (1, 3))
        self.assertEqual(schema["schema"]["properties"]["outcome"]["enum"], ["series", "no_source"])

        # The reviewer gets the candidate and the fragments its parts cite.
        model, system, user_payload, schema = calls[1]
        self.assertEqual(model, "anthropic/claude-haiku-5.5")
        self.assertIn("confirmed by the fragments that this part cites", system)
        self.assertEqual(user_payload["operation"], "review_answer_from_sources")
        self.assertEqual(user_payload["question"], POLITICAL_ANIMAL)
        cited = {source["citation_id"] for part in user_payload["candidate"]["parts"] for source in part["sources"]}
        self.assertEqual({source["id"] for source in user_payload["sources"]}, cited)
        self.assertEqual(schema["name"], "review_result")

        # Each call is priced by its own model: 5000 in + 1000 out.
        costs = list(ProviderAttempt.objects.filter(job=job).order_by("stage_number").values_list("model", "cost_usd", "state"))
        self.assertEqual(costs, [
            ("anthropic/claude-sonnet-5.5", Decimal("0.020000"), "completed"),
            ("anthropic/claude-haiku-5.5", Decimal("0.001000"), "completed"),
        ])
        self.assertEqual(BudgetBucket.objects.order_by("period").first().consumed_usd, Decimal("0.021000"))
        report = QualityReport.objects.get(revision=project.head_revision)
        self.assertEqual((report.prompt_version, report.review_model), ("review-grounded-v1", "anthropic/claude-haiku-5.5"))
        self.assertEqual(len(SOURCE_LINE.findall(self.project_page(project))), 3)
        self.assertEqual(self.wallet().total_credits, 20)

    def test_model_refusal_costs_the_owner_but_not_the_reader(self):
        before = self.wallet()
        project, job, calls = self.run_live(POLITICAL_ANIMAL, lambda payload, number: grounded(outcome="no_source", message="Фрагменты говорят о другом."))
        self.assertEqual(len(calls), 1)
        self.assertEqual((job.status, job.outcome), (AIJob.Status.SUCCEEDED, AIJob.Outcome.NO_SOURCE), job.result_reference)
        self.assertEqual(job.result_reference["refusal_line"], "model")
        self.assert_nothing_was_made_or_charged(project, job, before)
        # The owner's spending is recorded as usual.
        self.assertEqual(ProviderAttempt.objects.get(job=job).cost_usd, Decimal("0.020000"))
        self.assertEqual(BudgetBucket.objects.order_by("period").first().consumed_usd, Decimal("0.020000"))
        # The reader gets the fixed text; the model's own words are kept for the owner's report only.
        self.assertEqual(Message.objects.get(project=project, kind="explanation").body, grounding.NO_SOURCE_MESSAGE)
        self.assertEqual(job.result_reference["model_explanation"], "Фрагменты говорят о другом.")
        html = self.project_page(project)
        self.assertIn("We have no source for this yet.", html)
        self.assertNotIn("Фрагменты говорят о другом.", html)

    def test_rejected_answer_gets_one_correction_with_the_violations(self):
        def answers(user_payload, number):
            parts = self.parts_from(user_payload["sources"])
            if number == 1:
                parts[0]["sources"][0]["quote"] = "these words were never written in the book at all"
                return grounded(parts)
            return grounded(parts) if number == 2 else PASS_REVIEW

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertEqual(len(calls), 3)
        correction = calls[1][2]["correction"]
        self.assertIn("part 1: no valid source", correction["violations"][0])
        self.assertEqual(correction["previous_answer"]["parts"][0]["sources"][0]["quote"], "these words were never written in the book at all")
        self.assertEqual(calls[1][3]["name"], "grounded_answer")
        self.assertEqual(ProviderAttempt.objects.filter(job=job).count(), 3)
        self.assertEqual(sum(attempt.cost_usd for attempt in ProviderAttempt.objects.filter(job=job)), Decimal("0.041000"))

    def test_answer_rejected_twice_is_not_saved_and_the_calls_are_recorded(self):
        before = self.wallet()

        def answers(user_payload, number):
            parts = self.parts_from(user_payload["sources"])
            parts[2]["sources"] = [{"id": "S42", "quote": parts[2]["sources"][0]["quote"]}]
            return grounded(parts)

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual(len(calls), 2)
        self.assertEqual((job.outcome, job.error_code), (AIJob.Outcome.NO_CHARGE_FAILURE, "source_validation_failed"), job.result_reference)
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assertEqual(ProviderAttempt.objects.filter(job=job).count(), 2)

    def test_threshold_refusal_makes_no_call(self):
        before = self.wallet()
        project, job, calls = self.run_live(KANT, lambda payload, number: self.fail("the model must not be called"))
        self.assertEqual(calls, [])
        self.assertEqual(job.outcome, AIJob.Outcome.NO_SOURCE, job.result_reference)
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assertFalse(BudgetBucket.objects.filter(consumed_usd__gt=0).exists())

    def test_repair_keeps_the_grounded_shape_and_is_checked_again(self):
        repair_verdict = {**PASS_REVIEW, "verdict": "repair", "issues": [{
            "severity": "blocking", "category": "factual", "part_key": "part-1", "sentence_key": "",
            "reason_ru": "Часть говорит больше, чем источник.", "proposed_fix": "",
        }]}

        def answers(user_payload, number):
            if number in (1, 3):
                parts = self.parts_from(user_payload["sources"])
                if number == 3:
                    parts[0]["sentences"][0]["en"] = "Aristotle says that man is made for the city."
                return grounded(parts)
            return repair_verdict if number == 2 else PASS_REVIEW

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertEqual(len(calls), 4)
        model, system, user_payload, schema = calls[2]
        self.assertEqual(user_payload["operation"], "repair")
        self.assertEqual(user_payload["question"], POLITICAL_ANIMAL)
        self.assertTrue(user_payload["sources"])
        self.assertIn("quote", user_payload["candidate"]["parts"][0]["sources"][0])  # the model's own {id, quote}
        self.assertIn("This candidate is an answer to `question` written only from `sources`", system)
        self.assertEqual(schema["name"], "grounded_answer")
        payload = project.head_revision.payload
        self.assertEqual(payload["parts"][0]["sentences"][0]["en"], "Aristotle says that man is made for the city.")
        self.assertNotIn("note", payload)
        self.assertTrue(all(part["sources"] for part in payload["parts"]))


class PromptDocumentTests(SimpleTestCase):
    """The prompts in the code and in docs/spec/prompts/ are the same text."""

    def test_grounded_prompts_are_documented_word_for_word(self):
        from apps.ai import openrouter

        with open(os.path.join(settings.BASE_DIR, "docs", "spec", "prompts", "grounded-answer.md"), encoding="utf-8", newline="") as handle:
            raw = handle.read()
        self.assertIn("\r\n", raw)  # the neighbours in this folder are CRLF
        document = raw.replace("\r\n", "\n")
        for name in ("GROUNDED_SYSTEM_PROMPT", "GROUNDED_REVIEW_SYSTEM_PROMPT", "GROUNDED_REPAIR_ADDENDUM"):
            with self.subTest(prompt=name):
                self.assertIn(getattr(openrouter, name).strip(), document)
        self.assertIn(openrouter.GROUNDED_PROMPT_VERSION, document)
        self.assertIn(openrouter.GROUNDED_REVIEW_PROMPT_VERSION, document)


# --- the control run ----------------------------------------------------------------------------


class SnapshotLinksTests(SimpleTestCase):
    def test_only_web_addresses_of_a_snapshot_become_links(self):
        from apps.workshop.views import _parts_with_sources

        payload = {
            "parts": [{"sources": [{"book": "A book", "label": "A book, 1"}]}],
            "source_editions": [
                {
                    "book": "A book",
                    "attribution": "A book. A licence.",
                    "links": [["Publisher", "https://example.org/"], ["Script", "javascript:alert(1)"], "broken", ["File", "ftp://example.org/file"]],
                }
            ],
        }
        source = _parts_with_sources(payload)[0]["sources"][0]
        self.assertEqual(source["attribution"], "A book. A licence.")
        self.assertEqual(source["links"], [("Publisher", "https://example.org/")])
        # A revision made before the links were stored still renders.
        del payload["source_editions"][0]["links"]
        self.assertEqual(_parts_with_sources(payload)[0]["sources"][0]["links"], [])
