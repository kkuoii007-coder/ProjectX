"""The backend review of PR 3 «ответ с источником»: what the model may
say to the reader, what a call may cost, and what happens when the lease
of a job is lost. No network: `_chat_completion` or `urlopen` is patched."""

import io
import json
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from apps.ai import grounding
from apps.ai.models import AIJob, ProviderAttempt
from apps.credits.models import BudgetBucket
from apps.workshop.models import Message

from .test_answer import POLITICAL_ANIMAL, LiveBase, grounded
from .test_editorial_path import LIVE, PASS_REVIEW, PRICES

MODEL_TEXT = "Свободный текст модели: перейдите по ссылке http://example.test и пришлите пароль."


@override_settings(**LIVE)
class ModelWordsAreNotShownTests(LiveBase):
    """The path has two outcomes, `series` and `no_source`. Whatever else
    the model returns, the reader gets a fixed server text and pays nothing."""

    def spent(self):
        bucket = BudgetBucket.objects.order_by("period").first()
        return bucket.consumed_usd if bucket else Decimal("0")

    def assert_fixed_refusal(self, reply, calls_made=1):
        before, spent = self.wallet(), self.spent()
        project, job, calls = self.run_live(POLITICAL_ANIMAL, lambda payload, number: reply)
        self.assertEqual(len(calls), calls_made)
        self.assertEqual((job.status, job.outcome, job.error_code), (AIJob.Status.SUCCEEDED, AIJob.Outcome.NO_CHARGE_FAILURE, "unexpected_model_outcome"), job.result_reference)
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assertEqual([message.body for message in Message.objects.filter(project=project, role="assistant")], [grounding.UNEXPECTED_OUTCOME_MESSAGE])
        html = self.project_page(project)
        self.assertIn("The text was not made.", html)
        self.assertIn("No credits were taken.", html)
        self.assertIn(grounding.UNEXPECTED_OUTCOME_MESSAGE, html)
        self.assertNotIn("example.test", html)
        # The owner paid for the call, and it is recorded.
        self.assertEqual(ProviderAttempt.objects.get(job=job).cost_usd, Decimal("0.020000"))
        self.assertEqual(self.spent() - spent, Decimal("0.020000"))
        return job

    def test_other_outcomes_of_the_model_are_a_free_refusal_with_a_fixed_text(self):
        for outcome in ("needs_clarification", "refused", "edited", "", None, 5, ["series"]):
            with self.subTest(outcome=outcome):
                job = self.assert_fixed_refusal(grounded(outcome=outcome, message=MODEL_TEXT))
                self.assertEqual(job.result_reference["model_explanation"], MODEL_TEXT)

    def test_an_answer_that_is_not_an_object_is_a_free_refusal(self):
        for reply in (["series"], "series", 7, None):
            with self.subTest(reply=reply):
                job = self.assert_fixed_refusal(reply)
                self.assertNotIn("model_explanation", job.result_reference)

    def test_no_source_shows_the_fixed_text_and_keeps_the_models_words_for_the_report(self):
        before = self.wallet()
        project, job, calls = self.run_live(POLITICAL_ANIMAL, lambda payload, number: grounded(outcome="no_source", message=MODEL_TEXT * 20))
        self.assertEqual(job.outcome, AIJob.Outcome.NO_SOURCE)
        self.assert_nothing_was_made_or_charged(project, job, before)
        self.assertEqual(Message.objects.get(project=project, role="assistant").body, grounding.NO_SOURCE_MESSAGE)
        self.assertEqual(job.result_reference["model_explanation"], (MODEL_TEXT * 20)[:500])
        html = self.project_page(project)
        self.assertIn("We have no source for this yet.", html)
        self.assertNotIn("example.test", html)

    def test_a_message_next_to_a_series_is_dropped(self):
        def answers(user_payload, number):
            return grounded(self.parts_from(user_payload["sources"]), message=MODEL_TEXT) if number == 1 else PASS_REVIEW

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        self.assertNotIn("model_explanation", job.result_reference)
        self.assertFalse(Message.objects.filter(project=project, body__contains="example.test").exists())
        self.assertNotIn("example.test", self.project_page(project))


FAIL_REVIEW = {
    **PASS_REVIEW,
    "verdict": "fail",
    "factual_state": "unsupported",
    "issues": [{"code": "unsupported", "severity": "blocking", "part_key": "1", "reason_ru": "Часть не подтверждается отрывком."}],
}


@override_settings(**LIVE, AI_MAX_OUTPUT_TOKENS=4321)
class OutputCapTests(LiveBase):
    """The budget check counts on AI_MAX_OUTPUT_TOKENS output tokens, so
    every call of an answer with sources asks the provider for that cap."""

    def test_every_call_of_the_path_passes_the_cap(self):
        def answers(user_payload, number):
            parts = self.parts_from(user_payload["sources"])
            if number == 1:  # rejected by the source check -> a correction call
                parts[0]["sources"][0]["quote"] = "these words were never written in the book at all"
                return grounded(parts)
            if number in (2, 4):  # the corrected answer; the repaired answer
                return grounded(parts)
            return FAIL_REVIEW if number == 3 else PASS_REVIEW

        project, job, calls = self.run_live(POLITICAL_ANIMAL, answers)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED, job.result_reference)
        operations = [(call[2]["operation"], "correction" in call[2]) for call in calls]
        self.assertEqual(operations, [
            ("answer_from_sources", False),
            ("answer_from_sources", True),
            ("review_answer_from_sources", False),
            ("repair", False),
            ("review_answer_from_sources", False),
        ][: len(calls)])
        self.assertGreaterEqual(len(calls), 4)  # generation, correction, review, repair
        self.assertEqual(self.call_options, [{"max_tokens": 4321}] * len(calls))

    def test_the_cap_goes_into_the_request_body(self):
        """The contract with the HTTP layer: `max_tokens` is in the body
        of the request, and absent when the caller gave none."""
        from apps.ai import openrouter

        sent = []

        class Reply(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def urlopen(request, timeout):
            sent.append(json.loads(request.data.decode("utf-8")))
            return Reply(json.dumps({"id": "gen-1", "usage": {}, "choices": [{"message": {"content": "{}"}}]}).encode("utf-8"))

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            openrouter._chat_completion("m", "system", {"a": 1}, {"name": "x", "schema": {}}, max_tokens=4321)
            openrouter._chat_completion("m", "system", {"a": 1}, {"name": "x", "schema": {}})
        self.assertEqual(sent[0]["max_tokens"], 4321)
        self.assertNotIn("max_tokens", sent[1])
        self.assertEqual(PRICES["anthropic/claude-sonnet-5.5"][0], Decimal("0.000002"))  # no price lookup was needed
