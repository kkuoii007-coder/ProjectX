"""The page of a workshop project after the interface review of PR 3.

- An action that was refused (409, 402, 400, 404) leaves the text on the page;
  «The text was not made. No credits were taken.» is shown only when it is true.
- An answer is printed with its sources opened and without the service blocks.
- With an empty shelf nothing invites to ask a question.
- A source line: a separator before its caption, a 44px target.
- A long question: the heading is cut, the question is shown in full.
- An answer with sources has no «Сравнить» link.

No network and no paid call: FakeProvider and patched embeddings."""

import re
from unittest import mock
from uuid import uuid4

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.ai.models import AIJob
from apps.ai.services import create_job
from apps.credits.models import CreditReservation, Wallet, grant_welcome_credits
from apps.sources import retrieval
from apps.sources.models import SourceDocument
from apps.workshop.models import Project, Revision

from .helpers import no_embeddings
from .test_answer import KANT, POLITICAL_ANIMAL, AnswerBase, page
from .test_workshop import make_base_revision

FALSE_SCREEN = ("The text was not made.", "Текст не создан.", "No credits were taken.", "Кредиты не списаны.", "Ask another question")


def refusal_section(content: str) -> str:
    """The final screen without a text, without the header and the footer."""
    return re.search(r'<section class="refusal".*?</section>', content, re.S).group()


class RefusedActionKeepsTheTextTests(TestCase):
    """A text made before «answer with a source»: no sources, edit forms."""

    def setUp(self):
        self.user = User.objects.create_user(login="oldtext", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.client.force_login(self.user)
        self.project = Project.objects.create(owner=self.user, title="Сократ о справедливости", mode=Project.Mode.EXPLAIN)
        self.revision = make_base_revision(self.project)
        self.project.head_revision = self.revision
        self.project.save(update_fields=["head_revision"])
        self.edit_url = reverse("workshop:edit_fragment", args=[self.project.pk])
        self.apply_url = reverse("workshop:apply_head", args=[self.project.pk])

    def assert_text_is_on_the_page(self, response, status, error):
        self.assertEqual(response.status_code, status)
        content = page(response)
        self.assertIn(error, content)
        # The text, its edit forms, the versions and the discussion are where they were.
        for present in ("workshop-reading", "First idea.", "Первая мысль.", "Third idea.", "Изменить предложение", "Версии", "Обсуждение", "Сравнить"):
            self.assertIn(present, content, present)
        self.assertEqual(content.count('name="kind" value="edit_sentence"'), 3)
        for absent in (*FALSE_SCREEN, 'class="refusal"', "Текст ещё не создан"):
            self.assertNotIn(absent, content, absent)

    def test_conflict_409_keeps_the_text(self):
        # Another job of the same reader is still in the queue.
        other = Project.objects.create(owner=self.user, title="Другой проект", mode=Project.Mode.EXPLAIN)
        create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex, instruction="Кого Аристотель считает гражданином?", project=other)
        response = self.client.post(self.edit_url, {"kind": "edit_sentence", "target": "0:0", "instruction": "проще"})
        self.assert_text_is_on_the_page(response, 409, "Дождитесь завершения текущей задачи")
        self.assertEqual(AIJob.objects.count(), 1)

    def test_no_credits_402_keeps_the_text(self):
        Wallet.objects.filter(user=self.user).update(total_credits=0)
        for data in (
            {"kind": "edit_sentence", "target": "0:0", "instruction": "проще"},
            {"kind": "edit_part", "target": "1", "instruction": "короче"},
        ):
            with self.subTest(kind=data["kind"]):
                response = self.client.post(self.edit_url, data)
                self.assert_text_is_on_the_page(response, 402, "Недостаточно кредитов для этого действия")
        self.assertFalse(AIJob.objects.exists())

    def test_bad_request_400_keeps_the_text(self):
        cases = (
            ({"kind": "edit_sentence", "target": "0:0", "instruction": ""}, "Введите инструкцию до 1000 символов"),
            ({"kind": "edit_sentence", "target": "0:0", "instruction": "я" * 1001}, "Введите инструкцию до 1000 символов"),
            ({"kind": "edit_sentence", "target": "нет", "instruction": "проще"}, "Некорректная цель правки"),
            ({"kind": "edit_sentence", "target": "0:9", "instruction": "проще"}, "Некорректная цель правки"),
            ({"kind": "edit_part", "target": "9", "instruction": "проще"}, "Некорректная цель правки"),
        )
        for data, error in cases:
            with self.subTest(data=data):
                self.assert_text_is_on_the_page(self.client.post(self.edit_url, data), 400, error)
        self.assertFalse(AIJob.objects.exists())

    def test_refused_apply_keeps_the_text(self):
        self.assert_text_is_on_the_page(self.client.post(self.apply_url, {"revision_id": ""}), 400, "Не указана ревизия")
        self.assert_text_is_on_the_page(self.client.post(self.apply_url, {"revision_id": str(uuid4())}), 404, "Ревизия не найдена")
        self.assert_text_is_on_the_page(self.client.post(self.apply_url, {"revision_id": "не uuid"}), 404, "Ревизия не найдена")

    def test_refused_question_keeps_the_text(self):
        """`ask` is switched off; its page is the same whole page."""
        response = self.client.post(reverse("workshop:ask", args=[self.project.pk]), {"instruction": "Почему?"})
        self.assertEqual(response.status_code, 403)
        content = page(response)
        self.assertIn("First idea.", content)
        for absent in FALSE_SCREEN:
            self.assertNotIn(absent, content)

    def test_old_text_has_no_question_line(self):
        content = page(self.client.get(reverse("workshop:project", args=[self.project.pk])))
        self.assertNotIn('class="asked"', content)
        self.assertIn("<h1>Сократ о справедливости</h1>", content)


class NoTextScreenIsTrueTests(TestCase):
    """«The text was not made. No credits were taken.» only when the job
    that had to make the text is over, left no text and took nothing."""

    def setUp(self):
        self.user = User.objects.create_user(login="notext", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.client.force_login(self.user)
        self.project = Project.objects.create(owner=self.user, title="Вопрос", mode=Project.Mode.EXPLAIN)
        self.url = reverse("workshop:project", args=[self.project.pk])

    def job(self, **fields):
        job, _ = create_job(owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex, instruction="Вопрос", project=self.project)
        if fields:
            AIJob.objects.filter(pk=job.pk).update(**fields)
        return job

    def test_project_without_a_job_is_waiting(self):
        content = page(self.client.get(self.url))
        self.assertIn("Текст ещё не создан", content)
        for absent in FALSE_SCREEN:
            self.assertNotIn(absent, content)

    def test_queued_and_running_jobs_are_waiting(self):
        job = self.job()
        for status in (AIJob.Status.QUEUED, AIJob.Status.RUNNING):
            AIJob.objects.filter(pk=job.pk).update(status=status)
            content = page(self.client.get(self.url))
            self.assertIn("Текст ещё не создан", content)
            for absent in FALSE_SCREEN:
                self.assertNotIn(absent, content)

    def test_failed_job_with_a_released_reservation(self):
        job = self.job(status=AIJob.Status.FAILED, outcome=AIJob.Outcome.NO_CHARGE_FAILURE, error_code="provider_error")
        CreditReservation.objects.filter(job=job).update(state=CreditReservation.State.RELEASED)
        content = page(self.client.get(self.url))
        self.assertIn("The text was not made.", content)
        self.assertIn("No credits were taken.", content)
        self.assertNotIn("Версии", content)

    def test_no_word_about_credits_while_the_reservation_is_held(self):
        # Reconciliation: the job is over for the reader, the reservation is not released.
        job = self.job(status=AIJob.Status.RECONCILIATION)
        self.assertEqual(CreditReservation.objects.get(job=job).state, CreditReservation.State.ACTIVE)
        content = page(self.client.get(self.url))
        self.assertIn("The text was not made.", content)
        self.assertNotIn("No credits were taken.", content)
        self.assertNotIn("Кредиты не списаны.", content)

    def test_no_word_about_credits_when_they_were_taken(self):
        job = self.job(status=AIJob.Status.FAILED)
        CreditReservation.objects.filter(job=job).update(state=CreditReservation.State.CAPTURED)
        content = page(self.client.get(self.url))
        self.assertNotIn("No credits were taken.", content)


class PrintTests(SimpleTestCase):
    """Checked on the source: the suite has no browser."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        styles = (settings.BASE_DIR / "static" / "css" / "main.css").read_text(encoding="utf-8")
        cls.styles = styles
        start = styles.index("@media print {")
        cls.print_rules = re.sub(r"/\*.*?\*/", "", styles[start:styles.index("\n}\n", start)], flags=re.S)
        cls.script = (settings.BASE_DIR / "static" / "js" / "reader.js").read_text(encoding="utf-8")
        cls.template = (settings.BASE_DIR / "templates" / "workshop" / "project.html").read_text(encoding="utf-8")

    def rule(self, selector: str) -> str:
        """The body of the print rule whose selector list has `selector`."""
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", self.print_rules):
            if selector in [piece.strip() for piece in selectors.split(",")]:
                return body
        self.fail(f"no print rule for {selector}")

    def test_sources_are_printed_opened(self):
        body = self.rule(".source::details-content")
        self.assertIn("content-visibility: visible", body)
        self.assertIn("display: block", body)
        # The excerpt and the edition are not hidden on paper.
        for selector in (".source__excerpt", ".source__attribution", ".edition-links", ".sources", ".source"):
            for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", self.print_rules):
                if selector in [piece.strip() for piece in selectors.split(",")]:
                    self.assertNotIn("display: none", body, selector)
        # Where ::details-content is not supported the script opens the lines.
        self.assertIn("'beforeprint'", self.script)
        self.assertIn("'afterprint'", self.script)
        self.assertIn("details.source:not([open])", self.script)

    def test_service_blocks_are_not_printed(self):
        for selector in (".workshop-back", ".workshop-service", ".workshop-edit", ".site-header", ".site-footer"):
            self.assertIn("display: none !important", self.rule(selector), selector)
        # The way back, the versions and the discussion carry these classes.
        self.assertIn('<p class="workshop-back">', self.template)
        service = self.template[self.template.index('<div class="wrap workshop-service">'):]
        self.assertIn("Версии", service)
        self.assertIn("Обсуждение", service)
        self.assertNotIn("Версии", self.template[:self.template.index('<div class="wrap workshop-service">')])

    def test_source_line_is_a_44px_target_at_every_width(self):
        screen = self.styles[:self.styles.index("@media print {")]
        body = re.search(r"\n\.source summary \{([^}]*)\}", screen).group(1)
        self.assertIn("min-height: 44px", body)
        # No narrower rule takes it back.
        self.assertEqual(len(re.findall(r"\.source summary \{", screen)), 1)


class EmptyShelfTests(AnswerBase):
    def empty_the_shelf(self):
        SourceDocument.objects.filter(pk=self.document.pk).update(processing_state=SourceDocument.Processing.FAILED)

    def test_question_page_does_not_invite_to_ask(self):
        full = page(self.client.get(self.create_url))
        for present in ("Задайте вопрос о книге с нашей полки", "Ответ с источниками", "workshop-terms", 'name="topic"'):
            self.assertIn(present, full)
        self.empty_the_shelf()
        content = page(self.client.get(self.create_url))
        # The page itself: the header and the footer of the site have their own links about credits.
        content = content[content.index('<header class="page-head">'):content.index("</section>")]
        self.assertIn("There are no books on our shelf yet.", content)
        self.assertIn("На полке пока нет книг", content)
        self.assertIn("Сейчас полка пуста.", content)
        self.assertIn(f'<a class="btn" href="{reverse("library:home")}" lang="en">Go to the library</a>', content)
        for absent in ("Задайте вопрос", "Ответ с источниками", "кредитов", "workshop-terms", "можно задать новый вопрос", 'name="topic"', "<form method=\"post\" class=\"workshop-form\""):
            self.assertNotIn(absent, content, absent)

    def test_refusal_screen_does_not_lead_to_a_page_without_a_form(self):
        asked = {"no_source": self.ask(KANT)[0]}
        with mock.patch.object(retrieval, "embed_texts", no_embeddings):
            asked["search_unavailable"] = self.ask(KANT)[0]
        for state, project in asked.items():
            with self.subTest(state=state, shelf="full"):
                section = refusal_section(self.project_page(project))
                self.assertIn(f'href="{self.create_url}"', section)
                self.assertNotIn("Go to the library", section)
        self.empty_the_shelf()
        for state, project in asked.items():
            with self.subTest(state=state, shelf="empty"):
                section = refusal_section(self.project_page(project))
                self.assertNotIn(self.create_url, section)
                for absent in ("Ask another question", "Ask again", "You can ask about a book on our shelf"):
                    self.assertNotIn(absent, section)
                self.assertIn(f'<a class="btn" href="{reverse("library:home")}" lang="en">Go to the library</a>', section)
                self.assertIn("No credits were taken.", section)


class SourceLineTests(AnswerBase):
    def test_separator_between_the_line_and_its_caption(self):
        project, _ = self.ask(POLITICAL_ANIMAL)
        content = self.project_page(project)
        lines = re.findall(r"<summary>(.*?)</summary>", content, re.S)
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertRegex(line, r'^<span lang="en">Source: Aristotle, Politics, 1\.125[23][ab](–125[23][ab])?</span> <span class="ru-ui" lang="ru">Источник</span>$')
            # What a screen reader, a copy and a page without CSS get.
            self.assertRegex(re.sub(r"<[^>]+>", "", line), r"^Source: Aristotle, Politics, \S+ Источник$")

    def test_answer_with_sources_has_no_compare_link(self):
        project, _ = self.ask(POLITICAL_ANIMAL)
        content = self.project_page(project)
        self.assertIn("Версии", content)
        self.assertIn("(текущая)", content)
        self.assertNotIn("Сравнить", content)
        self.assertNotIn(reverse("workshop:revision_diff", args=[project.pk, project.head_revision_id]), content)


class LongQuestionTests(AnswerBase):
    LONG = (
        "Почему Аристотель называет человека политическим животным и чем, по его словам, человек отличается от пчёл и других "
        "животных, которые живут вместе, и какую роль в этом играет речь, которой нет у других животных, и что из этого следует "
        "для государства, которое, как он говорит, существует по природе?"
    )

    def assert_question_is_whole(self, content):
        heading = re.search(r"<h1>(.*?)</h1>", content, re.S).group(1)
        self.assertEqual(len(heading), 80)
        self.assertTrue(heading.endswith("…"))
        self.assertTrue(self.LONG.startswith(heading[:-1]))
        self.assertIn(f'<span class="asked__label">Ваш вопрос</span> <span class="asked__text">{self.LONG}</span>', content)
        title = re.search(r"<title>(.*?)</title>", content, re.S).group(1)
        self.assertIn("…", title)
        self.assertNotIn(self.LONG, title)

    def test_answer(self):
        self.assertGreater(len(self.LONG), 200)
        project, job = self.ask(self.LONG)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertEqual(project.title, self.LONG[:200])
        self.assert_question_is_whole(self.project_page(project))

    def test_waiting(self):
        project, _ = self.ask(self.LONG, run=False)
        content = self.project_page(project)
        self.assertIn("Текст ещё не создан", content)
        self.assert_question_is_whole(content)

    def test_refusal(self):
        question = self.LONG.replace("Аристотель", "Кант")
        self.LONG = question
        project, job = self.ask(question)
        self.assertEqual(job.outcome, AIJob.Outcome.NO_SOURCE, job.result_reference)
        content = self.project_page(project)
        self.assertIn("We have no source for this yet.", content)
        self.assert_question_is_whole(content)

    def test_failure(self):
        with mock.patch.object(retrieval, "embed_texts", no_embeddings):
            self.LONG = self.LONG.replace("Аристотель", "Кант")
            project, job = self.ask(self.LONG)
        self.assertEqual(job.error_code, "search_unavailable", job.result_reference)
        content = self.project_page(project)
        self.assertIn("Search is not available now.", content)
        self.assert_question_is_whole(content)
        # A job that broke for another reason: the same line.
        AIJob.objects.filter(pk=job.pk).update(error_code="provider_error")
        content = self.project_page(project)
        self.assertIn("The text was not made.", content)
        self.assert_question_is_whole(content)

    def test_line_breaks_and_markup_of_a_question(self):
        question = "Кого Аристотель считает\nгражданином? <b>&"
        project, _ = self.ask(question, run=False)
        content = self.project_page(project)
        self.assertIn('<span class="asked__text">Кого Аристотель считает\nгражданином? &lt;b&gt;&amp;</span>', content)
        self.assertNotIn("<b>&", content)

    def test_short_question_is_not_cut(self):
        project, _ = self.ask(POLITICAL_ANIMAL)
        content = self.project_page(project)
        self.assertIn(f"<h1>{POLITICAL_ANIMAL}</h1>", content)
        self.assertIn(f'<span class="asked__text">{POLITICAL_ANIMAL}</span>', content)
        self.assertEqual(Revision.objects.filter(project=project).count(), 1)
