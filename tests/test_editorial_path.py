"""Stage B: a ready-made text travels to publication without being changed.

submit_text -> live review (one reviewer call, remarks only) -> the
existing gate publish_revision. No test here creates a Publication or a
QualityReport for real content by hand; the report comes from the worker.
"""

import copy
import json
import tempfile
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.ai.models import AIJob, ProviderAttempt, QualityReport
from apps.ai.services import ReviewNotAllowed, claim_job, create_review_job, execute_job
from apps.accounts.forms import RegisterForm
from apps.credits.models import BudgetBucket, CreditTransaction
from apps.library.editorial import EditorialError, canonical_hash, check_editorial_payload, load_card, profile_vocabulary, submit_text, vocabulary_errors
from apps.library.models import KnowledgeCard, Publication
from apps.library.services import PublicationBlocked, publication_slug, publish_revision
from apps.operations.models import AuditEvent
from apps.workshop.models import Project, Revision, RevisionKnowledgeCard

CONTENT = Path(settings.BASE_DIR) / "content"
TEXT_FILE = CONTENT / "texts" / "what-does-it-mean-to-be-brave.json"
CARD_FILE = CONTENT / "cards" / "plato-laches-courage-v1.json"
# sha256 of the canonical payload. The first text is loaded unchanged: any
# edit to the file, however small, must be deliberate and change this value.
FIRST_TEXT_HASH = "cee91034107e66c1ad5f37d9861641eea2138d1e2946c0a655ce3914bc74b815"

PASS_REVIEW = {
    "verdict": "pass",
    "factual_state": "supported",
    "issues": [{"code": "length", "severity": "nonblocking", "part_key": "4", "reason_ru": "Четвёртая часть короче ориентира."}],
    "unsupported_claims": [],
    "language_notes": [],
    "translation_issues": [],
    "uncertainty_notes": [],
}
BLOCKING_REVIEW = {
    "verdict": "repair",
    "factual_state": "mixed",
    "issues": [{"code": "pitfall", "severity": "blocking", "part_key": "2", "sentence_key": "8", "reason_ru": "Утверждение шире источника.", "proposed_fix_ru": "Смягчить формулировку."}],
    "unsupported_claims": [],
    "language_notes": [],
    "translation_issues": [],
    "uncertainty_notes": [],
}


def text_data():
    return json.loads(TEXT_FILE.read_text(encoding="utf-8"))


def card_data():
    return json.loads(CARD_FILE.read_text(encoding="utf-8"))


class StubReviewer:
    """Stands in for the live reviewer. It has no generate/repair: a call
    to anything but review_revision would fail the test."""

    editorial_review_prompt_version = "review-v3"

    def __init__(self, review):
        self.review = review
        self.calls = []

    def review_revision(self, payload, cards, job=None):
        self.calls.append({"payload": copy.deepcopy(payload), "cards": copy.deepcopy(cards)})
        self._attempts = [{
            "request_hash": "0" * 64,
            "provider_request_id": "gen-test",
            "usage_json": {"prompt_tokens": 5000, "completion_tokens": 900},
            "model": "anthropic/claude-sonnet-5.5",
            "cost_usd": Decimal("0.019000"),
        }]
        return copy.deepcopy(self.review)


def run_review(revision, review=PASS_REVIEW):
    """Queue and execute one review with a stubbed live provider."""
    stub = StubReviewer(review)
    job, _ = create_review_job(revision=revision, actor="test")
    with patch("apps.ai.services.get_provider", return_value=stub):
        execute_job(claim_job(job))
    job.refresh_from_db()
    return job, stub


class EditorialBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(login="owner", password="TestPassword123!", is_staff=True, is_superuser=True)

    def submit(self, data=None):
        load_card(card_data(), actor="test")
        return submit_text(data or text_data(), actor="test")


class FirstTextFileTests(TestCase):
    def test_file_is_the_owner_text_unchanged(self):
        payload = text_data()["payload"]
        self.assertEqual(canonical_hash(payload), FIRST_TEXT_HASH)
        self.assertEqual(payload["title_en"], "What does it mean to be brave?")
        self.assertEqual([len(part["sentences"]) for part in payload["parts"]], [10, 11, 12, 5])
        self.assertEqual(payload["parts"][0]["sentences"][0], {"en": "Two men talk.", "ru": "Двое мужчин беседуют."})
        self.assertEqual(payload["parts"][3]["sentences"][-1], {"en": "When is it brave to turn back?", "ru": "А когда — повернуть назад?"})
        fork = payload["parts"][0]["fork"]
        self.assertEqual([option["label_en"] for option in fork["options"]], ["Yes", "No", "Not sure"])
        self.assertEqual([term["term_en"] for term in payload["glossary"]], ["brave", "soldier", "fight"])

    def test_file_passes_structural_check_and_length_is_only_a_warning(self):
        errors, diagnostics = check_editorial_payload(text_data()["payload"])
        self.assertEqual(errors, [])
        self.assertEqual(diagnostics["fork_count"], 3)
        # Part 4 is below the editorial word range: reported, never blocking.
        self.assertTrue(any("Часть 4" in warning for warning in diagnostics["warnings_ru"]))

    def test_structural_check_rejects_broken_texts(self):
        payload = text_data()["payload"]
        broken = copy.deepcopy(payload)
        broken["parts"] = broken["parts"][:2]
        self.assertTrue(check_editorial_payload(broken)[0])
        broken = copy.deepcopy(payload)
        broken["parts"][1]["sentences"][0]["ru"] = " "
        self.assertTrue(check_editorial_payload(broken)[0])
        broken = copy.deepcopy(payload)
        broken["parts"][0]["fork"]["options"].pop()
        self.assertTrue(check_editorial_payload(broken)[0])
        broken = copy.deepcopy(payload)
        broken["parts"][3]["fork"] = broken["parts"][0]["fork"]  # after the last part
        self.assertTrue(check_editorial_payload(broken)[0])
        broken = copy.deepcopy(payload)
        broken["notes"] = "typo in a key must not pass silently"
        self.assertTrue(check_editorial_payload(broken)[0])


class ForkRulesTests(TestCase):
    """The fork after every part but the last: exact labels and "?" (owner decision, Nov 2026)."""

    def test_a_button_label_other_than_the_exact_word_is_refused(self):
        payload = text_data()["payload"]
        broken = copy.deepcopy(payload)
        broken["parts"][0]["fork"]["options"][0]["label_en"] = "Yes, of course"
        errors = check_editorial_payload(broken)[0]
        self.assertTrue(any("подпись кнопки" in error for error in errors))

    def test_b_question_without_a_question_mark_is_refused(self):
        payload = text_data()["payload"]
        broken = copy.deepcopy(payload)
        broken["parts"][1]["fork"]["prompt"]["en"] = "Are people brave only in war"
        errors = check_editorial_payload(broken)[0]
        self.assertTrue(any("знаком вопроса" in error for error in errors))

    def test_c_missing_fork_after_a_non_last_part_is_refused(self):
        payload = text_data()["payload"]
        broken = copy.deepcopy(payload)
        del broken["parts"][2]["fork"]
        errors = check_editorial_payload(broken)[0]
        self.assertTrue(any("Часть 3: после непоследней части нужна развилка" == error for error in errors))


class VocabularyGatesTests(EditorialBase):
    """submit_text gates: A1 share, words above A2 in the glossary, sentence length."""

    def test_the_file_passes_all_gates(self):
        payload = text_data()["payload"]
        self.assertEqual(vocabulary_errors(payload), [])

    def test_low_a1_share_is_refused(self):
        payload = text_data()["payload"]
        # Fill part 4 with B1 words ("abandon"): the share drops under 90 %.
        part = payload["parts"][3]
        part["sentences"] = [
            {"en": "They abandon every answer and abandon the easy way.", "ru": "Они оставляют всякий ответ и лёгкий путь."},
            {"en": "They abandon the fear and abandon the doubt.", "ru": "Они оставляют страх и сомнение."},
            {"en": "They abandon the old words and abandon the new.", "ru": "Они оставляют старые слова и новые."},
            {"en": "They abandon the road and abandon the field.", "ru": "Они оставляют дорогу и поле."},
            {"en": "They abandon the house and abandon the town.", "ru": "Они оставляют дом и город."},
        ]
        errors = vocabulary_errors(payload)
        self.assertTrue(any("слов уровня A1" in error for error in errors))

    def test_word_above_a2_without_glossary_is_refused(self):
        payload = text_data()["payload"]
        # "abandon" is B1 in the CEFR-J list and absent from the glossary.
        payload["parts"][3]["sentences"][0] = {"en": "They abandon the first answer.", "ru": "Они оставляют первый ответ."}
        errors = vocabulary_errors(payload)
        self.assertTrue(any("выше A2 без словарика — abandon" in error for error in errors))

    def test_glossed_word_above_a2_passes(self):
        payload = text_data()["payload"]
        # "soldier" is A2... "abandon" is B1: gloss it, then the gate passes.
        payload["glossary"].append({"term_en": "abandon", "explanation_en": "To leave without help.", "translation_ru": "оставлять"})
        payload["parts"][3]["sentences"][0] = {"en": "They abandon the first answer.", "ru": "Они оставляют первый ответ."}
        self.assertEqual(vocabulary_errors(payload), [])

    def test_multiword_glossary_covers_its_words(self):
        payload = text_data()["payload"]
        # "political" (A2) rides inside the glossed phrase "political animal":
        # the reader meets it inside the explained term.
        payload["glossary"].append({"term_en": "political animal", "explanation_en": "An animal that lives with others in a city.", "translation_ru": "политическое животное"})
        payload["parts"][3]["sentences"][0] = {"en": "Man is a political animal.", "ru": "Человек — политическое животное."}
        self.assertEqual(vocabulary_errors(payload), [])
        profile = profile_vocabulary(payload)
        self.assertTrue(profile["above_a1"]["political"]["in_glossary"])

    def test_form_takes_the_lowest_level_of_its_base_words(self):
        payload = text_data()["payload"]
        # "means" lists as a noun B2 and as the verb mean A1: the A1 verb
        # must not be charged with the noun's level.
        payload["parts"][3]["sentences"][0] = {"en": "This means one thing.", "ru": "Это значит одно."}
        self.assertEqual(vocabulary_errors(payload), [])
        profile = profile_vocabulary(payload)
        self.assertNotIn("means", profile["above_a1"])

    def test_capitalised_title_is_not_vocabulary(self):
        payload = text_data()["payload"]
        # "Politics" as a book title is a proper noun, not a B1 word.
        payload["parts"][3]["sentences"][0] = {"en": "The book is called Politics.", "ru": "Книга называется «Политика»."}
        profile = profile_vocabulary(payload)
        self.assertNotIn("politics", profile["above_a1"])
        # But a lowercase form keeps its list level.
        payload["parts"][3]["sentences"][0] = {"en": "He wrote about politics.", "ru": "Он писал о политике."}
        profile = profile_vocabulary(payload)
        self.assertIn("politics", profile["above_a1"])
        self.assertEqual(profile["above_a1"]["politics"]["level"], "B1")

    def test_long_sentence_is_refused(self):
        payload = text_data()["payload"]
        long_sentence = " ".join(["He said one word."] * 5)  # 20 words total
        payload["parts"][3]["sentences"][0] = {"en": long_sentence, "ru": "Он сказал одно слово. Он сказал одно слово. Он сказал одно слово. Он сказал одно слово. Он сказал одно слово."}
        errors = vocabulary_errors(payload)
        self.assertTrue(any("предложение длиннее 16 слов" in error for error in errors))

    def test_gates_run_at_submission(self):
        data = text_data()
        data["payload"]["parts"][3]["sentences"][0] = {"en": "They abandon the first answer.", "ru": "Они оставляют первый ответ."}
        with self.assertRaises(EditorialError) as ctx:
            self.submit(data)
        self.assertTrue(any("abandon" in message for message in ctx.exception.messages))


class SubmitTests(EditorialBase):
    def test_submission_stores_the_payload_exactly(self):
        revision, created = self.submit()
        self.assertTrue(created)
        self.assertEqual(revision.payload, text_data()["payload"])
        self.assertEqual(revision.content_hash, FIRST_TEXT_HASH)
        self.assertEqual(revision.project.head_revision_id, revision.pk)
        self.assertFalse(revision.project.owner.is_active)
        self.assertIsNone(revision.created_by_job)
        self.assertEqual(AIJob.objects.count(), 0)  # no model call to submit
        card = KnowledgeCard.objects.get(slug="plato-laches-courage", version=1)
        self.assertTrue(RevisionKnowledgeCard.objects.filter(revision=revision, knowledge_card=card).exists())
        self.assertTrue(AuditEvent.objects.filter(action="editorial_submit", object_reference=f"revision:{revision.pk}").exists())

    def test_resubmission_is_idempotent(self):
        first, _ = self.submit()
        second, created = self.submit()
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Revision.objects.filter(project=first.project).count(), 1)
        self.assertEqual(RevisionKnowledgeCard.objects.filter(revision=first).count(), 1)

    def test_any_edit_becomes_a_new_revision_without_a_report(self):
        first, _ = self.submit()
        run_review(first)
        edited = text_data()
        edited["payload"]["parts"][0]["sentences"][3]["en"] = "He knows war very well."
        second, created = self.submit(edited)
        self.assertTrue(created)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.parent_id, first.pk)
        self.assertEqual(first.payload, text_data()["payload"])  # the old revision is untouched
        self.assertFalse(QualityReport.objects.filter(revision=second).exists())
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=second, admin=self.admin, slug="what-does-it-mean-to-be-brave")
        self.assertIn("Нет QualityReport", str(blocked.exception))

    def test_resubmission_with_a_new_card_version_replaces_the_link_and_stales_the_report(self):
        revision, _ = self.submit()
        run_review(revision)
        newer = card_data()
        newer["version"] = 2
        newer["pitfalls"].append("Do not date the conversation.")
        load_card(newer, actor="test")
        data = text_data()
        data["knowledge_cards"] = [{"slug": "plato-laches-courage", "version": 2}]
        same, created = submit_text(data, actor="test")
        self.assertFalse(created)
        self.assertEqual(same.pk, revision.pk)
        linked = [(link.knowledge_card.slug, link.knowledge_card.version) for link in RevisionKnowledgeCard.objects.filter(revision=revision)]
        self.assertEqual(linked, [("plato-laches-courage", 2)])
        self.assertTrue(AuditEvent.objects.filter(action="editorial_cards_changed").exists())
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave")
        self.assertIn("нужна новая проверка", str(blocked.exception))
        data["knowledge_cards"].append({"slug": "plato-laches-courage", "version": 1})
        with self.assertRaises(EditorialError):
            submit_text(data, actor="test")  # two versions of one card

    def test_editorial_account_must_stay_inactive(self):
        owner = User.objects.create_user(login="editorial", password="TestPassword123!")
        self.assertTrue(owner.is_active)
        load_card(card_data(), actor="test")
        with self.assertRaises(EditorialError):
            submit_text(text_data(), actor="test")
        self.assertEqual(Revision.objects.count(), 0)

    def test_submission_needs_a_known_card_and_a_valid_slug(self):
        with self.assertRaises(EditorialError):
            submit_text(text_data(), actor="test")  # card not loaded
        load_card(card_data(), actor="test")
        data = text_data()
        for bad in ("what does it mean?", None, 7, "x" * 51):
            data["slug"] = bad
            with self.subTest(slug=bad), self.assertRaises(EditorialError):
                submit_text(data, actor="test")
        self.assertEqual(Revision.objects.count(), 0)


class CardTests(TestCase):
    def test_card_is_loaded_with_its_verification_record(self):
        card, created = load_card(card_data(), actor="test")
        self.assertTrue(created)
        self.assertEqual(card.verification_status, KnowledgeCard.Status.VERIFIED)
        self.assertIsNone(card.verified_by)  # checked by AI, not by a person
        event = AuditEvent.objects.get(action="knowledge_card_loaded")
        self.assertEqual(event.sanitized_metadata["method"], "ai")
        self.assertIn("Человек карточку не вычитывал", event.reason)
        again, created = load_card(card_data(), actor="test")
        self.assertFalse(created)
        self.assertEqual(again.pk, card.pk)

    def test_card_version_is_immutable(self):
        load_card(card_data(), actor="test")
        changed = card_data()
        changed["facts"][0]["text"] = "Socrates asks Nicias what courage is."
        with self.assertRaises(EditorialError):
            load_card(changed, actor="test")
        changed["version"] = 2
        card, created = load_card(changed, actor="test")
        self.assertTrue(created)
        self.assertEqual(KnowledgeCard.objects.filter(slug="plato-laches-courage").count(), 2)

    def test_verified_card_needs_sources_and_method(self):
        data = card_data()
        data["sources"] = []
        with self.assertRaises(EditorialError):
            load_card(data, actor="test")

    def test_card_file_is_validated(self):
        for change in (
            {"slug": "x" * 51},
            {"slug": "not a slug"},
            {"version": True},
            {"version": 0},
            {"verification": "verified"},
            {"facts": [{"fact_id": "a", "locator": "190e", "text": "T", "type": "source"}]},
        ):
            with self.subTest(change=change), self.assertRaises(EditorialError):
                load_card({**card_data(), **change}, actor="test")
        self.assertFalse(KnowledgeCard.objects.exists())


class ReviewTests(EditorialBase):
    def test_reviewer_gets_text_and_verified_card_and_changes_nothing(self):
        revision, _ = self.submit()
        job, stub = run_review(revision, BLOCKING_REVIEW)
        self.assertEqual(job.status, AIJob.Status.SUCCEEDED)
        self.assertEqual(len(stub.calls), 1)  # exactly one reviewer call
        call = stub.calls[0]
        self.assertEqual(call["payload"], text_data()["payload"])
        self.assertEqual(call["cards"][0]["slug"], "plato-laches-courage")
        self.assertEqual(len(call["cards"][0]["facts"]), 10)
        self.assertTrue(call["cards"][0]["pitfalls"])
        self.assertTrue(call["cards"][0]["sources"][0]["url"].startswith("https://www.perseus.tufts.edu/"))
        # Remarks are stored in Russian; the text is the same revision, unchanged.
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.review_json["issues"][0]["reason_ru"], "Утверждение шире источника.")
        self.assertEqual(report.review_json["issues"][0]["proposed_fix_ru"], "Смягчить формулировку.")
        revision.refresh_from_db()
        self.assertEqual(revision.payload, text_data()["payload"])
        self.assertEqual(Revision.objects.count(), 1)  # no repaired copy

    def test_report_is_bound_to_this_revision_hash_and_cards(self):
        revision, _ = self.submit()
        job, _ = run_review(revision)
        report = QualityReport.objects.get(pk=job.result_reference["quality_report_id"])
        self.assertEqual(report.revision_id, revision.pk)
        self.assertEqual(report.revision_content_hash, FIRST_TEXT_HASH)
        self.assertEqual(report.review_model, "anthropic/claude-sonnet-5.5")
        self.assertEqual(report.prompt_version, "review-v3")
        self.assertEqual(report.factual_state, "supported")
        self.assertEqual(report.eligibility_state, "eligible")
        self.assertEqual([(c["slug"], c["version"]) for c in report.card_versions_json], [("plato-laches-courage", 1)])
        self.assertTrue(report.deterministic_json["warnings_ru"])
        attempt = ProviderAttempt.objects.get(job=job)
        self.assertEqual(attempt.cost_usd, Decimal("0.019000"))

    def test_review_takes_no_user_credits(self):
        revision, _ = self.submit()
        job, _ = run_review(revision)
        self.assertEqual(job.reserved_credits, 0)
        self.assertEqual(CreditTransaction.objects.count(), 0)

    def test_review_is_refused_without_a_verified_card(self):
        revision, _ = self.submit()
        KnowledgeCard.objects.update(verification_status=KnowledgeCard.Status.DRAFT)
        with self.assertRaises(ReviewNotAllowed):
            create_review_job(revision=revision, actor="test")
        self.assertEqual(AIJob.objects.count(), 0)

    def test_queued_review_is_reused(self):
        revision, _ = self.submit()
        first, created = create_review_job(revision=revision, actor="test")
        second, created_again = create_review_job(revision=revision, actor="test")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)

    def test_fake_provider_never_makes_a_publishable_report(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")
        execute_job(claim_job(job))  # AI_PROVIDER=fake in tests
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.factual_state, "unverified")
        self.assertEqual(report.review_model, "")
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave")

    def test_malformed_reviewer_answer_is_never_publishable(self):
        revision, _ = self.submit()
        run_review(revision, {"verdict": "publish it", "factual_state": "certainly", "issues": "none"})
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.review_json["verdict"], "fail")
        self.assertEqual(report.factual_state, "unverified")
        self.assertEqual(report.eligibility_state, "ineligible")

    def test_review_fields_of_the_wrong_type_are_not_dropped_silently(self):
        revision, _ = self.submit()
        for broken in (
            {**PASS_REVIEW, "issues": "BLOCKING: the text misquotes Plato"},
            {**PASS_REVIEW, "issues": ["the text misquotes Plato"]},
            {**PASS_REVIEW, "unsupported_claims": "everything"},
            {**PASS_REVIEW, "translation_issues": [{"ru": "x"}]},
            "pass",
        ):
            with self.subTest(review=broken):
                QualityReport.objects.all().delete()
                run_review(revision, broken)
                report = QualityReport.objects.get(revision=revision)
                self.assertEqual(report.review_json["verdict"], "fail")
                self.assertEqual(report.review_json["issues"][0]["code"], "malformed_review")
                self.assertEqual(report.eligibility_state, "ineligible")
                with self.assertRaises(PublicationBlocked):
                    publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave")

    def test_unknown_severity_counts_as_blocking(self):
        revision, _ = self.submit()
        run_review(revision, {**PASS_REVIEW, "issues": [{"code": "x", "severity": "critical", "reason_ru": "Ошибка."}]})
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.review_json["issues"][0]["severity"], "blocking")
        self.assertEqual(report.eligibility_state, "ineligible")

    def test_blocking_remark_beyond_the_stored_cap_is_kept(self):
        revision, _ = self.submit()
        issues = [{"code": f"n{i}", "severity": "nonblocking", "reason_ru": "Стиль."} for i in range(25)]
        issues.append({"code": "last", "severity": "blocking", "reason_ru": "Ошибка в конце списка."})
        run_review(revision, {**PASS_REVIEW, "issues": issues})
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(len(report.review_json["issues"]), 20)
        self.assertEqual(report.review_json["issues"][0]["code"], "last")
        self.assertEqual(report.eligibility_state, "ineligible")

    def test_unsupported_claims_make_the_report_ineligible(self):
        revision, _ = self.submit()
        run_review(revision, {**PASS_REVIEW, "unsupported_claims": [{"claim_text": "Laches is a soldier.", "reason": "Нет в карточке."}]})
        self.assertEqual(QualityReport.objects.get(revision=revision).eligibility_state, "ineligible")
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave")
        self.assertIn("eligibility_state=ineligible", str(blocked.exception))

    def test_provider_failure_fails_the_job_without_a_report(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")

        class Broken:
            def review_revision(self, *args, **kwargs):
                raise RuntimeError("network down")

        with patch("apps.ai.services.get_provider", return_value=Broken()):
            execute_job(claim_job(job))
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.FAILED)
        self.assertFalse(QualityReport.objects.exists())


class PublicationTests(EditorialBase):
    slug = "what-does-it-mean-to-be-brave"

    def test_reviewed_text_is_published_and_opens_at_its_address(self):
        revision, _ = self.submit()
        run_review(revision)
        publication, created = publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertTrue(created)
        self.assertEqual(publication.revision.payload, text_data()["payload"])
        response = self.client.get(reverse("library:read", args=[self.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Two men talk.")
        self.assertContains(response, "Двое мужчин беседуют.")
        self.assertEqual(self.client.get(reverse("library:home")).status_code, 200)
        self.assertEqual(self.client.get(reverse("library:library")).status_code, 200)

    def test_gate_rejects_missing_report_blocking_issue_and_unverified_card(self):
        revision, _ = self.submit()
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertIn("Нет QualityReport", str(blocked.exception))

        run_review(revision, BLOCKING_REVIEW)
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertIn("блокирующие замечания", str(blocked.exception))
        self.assertIn("verdict=repair", str(blocked.exception))

        run_review(revision)  # a later clean report for the same hash
        KnowledgeCard.objects.update(verification_status=KnowledgeCard.Status.DRAFT)
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertIn("Нет проверенной карточки", str(blocked.exception))
        self.assertFalse(Publication.objects.exists())

    def test_gate_needs_each_condition_on_its_own(self):
        """One defect at a time in an otherwise clean report."""
        revision, _ = self.submit()
        run_review(revision)
        report = QualityReport.objects.get(revision=revision)
        clean = {"review_json": report.review_json, "factual_state": "supported", "eligibility_state": "eligible", "card_versions_json": report.card_versions_json}
        blocking_issue = {"code": "x", "severity": "blocking", "reason_ru": "Ошибка."}
        defects = {
            "verdict=repair": {"review_json": {**report.review_json, "verdict": "repair"}},
            "verdict=None": {"review_json": {"issues": []}},
            "блокирующие замечания": {"review_json": {**report.review_json, "issues": [blocking_issue]}},
            "factual_state=mixed": {"factual_state": "mixed"},
            "eligibility_state=ineligible": {"eligibility_state": "ineligible"},
            "без нынешних проверенных карточек": {"card_versions_json": []},
        }
        for expected, defect in defects.items():
            with self.subTest(defect=expected):
                QualityReport.objects.filter(pk=report.pk).update(**{**clean, **defect})
                with self.assertRaises(PublicationBlocked) as blocked:
                    publish_revision(revision=revision, admin=self.admin, slug=self.slug)
                self.assertEqual(str(blocked.exception).count(";"), 0, str(blocked.exception))  # the only reason
                self.assertIn(expected, str(blocked.exception))
        self.assertFalse(Publication.objects.exists())
        QualityReport.objects.filter(pk=report.pk).update(**clean)
        self.assertTrue(publish_revision(revision=revision, admin=self.admin, slug=self.slug)[1])

    def test_report_from_the_generation_cycle_does_not_publish(self):
        """The generate -> review cycle gives the reviewer no cards, so its
        report cannot open the gate even with a verified card linked later."""
        revision, _ = self.submit()
        QualityReport.objects.create(
            revision=revision, revision_content_hash=revision.content_hash,
            review_json={"verdict": "pass", "factual_state": "supported", "issues": []},
            factual_state="supported", eligibility_state="ineligible",
        )
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=revision, admin=self.admin, slug=self.slug)

    def test_one_text_has_one_address(self):
        first, _ = self.submit()
        run_review(first)
        publish_revision(revision=first, admin=self.admin, slug=self.slug)
        edited = text_data()
        edited["payload"]["parts"][0]["sentences"][3]["ru"] = "Он очень хорошо знает войну."
        second, _ = self.submit(edited)
        run_review(second)
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=second, admin=self.admin, slug="a-second-address")
        self.assertIn("адрес не меняется", str(blocked.exception))
        self.assertEqual(Publication.objects.count(), 1)
        self.assertEqual(Publication.objects.get().revision_id, first.pk)

    def test_address_must_be_a_string_slug(self):
        revision, _ = self.submit()
        run_review(revision)
        for bad in (None, 5, "", "x" * 51, "what does it mean"):
            with self.subTest(slug=bad), self.assertRaises(PublicationBlocked):
                publish_revision(revision=revision, admin=self.admin, slug=bad)

    def test_gate_rejects_a_report_made_with_a_different_card(self):
        revision, _ = self.submit()
        run_review(revision)
        KnowledgeCard.objects.filter(slug="plato-laches-courage").update(pitfalls_json=["changed after the review"])
        with self.assertRaises(PublicationBlocked) as blocked:
            publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertIn("нужна новая проверка", str(blocked.exception))

    def test_edited_text_replaces_the_publication_only_after_a_new_review(self):
        first, _ = self.submit()
        run_review(first)
        publication, _ = publish_revision(revision=first, admin=self.admin, slug=self.slug)
        edited = text_data()
        edited["payload"]["parts"][3]["sentences"][0]["ru"] = "В конце Сократ говорит: «Ответа у нас нет»."
        second, _ = self.submit(edited)
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=second, admin=self.admin, slug=self.slug)
        publication.refresh_from_db()
        self.assertEqual(publication.revision_id, first.pk)  # readers still see the reviewed text

        job, _ = run_review(second)
        republished, changed = publish_revision(revision=second, admin=self.admin, slug=self.slug)
        self.assertTrue(changed)
        self.assertEqual(republished.pk, publication.pk)
        self.assertEqual(republished.revision_id, second.pk)
        self.assertEqual(republished.quality_report_id, job.result_reference["quality_report_id"])  # the new report
        self.assertEqual(republished.quality_report.revision_id, second.pk)
        self.assertEqual(Publication.objects.count(), 1)
        self.assertTrue(AuditEvent.objects.filter(action="editorial_republish").exists())
        self.assertContains(self.client.get(reverse("library:read", args=[self.slug])), "В конце Сократ говорит")

    def test_question_mark_in_title_gives_a_working_address(self):
        revision, _ = self.submit()
        self.assertEqual(publication_slug(revision), self.slug)
        run_review(revision)
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave?")
        self.assertFalse(Publication.objects.exists())
        self.assertEqual(self.client.get(reverse("library:home")).status_code, 200)

    def test_address_of_another_text_is_not_taken_over(self):
        revision, _ = self.submit()
        run_review(revision)
        publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        other = text_data()
        other["slug"] = "another-text"
        other["payload"]["title_ru"] = "Другой текст"
        second, _ = self.submit(other)
        run_review(second)
        self.assertEqual(publication_slug(second), "what-does-it-mean-to-be-brave-2")
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=second, admin=self.admin, slug=self.slug)

    def test_withdrawn_text_is_restored_through_the_gate(self):
        revision, _ = self.submit()
        run_review(revision)
        publication, _ = publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        call_command("withdraw_publication", self.slug, "--admin", "owner", stdout=StringIO())
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.WITHDRAWN)
        self.assertEqual(self.client.get(reverse("library:read", args=[self.slug])).status_code, 404)
        restored, changed = publish_revision(revision=revision, admin=self.admin, slug=self.slug)
        self.assertTrue(changed)
        self.assertEqual(restored.status, Publication.Status.PUBLISHED)


class AdminTests(EditorialBase):
    def setUp(self):
        self.client.force_login(self.admin)

    def test_publication_cannot_be_created_by_hand(self):
        self.assertEqual(self.client.get("/admin/library/publication/add/").status_code, 403)

    def test_quality_report_is_read_only_in_admin(self):
        revision, _ = self.submit()
        run_review(revision, BLOCKING_REVIEW)
        report = QualityReport.objects.get()
        self.assertEqual(self.client.get("/admin/ai/qualityreport/add/").status_code, 403)
        url = f"/admin/ai/qualityreport/{report.pk}/change/"
        self.assertEqual(self.client.get(url).status_code, 200)  # viewable
        response = self.client.post(url, {"factual_state": "supported", "eligibility_state": "eligible", "review_json": '{"verdict": "pass"}'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post(f"/admin/ai/qualityreport/{report.pk}/delete/", {"post": "yes"}).status_code, 403)
        report.refresh_from_db()
        self.assertEqual((report.factual_state, report.eligibility_state), ("mixed", "ineligible"))

    def test_withdrawn_publication_cannot_be_reopened_from_the_change_form(self):
        revision, _ = self.submit()
        run_review(revision)
        publication, _ = publish_revision(revision=revision, admin=self.admin, slug="what-does-it-mean-to-be-brave")
        self.client.post("/admin/library/publication/", {"action": "withdraw_action", "_selected_action": [str(publication.pk)]})
        url = f"/admin/library/publication/{publication.pk}/change/"
        self.assertNotContains(self.client.get(url), 'name="status"')
        self.client.post(url, {"status": "published", "thinker": "", "_save": "Save"})
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.WITHDRAWN)
        self.assertEqual(self.client.get("/read/what-does-it-mean-to-be-brave/").status_code, 404)

    def test_card_content_is_read_only_in_admin(self):
        card, _ = load_card(card_data(), actor="test")
        page = self.client.get(f"/admin/library/knowledgecard/{card.pk}/change/")
        self.assertNotContains(page, 'name="facts_json"')
        self.assertNotContains(page, 'name="pitfalls_json"')
        self.assertContains(page, 'name="verification_status"')

    def test_review_and_publish_actions_are_offered_for_revisions(self):
        revision, _ = self.submit()
        url = "/admin/workshop/revision/"
        page = self.client.get(url)
        self.assertContains(page, "Проверить ревизию (живая проверка, платный вызов)")
        self.assertContains(page, "Редакционная публикация выбранной ревизии")
        self.client.post(url, {"action": "review_revision_action", "_selected_action": [revision.pk]})
        job = AIJob.objects.get(kind=AIJob.Kind.REVIEW, base_revision=revision)
        self.assertEqual(job.status, AIJob.Status.QUEUED)
        # Publishing before the review is refused by the same gate.
        self.client.post(url, {"action": "publish_revision_action", "_selected_action": [revision.pk]})
        self.assertFalse(Publication.objects.exists())

    def test_admin_actions_publish_and_withdraw(self):
        revision, _ = self.submit()
        run_review(revision)
        self.client.post("/admin/workshop/revision/", {"action": "publish_revision_action", "_selected_action": [revision.pk]})
        publication = Publication.objects.get()
        self.assertEqual(publication.slug, "what-does-it-mean-to-be-brave")
        self.client.post("/admin/library/publication/", {"action": "withdraw_action", "_selected_action": [str(publication.pk)]})
        publication.refresh_from_db()
        self.assertEqual(publication.status, Publication.Status.WITHDRAWN)
        self.assertTrue(AuditEvent.objects.filter(action="editorial_withdraw").exists())


class CommandTests(EditorialBase):
    def run_command(self, *args):
        out = StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    def test_commands_take_the_first_text_to_publication(self):
        out = self.run_command("submit_text", str(TEXT_FILE))
        self.assertIn("Карточка plato-laches-courage v1: загружена, статус verified", out)
        self.assertIn("создана", out)
        with self.assertRaises(CommandError) as refused:
            self.run_command("publish_text", str(TEXT_FILE))
        self.assertIn("Нет QualityReport", str(refused.exception))

        with patch("apps.ai.services.get_provider", return_value=StubReviewer(PASS_REVIEW)):
            out = self.run_command("review_text", str(TEXT_FILE), "--inline")
        self.assertIn("verdict=pass", out)
        self.assertIn("[замечание] 4: Четвёртая часть короче ориентира.", out)
        self.assertIn("Отчёт допускает публикацию", out)

        out = self.run_command("publish_text", str(TEXT_FILE))  # the only superuser publishes
        self.assertIn("Опубликовано: /read/what-does-it-mean-to-be-brave/", out)
        publication = Publication.objects.get(slug="what-does-it-mean-to-be-brave")
        self.assertEqual(publication.published_by, self.admin)
        self.assertEqual(publication.revision.content_hash, FIRST_TEXT_HASH)
        self.assertIn("Уже опубликовано", self.run_command("publish_text", str(TEXT_FILE)))

    def test_review_command_reports_blocking_remarks_and_keeps_the_text(self):
        self.run_command("submit_text", str(TEXT_FILE))
        with patch("apps.ai.services.get_provider", return_value=StubReviewer(BLOCKING_REVIEW)):
            out = self.run_command("review_text", str(TEXT_FILE), "--inline")
        self.assertIn("[БЛОКИРУЕТ] 2 8: Утверждение шире источника.", out)
        self.assertIn("Отчёт не допускает публикацию", out)
        self.assertEqual(Revision.objects.get().payload, text_data()["payload"])

    def test_commands_republish_an_edited_file_under_the_same_address(self):
        self.run_command("submit_text", str(TEXT_FILE))
        with patch("apps.ai.services.get_provider", return_value=StubReviewer(PASS_REVIEW)):
            self.run_command("review_text", str(TEXT_FILE), "--inline")
        self.run_command("publish_text", str(TEXT_FILE))
        edited = text_data()
        edited["payload"]["parts"][0]["sentences"][3]["ru"] = "Он очень хорошо знает войну."
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "edited.json"
            path.write_text(json.dumps(edited, ensure_ascii=False), encoding="utf-8")
            self.run_command("submit_text", str(path))
            with self.assertRaises(CommandError):
                self.run_command("publish_text", str(path))  # edited: needs a new review
            with patch("apps.ai.services.get_provider", return_value=StubReviewer(PASS_REVIEW)):
                self.run_command("review_text", str(path), "--inline")
            out = self.run_command("publish_text", str(path))
        self.assertIn("Опубликовано: /read/what-does-it-mean-to-be-brave/", out)
        publication = Publication.objects.get()
        self.assertEqual(publication.revision.payload["parts"][0]["sentences"][3]["ru"], "Он очень хорошо знает войну.")

    def test_editorial_login_cannot_be_registered(self):
        form = RegisterForm({"login": "editorial", "password": "TestPassword123!", "password_confirm": "TestPassword123!", "no_recovery_ack": "on"})
        self.assertFalse(form.is_valid())
        self.assertIn("login", form.errors)

    def test_seed_demo_no_longer_publishes(self):
        self.run_command("seed_demo")
        self.assertFalse(Publication.objects.exists())
        self.assertFalse(QualityReport.objects.exists())
        self.assertTrue(Project.objects.filter(title="Editorial: Socratic method").exists())


LIVE = dict(
    LIVE_AI_ENABLED=True,
    AI_PROVIDER="openrouter",
    OPENROUTER_API_KEY="test-key",
    OWNER_DAILY_BUDGET_USD="1",
    OWNER_MONTHLY_BUDGET_USD="10",
    AI_GENERATION_MODEL="anthropic/claude-sonnet-5.5",
    AI_REVIEW_MODEL="anthropic/claude-haiku-5.5",
)
PRICES = {
    "anthropic/claude-sonnet-5.5": (Decimal("0.000002"), Decimal("0.00001")),
    "anthropic/claude-haiku-5.5": (Decimal("0.0000001"), Decimal("0.0000005")),
}


def fake_completion(content):
    return {"id": "gen-1", "usage": {"prompt_tokens": 5000, "completion_tokens": 1000}, "choices": [{"message": {"content": json.dumps(content)}}]}, 0.5


@override_settings(**LIVE)
class LiveProviderContractTests(EditorialBase):
    """The real OpenRouterProvider with the HTTP layer patched: what is
    sent to the reviewer and what the call costs. No network."""

    def test_editorial_review_sends_card_and_is_priced_by_the_review_model(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")
        with patch("apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]), patch(
            "apps.ai.openrouter._chat_completion", return_value=fake_completion(PASS_REVIEW)
        ) as chat:
            execute_job(claim_job(job))
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.SUCCEEDED, job.result_reference)
        self.assertEqual(chat.call_count, 1)  # one paid call, no repair
        model, system, user_payload, schema = chat.call_args.args
        self.assertEqual(model, "anthropic/claude-haiku-5.5")
        self.assertIn("Write every free-text field in Russian", system)
        self.assertIn("you never write or rewrite it", system)
        self.assertEqual(user_payload["candidate"], text_data()["payload"])
        self.assertEqual(user_payload["knowledge_cards"][0]["slug"], "plato-laches-courage")
        self.assertEqual(schema["name"], "review_result")
        # 5000 * 0.0000001 + 1000 * 0.0000005 = 0.001 at review-model prices.
        self.assertEqual(ProviderAttempt.objects.get(job=job).cost_usd, Decimal("0.001000"))
        self.assertEqual(sum(b.consumed_usd for b in BudgetBucket.objects.all()), Decimal("0.002000"))  # day + month
        report = QualityReport.objects.get(revision=revision)
        self.assertEqual(report.review_model, "anthropic/claude-haiku-5.5")
        self.assertEqual(report.eligibility_state, "eligible")

    def test_generation_review_step_is_priced_by_the_review_model(self):
        """Regression: review() used the generation model's prices."""
        from apps.ai.openrouter import OpenRouterProvider

        provider = OpenRouterProvider()
        provider.price_in, provider.price_out = PRICES["anthropic/claude-sonnet-5.5"]  # left by generate()
        job = AIJob.objects.create(
            owner=self.admin, kind=AIJob.Kind.GENERATE, input_json={}, input_hash="x", idempotency_key="price-check", policy_version="credits-v1"
        )
        with patch("apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]), patch(
            "apps.ai.openrouter._chat_completion", return_value=fake_completion(PASS_REVIEW)
        ):
            provider.review({"parts": []}, job)
        self.assertEqual(BudgetBucket.objects.order_by("period").first().consumed_usd, Decimal("0.001000"))

    def test_exhausted_budget_stops_the_review_before_any_call(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")
        # Worst case of one call at these prices is 60000*0.0000001 + 8000*0.0000005 = 0.01.
        with override_settings(OWNER_DAILY_BUDGET_USD="0.005"), patch(
            "apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]
        ), patch("apps.ai.openrouter._chat_completion") as chat:
            execute_job(claim_job(job))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (AIJob.Status.FAILED, "budget_exhausted"))
        chat.assert_not_called()
        self.assertFalse(QualityReport.objects.exists())

    def test_paid_call_with_unreadable_answer_is_still_recorded(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")
        bad = ({"id": "gen-2", "usage": {"prompt_tokens": 5000, "completion_tokens": 1000}, "choices": [{"message": {"content": "not json"}}]}, 0.5)
        with patch("apps.ai.openrouter._pricing_for", side_effect=lambda model: PRICES[model]), patch(
            "apps.ai.openrouter._chat_completion", return_value=bad
        ):
            execute_job(claim_job(job))
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_code), (AIJob.Status.FAILED, "provider_error"))
        attempt = ProviderAttempt.objects.get(job=job)
        self.assertEqual((attempt.state, attempt.cost_usd), ("failed", Decimal("0.001000")))
        self.assertFalse(QualityReport.objects.exists())

    def test_review_without_budget_fails_before_any_call(self):
        revision, _ = self.submit()
        job, _ = create_review_job(revision=revision, actor="test")
        with override_settings(OWNER_DAILY_BUDGET_USD=""), patch("apps.ai.openrouter._chat_completion") as chat:
            execute_job(claim_job(job))
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.FAILED)
        self.assertEqual(job.error_code, "ai_not_configured")
        chat.assert_not_called()
        self.assertFalse(QualityReport.objects.exists())
