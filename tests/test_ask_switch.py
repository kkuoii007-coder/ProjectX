"""Stage C: the paid question about a text is switched off.

With ASK_ENABLED off (the default) a request is refused before any job,
reservation or message exists — also for a direct form post. The last
test keeps the reason on record: the live path saves an empty answer and
charges for it.
"""

import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.ai.models import AIJob
from apps.ai.services import AskDisabled, claim_next_job, create_job, execute_job
from apps.credits.models import CreditReservation, CreditTransaction, Wallet, grant_welcome_credits
from apps.workshop.models import Message, Project, Revision

PAYLOAD = {
    "title_en": "A question",
    "title_ru": "Вопрос",
    "parts": [{"title_en": "One", "sentences": [{"en": "We read slowly.", "ru": "Мы читаем медленно."}]}],
    "terms": [],
    "glossary": [],
}
OFF_LINE = "Questions are not ready yet. You can still read your text."


class AskBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="asker", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="Проект", mode=Project.Mode.EXPLAIN)
        self.revision = Revision.objects.create(project=self.project, payload=PAYLOAD)
        self.project.head_revision = self.revision
        self.project.save(update_fields=["head_revision"])
        Message.objects.create(project=self.project, owner=self.user, role="user", kind="ask", body="Старый вопрос")
        Message.objects.create(project=self.project, owner=self.user, role="assistant", kind="answer", body="Старый ответ")
        self.client.force_login(self.user)
        self.url = reverse("workshop:ask", args=[self.project.pk])
        self.ledger = CreditTransaction.objects.count()

    def assert_nothing_spent(self):
        self.assertEqual(AIJob.objects.count(), 0)
        self.assertEqual(CreditReservation.objects.count(), 0)
        self.assertEqual(CreditTransaction.objects.count(), self.ledger)
        self.assertEqual(Message.objects.filter(project=self.project).count(), 2)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual((wallet.total_credits, wallet.reserved_credits), (30, 0))


class AskSwitchedOffTests(AskBase):
    def test_project_opens_as_before_with_a_line_instead_of_the_form(self):
        response = self.client.get(reverse("workshop:project", args=[self.project.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, OFF_LINE)
        self.assertContains(response, "Вопросы по тексту пока не работают.")
        self.assertNotContains(response, f'action="{self.url}"')
        self.assertNotContains(response, 'name="instruction" maxlength="2000"')
        # The text, earlier questions and the edit forms are still there.
        self.assertContains(response, "We read slowly.")
        self.assertContains(response, "Старый вопрос")
        self.assertContains(response, "Старый ответ")
        self.assertContains(response, reverse("workshop:edit_fragment", args=[self.project.pk]))

    def test_direct_form_post_is_refused_before_job_and_reservation(self):
        response = self.client.post(self.url, {"instruction": "Что значит это слово?", "idempotency_key": "direct-post"})
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, OFF_LINE, status_code=403)
        self.assertContains(response, "We read slowly.", status_code=403)  # the reader keeps the text
        self.assert_nothing_spent()

    def test_service_refuses_ask_for_any_caller(self):
        with self.assertRaises(AskDisabled):
            create_job(owner=self.user, kind=AIJob.Kind.ASK, idempotency_key="svc", instruction="Вопрос", project=self.project, base_revision=self.revision)
        self.assert_nothing_spent()

    def test_other_paid_actions_are_not_affected(self):
        response = self.client.post(
            reverse("workshop:edit_fragment", args=[self.project.pk]),
            {"instruction": "Проще", "target": "0:0", "kind": AIJob.Kind.EDIT_SENTENCE},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIJob.objects.get().kind, AIJob.Kind.EDIT_SENTENCE)

    def test_credit_help_does_not_price_a_switched_off_action(self):
        self.assertNotContains(self.client.get(reverse("library:credits_help")), "Вопрос:")
        with override_settings(ASK_ENABLED=True):
            self.assertContains(self.client.get(reverse("library:credits_help")), "Вопрос:")


@override_settings(ASK_ENABLED=True)
class AskSwitchedOnTests(AskBase):
    def test_form_and_fake_answer_work_when_enabled(self):
        response = self.client.get(reverse("workshop:project", args=[self.project.pk]))
        self.assertContains(response, f'action="{self.url}"')
        self.assertNotContains(response, OFF_LINE)
        self.assertEqual(self.client.post(self.url, {"instruction": "Что значит это слово?"}).status_code, 302)
        execute_job(claim_next_job())
        answer = Message.objects.filter(project=self.project, kind="answer").latest("created_at")
        self.assertIn("Тестовый ответ FakeProvider", answer.body)

    @unittest.expectedFailure
    @override_settings(
        LIVE_AI_ENABLED=True,
        AI_PROVIDER="openrouter",
        OPENROUTER_API_KEY="test-key",
        OWNER_DAILY_BUDGET_USD="1",
        OWNER_MONTHLY_BUDGET_USD="10",
    )
    def test_live_ask_must_not_save_an_empty_answer_or_charge_for_it(self):
        """Known defect, the reason for the switch (not fixed on purpose).

        The live provider answers a question through the series-generation
        path, so the answer text is empty; the job is still "delivered",
        an empty message is saved and one credit is charged. The
        assertions state the correct behaviour and fail today. When ask is
        repaired this test starts passing: remove expectedFailure then.
        """
        series = {
            "outcome": "series",
            "message_ru": "",
            "series": {"title_en": "T", "title_ru": "Т", "glossary": [], "parts": [{"title_en": "P", "sentences": [{"en": "One.", "ru": "Один."}]}] * 3},
        }
        review = {"verdict": "pass", "factual_state": "unverified", "issues": [], "unsupported_claims": [], "language_notes": [], "translation_issues": [], "uncertainty_notes": []}

        def completion(model, system, user_payload, schema):
            content = review if schema["name"] == "review_result" else series
            return {"id": "gen-1", "usage": {"prompt_tokens": 100, "completion_tokens": 100}, "choices": [{"message": {"content": json.dumps(content)}}]}, 0.1

        self.client.post(self.url, {"instruction": "Что значит это слово?"})
        with patch("apps.ai.openrouter._pricing_for", return_value=(Decimal("0.000002"), Decimal("0.00001"))), patch(
            "apps.ai.openrouter._chat_completion", side_effect=completion
        ):
            execute_job(claim_next_job())
        answer = Message.objects.filter(project=self.project, kind="answer").latest("created_at")
        wallet = Wallet.objects.get(user=self.user)
        self.assertTrue(answer.body.strip(), "an empty answer was saved")
        self.assertEqual(wallet.total_credits, 30, "a credit was charged for an empty answer")
