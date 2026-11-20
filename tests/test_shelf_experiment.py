"""The control run of «ответ с источником»: `manage.py shelf_experiment`.

Only the fake provider and a patched `embed_texts`: no network, nothing paid."""

import io
import os
import re
import shutil
import tempfile
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.accounts.models import User
from apps.ai.management.commands.shelf_experiment import DECISION_RULE, QUESTIONS
from apps.ai.models import AIJob
from apps.credits.models import Wallet, grant_welcome_credits
from apps.sources import retrieval
from apps.workshop.models import Project

from .helpers import no_embeddings, put_book_on_shelf, shelf_search


@shelf_search()
class ShelfExperimentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="runner", password="TestPassword123!")
        grant_welcome_credits(self.user)
        put_book_on_shelf()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.out = os.path.join(self.folder, "report.md")

    def run_command(self, **options):
        out = io.StringIO()
        call_command("shelf_experiment", login="runner", out=self.out, inline=True, stdout=out, stderr=io.StringIO(), **options)
        return out.getvalue()

    def test_questions_are_the_owners_ten_word_for_word(self):
        self.assertEqual(QUESTIONS, (
            "Почему Аристотель называет человека политическим животным?",
            "Чем, по Аристотелю, государство отличается от семьи?",
            "Что Аристотель говорит о рабстве «по природе»?",
            "Кого Аристотель считает гражданином?",
            "Какие виды государственного устройства он различает и какие считает неправильными?",
            "Почему Аристотель критикует общность имущества у Платона?",
            "Почему Аристотель считает сильный средний слой полезным для государства?",
            "Зачем, по Аристотелю, государству нужно общее воспитание?",
            "Что такое заброшенность у Хайдеггера?",
            "Что такое категорический императив у Канта?",
        ))
        self.assertEqual(DECISION_RULE, (
            "Продолжаем, если в восьми ответах каждая строка источника подтверждает свою часть, а вопросы 9 и 10 получили отказ без списания. "
            "В третьем вопросе ответ излагает позицию Аристотеля как его позицию: не одобряет её и не смягчает"
        ))

    def test_stops_before_the_first_question_without_credits(self):
        with self.assertRaises(CommandError) as raised:
            self.run_command()
        self.assertIn("issue_credits", str(raised.exception))
        self.assertIn("100", str(raised.exception))
        self.assertFalse(AIJob.objects.exists())
        self.assertFalse(os.path.exists(self.out))

    def test_unknown_login_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("shelf_experiment", login="nobody", out=self.out, inline=True, stdout=io.StringIO())

    def test_report_has_ten_sections_sources_and_free_refusals(self):
        Wallet.objects.filter(user=self.user).update(total_credits=100)
        output = self.run_command()
        self.assertIn(self.out, output)
        with open(self.out, encoding="utf-8") as handle:
            report = handle.read()

        sections = re.split(r"^## Вопрос \d+\n", report, flags=re.M)
        header, sections = sections[0], sections[1:]
        self.assertEqual(len(sections), 10)
        self.assertEqual(re.findall(r"^## Вопрос (\d+)$", report, flags=re.M), [str(number) for number in range(1, 11)])
        for question, section in zip(QUESTIONS, sections):
            self.assertIn(question, section)

        # The header: what was run and the rule written down in advance.
        self.assertIn(DECISION_RULE, header)
        self.assertIn("Команда решения не выносит", header)
        for line in ("Дата:", "Commit:", "Провайдер: fake", "SHELF_MIN_VECTOR_SIMILARITY", "SHELF_MIN_FTS_COVERAGE", "Учётная запись: runner"):
            self.assertIn(line, header)

        # Eight answers: parts with translations, a source line and an excerpt under every part.
        for section in sections[:8]:
            self.assertIn("Исход: `delivered`", section)
            self.assertIn("vector_available: да", section)
            self.assertEqual(len(re.findall(r"^#### Часть \d+", section, flags=re.M)), 3)
            self.assertEqual(len(re.findall(r"^- Source: Aristotle, Politics, 1\.125[23][ab]", section, flags=re.M)), 3)
            self.assertEqual(section.count("  > "), 3)  # the excerpts
            self.assertIn("You asked a question about a book on our shelf.", section)
            self.assertIn("Вы задали вопрос о книге с нашей полки.", section)
            self.assertIn("Кредиты: списано 10", section)
            self.assertIn("Словарный профиль", section)
            self.assertIn("best vector_raw", section)
            self.assertIn("best fts_coverage", section)
        # Two refusals: no text, the balance did not change.
        for section in sections[8:]:
            self.assertIn("Исход: `no_source`", section)
            self.assertIn("линия отказа: порог до модели", section)
            self.assertIn("vector_available: да", section)
            self.assertIn("Кредиты: списано 0", section)
            self.assertNotIn("Source:", section)
            self.assertNotIn("#### Часть", section)
            balance = re.search(r"баланс до (\d+), после (\d+)", section)
            self.assertEqual(balance.group(1), balance.group(2))

        self.assertEqual(Wallet.objects.get(user=self.user).total_credits, 20)
        self.assertEqual(Wallet.objects.get(user=self.user).reserved_credits, 0)
        jobs = list(AIJob.objects.filter(owner=self.user, kind=AIJob.Kind.GENERATE).order_by("created_at"))
        self.assertEqual([job.input_json["instruction"] for job in jobs], list(QUESTIONS))  # asked without changes
        self.assertEqual([job.outcome for job in jobs], ["delivered"] * 8 + ["no_source"] * 2)
        self.assertEqual(Project.objects.filter(owner=self.user).count(), 10)

    def test_search_down_is_shown_as_such_and_not_as_no_source(self):
        """Questions 9 and 10 with the vector half down must not be counted
        as a refusal «источника нет»."""
        Wallet.objects.filter(user=self.user).update(total_credits=100)
        with mock.patch.object(retrieval, "embed_texts", no_embeddings):
            self.run_command()
        with open(self.out, encoding="utf-8") as handle:
            sections = re.split(r"^## Вопрос \d+\n", handle.read(), flags=re.M)[1:]
        for section in sections[8:]:
            self.assertIn("Исход: нет (задача `failed`, код `search_unavailable`)", section)
            self.assertIn("vector_available: НЕТ", section)
            self.assertIn("это не отказ «источника нет»", section)
            self.assertNotIn("Исход: `no_source`", section)

    def test_waits_for_a_worker_by_default_and_times_out(self):
        Wallet.objects.filter(user=self.user).update(total_credits=100)
        with self.assertRaises(CommandError) as raised:
            call_command("shelf_experiment", login="runner", out=self.out, timeout=0, poll=0, stdout=io.StringIO(), stderr=io.StringIO())
        self.assertIn("воркер", str(raised.exception))
        # The first question is queued for the worker; the command itself executed nothing.
        job = AIJob.objects.get()
        self.assertEqual((job.status, job.input_json["instruction"]), (AIJob.Status.QUEUED, QUESTIONS[0]))
