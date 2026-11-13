"""Stage 7 workshop: fragment edits, snapshot integrity, head apply,
review/repair outcomes."""

from uuid import uuid4

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.ai.models import AIJob, QualityReport
from apps.ai.services import (
    EditScopeError,
    apply_edit,
    claim_next_job,
    create_job,
    execute_job,
)
from apps.credits.models import Wallet, grant_welcome_credits
from apps.workshop.models import Project, Revision

BASE_PAYLOAD = {
    "title_en": "A short series",
    "title_ru": "Короткая серия",
    "parts": [
        {
            "title_en": "Part one",
            "sentences": [
                {"en": "First idea.", "ru": "Первая мысль."},
                {"en": "Second idea.", "ru": "Вторая мысль."},
            ],
        },
        {
            "title_en": "Part two",
            "sentences": [
                {"en": "Third idea.", "ru": "Третья мысль."},
            ],
        },
    ],
    "terms": [],
    "glossary": [],
}


def base_revision_payload():
    import json

    return json.loads(json.dumps(BASE_PAYLOAD))


def make_base_revision(project):
    return Revision.objects.create(project=project, payload=base_revision_payload())


class EditScopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="edituser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.base = make_base_revision(self.project)

    def _job(self, kind, target):
        job, _ = create_job(
            owner=self.user, kind=kind, idempotency_key=uuid4().hex,
            instruction="fix it", project=self.project, base_revision=self.base,
            target_id=target,
        )
        return job

    def _decision(self, kind, **fields):
        if kind == AIJob.Kind.EDIT_SENTENCE:
            decision = {"outcome": "edited", "replacement_sentences": [{"en": "Fixed sentence.", "ru": "Исправленное предложение."}], "message_ru": ""}
        else:
            decision = {"outcome": "edited", "part": {"title_en": "Part one", "sentences": [{"en": "Rewritten part.", "ru": "Переписанная часть."}]}, "message_ru": ""}
        decision.update(fields)
        return decision

    def test_sentence_edit_replaces_only_target(self):
        job = self._job(AIJob.Kind.EDIT_SENTENCE, "0:1")
        result = apply_edit(job, self._decision(AIJob.Kind.EDIT_SENTENCE))
        self.assertEqual(result["outcome"], AIJob.Outcome.DELIVERED)
        parts = result["payload"]["parts"]
        # unchanged first sentence copied verbatim
        self.assertEqual(parts[0]["sentences"][0], self.base.payload["parts"][0]["sentences"][0])
        # target replaced with one replacement
        self.assertEqual(parts[0]["sentences"][1]["en"], "Fixed sentence.")
        # later part untouched
        self.assertEqual(parts[1]["sentences"], self.base.payload["parts"][1]["sentences"])
        # titles and glossary survive
        self.assertEqual(result["payload"]["title_ru"], "Короткая серия")
        self.assertEqual(parts[0]["title_en"], "Part one")

    def test_sentence_edit_two_replacements_shift_following(self):
        job = self._job(AIJob.Kind.EDIT_SENTENCE, "0:0")
        decision = self._decision(
            AIJob.Kind.EDIT_SENTENCE,
            replacement_sentences=[
                {"en": "Alpha.", "ru": "Альфа."},
                {"en": "Beta.", "ru": "Бета."},
            ],
        )
        result = apply_edit(job, decision)
        sentences = result["payload"]["parts"][0]["sentences"]
        self.assertEqual([s["en"] for s in sentences], ["Alpha.", "Beta.", "Second idea."])

    def test_sentence_edit_rejects_three_replacements(self):
        job = self._job(AIJob.Kind.EDIT_SENTENCE, "0:0")
        decision = self._decision(
            AIJob.Kind.EDIT_SENTENCE,
            replacement_sentences=[{"en": "a", "ru": "a"}, {"en": "b", "ru": "b"}, {"en": "c", "ru": "c"}],
        )
        with self.assertRaises(EditScopeError):
            apply_edit(job, decision)

    def test_part_edit_keeps_title_and_other_parts(self):
        job = self._job(AIJob.Kind.EDIT_PART, "0")
        result = apply_edit(job, self._decision(AIJob.Kind.EDIT_PART))
        parts = result["payload"]["parts"]
        self.assertEqual(parts[0]["title_en"], "Part one")
        self.assertEqual(len(parts[0]["sentences"]), 1)
        self.assertEqual(parts[1]["sentences"], self.base.payload["parts"][1]["sentences"])

    def test_part_edit_cannot_touch_other_parts_via_payload(self):
        job = self._job(AIJob.Kind.EDIT_PART, "0")
        decision = self._decision(AIJob.Kind.EDIT_PART)
        decision["part"]["sentences"].append({"en": "Injected into another part?", "ru": "Инъекция?"})
        result = apply_edit(job, decision)
        # Only part 0 changes; part 1 stays as in the base revision.
        self.assertEqual(result["payload"]["parts"][1]["sentences"], self.base.payload["parts"][1]["sentences"])

    def test_targets_outside_revision_rejected(self):
        job = self._job(AIJob.Kind.EDIT_SENTENCE, "5:0")
        with self.assertRaises(EditScopeError):
            apply_edit(job, self._decision(AIJob.Kind.EDIT_SENTENCE))
        # A job left queued blocks the next one (USER_ACTIVE_AI_JOBS=1);
        # finish it before the second scope case.
        claim_next_job()
        execute_job(job)
        job2 = self._job(AIJob.Kind.EDIT_SENTENCE, "0:9")
        with self.assertRaises(EditScopeError):
            apply_edit(job2, self._decision(AIJob.Kind.EDIT_SENTENCE))

    def test_broader_edit_outcome_is_no_charge(self):
        job = self._job(AIJob.Kind.EDIT_SENTENCE, "0:0")
        result = apply_edit(job, {"outcome": "needs_broader_edit", "message_ru": "Нужна правка соседних частей"})
        self.assertEqual(result["outcome"], AIJob.Outcome.BROADER_EDIT)
        self.assertEqual(result["payload"], {})


class EditJobFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="editflow", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.base = make_base_revision(self.project)
        # The first successful series set the head automatically in real flow.
        self.project.head_revision = self.base
        self.project.save(update_fields=["head_revision"])

    def test_execute_edit_creates_revision_but_head_stays(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.EDIT_SENTENCE, idempotency_key=uuid4().hex,
            instruction="make it simpler", project=self.project, base_revision=self.base,
            target_id="0:0",
        )
        claimed = claim_next_job()
        self.assertEqual(claimed.pk, job.pk)
        execute_job(claimed)
        claimed.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(claimed.status, AIJob.Status.SUCCEEDED)
        self.assertEqual(claimed.outcome, AIJob.Outcome.DELIVERED)
        # Spec: an edit adds a revision; head moves only after explicit apply.
        self.assertEqual(self.project.head_revision, self.base)
        new_revision = Revision.objects.get(pk=claimed.result_reference["revision_id"])
        self.assertEqual(new_revision.payload["parts"][0]["sentences"][0]["en"], "Edited sentence (test provider).")
        # unchanged neighbours survive
        self.assertEqual(new_revision.payload["parts"][0]["sentences"][1]["en"], "Second idea.")
        # credits charged for a delivered edit (cost edit_sentence = 1)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 29)

    def test_broader_edit_releases_reservation_without_debit(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.EDIT_PART, idempotency_key=uuid4().hex,
            instruction="broader change please", project=self.project, base_revision=self.base,
            target_id="0",
        )
        execute_job(claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.SUCCEEDED)
        self.assertEqual(job.outcome, AIJob.Outcome.BROADER_EDIT)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 30)
        self.assertEqual(wallet.reserved_credits, 0)
        # Only the base revision exists; no new snapshot for a no-charge outcome.
        self.assertEqual(Revision.objects.filter(project=self.project).count(), 1)
        self.assertEqual(self.project.head_revision, self.base)

    def test_generate_sets_head_and_creates_quality_report(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex,
            instruction="Socratic method", project=self.project,
        )
        execute_job(claim_next_job())
        job.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED)
        self.assertIsNotNone(self.project.head_revision)
        report = QualityReport.objects.filter(revision=self.project.head_revision).first()
        self.assertIsNotNone(report)
        self.assertEqual(report.revision_content_hash, self.project.head_revision.content_hash)

    def test_failed_review_still_delivers_text_with_note(self):
        """Owner decision (Nov 2026): the review never discards the text.
        A failed review delivers the candidate with a saved review note and
        charges credits — the user always receives a text."""
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex,
            instruction="fail-review", project=self.project,
        )
        execute_job(claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.status, AIJob.Status.SUCCEEDED)
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED)
        # Credits are charged: a delivered text is a paid result.
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 20)
        self.assertEqual(wallet.reserved_credits, 0)
        # The candidate is saved as head revision with a review note.
        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.head_revision)
        note = self.project.head_revision.payload.get("note", "")
        self.assertIn("Замечания проверки", note)
        self.assertIn("блокирующая", note)
        # Quality report records the failed review for the saved revision.
        report = QualityReport.objects.filter(revision=self.project.head_revision).first()
        self.assertIsNotNone(report)
        self.assertEqual(report.review_json.get("verdict"), "fail")

    def test_repair_review_flow_repairs_and_reapplies_review(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex,
            instruction="repair-review", project=self.project,
        )
        execute_job(claim_next_job())
        job.refresh_from_db()
        # The fake provider repairs, the re-review passes, credits charged.
        self.assertEqual(job.outcome, AIJob.Outcome.DELIVERED)
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.total_credits, 20)
        self.project.refresh_from_db()
        revision_payload = self.project.head_revision.payload
        self.assertTrue(any("repaired" in s["en"] for part in revision_payload["parts"] for s in part["sentences"]))


class ApplyHeadTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="headuser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.client.force_login(self.user)
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.base = make_base_revision(self.project)
        self.project.head_revision = self.base
        self.project.save(update_fields=["head_revision"])

    def _new_edit_revision(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.EDIT_SENTENCE, idempotency_key=uuid4().hex,
            instruction="fix", project=self.project, base_revision=self.base, target_id="0:0",
        )
        execute_job(claim_next_job())
        job.refresh_from_db()
        return Revision.objects.get(pk=job.result_reference["revision_id"])

    def test_apply_head_switches_and_repeat_is_noop(self):
        revision = self._new_edit_revision()
        url = reverse("workshop:apply_head", args=[self.project.pk])
        response = self.client.post(url, {"revision_id": str(revision.pk)})
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.head_revision, revision)
        # Repeat apply is a no-op, not a rollback to the older revision.
        self.client.post(url, {"revision_id": str(revision.pk)})
        self.project.refresh_from_db()
        self.assertEqual(self.project.head_revision, revision)

    def test_diff_shows_changed_sentences(self):
        revision = self._new_edit_revision()
        url = reverse("workshop:revision_diff", args=[self.project.pk, str(revision.pk)])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["same_hash"])
        changed = [item for item in data["diff"] if item["part"] == 0 and item["sentence"] == 0]
        self.assertTrue(changed)
        self.assertEqual(changed[0]["new"]["en"], "Edited sentence (test provider).")
        # unchanged sentence not in diff
        self.assertFalse([item for item in data["diff"] if item["part"] == 0 and item["sentence"] == 1])

    def test_apply_head_rejects_foreign_revision(self):
        other = User.objects.create_user(login="headuser2", password="TestPassword123!")
        other_project = Project.objects.create(owner=other, title="O", mode=Project.Mode.EXPLAIN)
        foreign = make_base_revision(other_project)
        url = reverse("workshop:apply_head", args=[self.project.pk])
        response = self.client.post(url, {"revision_id": str(foreign.pk)})
        self.assertEqual(response.status_code, 404)
        self.project.refresh_from_db()
        self.assertEqual(self.project.head_revision, self.base)

    def test_edit_forms_require_login_and_csrf(self):
        edit_url = reverse("workshop:edit_fragment", args=[self.project.pk])
        self.client.logout()
        response = self.client.post(edit_url, {"kind": "edit_sentence", "target": "0:0", "instruction": "x"})
        self.assertEqual(response.status_code, 302)  # redirect to login
        self.client.force_login(self.user)
        response = self.client.post(edit_url, {"kind": "edit_sentence", "target": "9:0", "instruction": "x"})
        self.assertEqual(response.status_code, 400)

    def test_create_project_without_wallet_returns_402_not_500(self):
        """Regression: rendering the 402 page inside a broken atomic block
        crashed the account_summary context processor on the aborted
        transaction (500 instead of a clean 402)."""
        # Ledger rows are PROTECT'ed; empty the balance instead of deleting.
        Wallet.objects.filter(user=self.user).update(total_credits=0, reserved_credits=0)
        url = reverse("workshop:create_project")
        response = self.client.post(url, {"mode": "explain_topic", "topic": "Stoic acceptance"})
        self.assertEqual(response.status_code, 402)
        self.assertFalse(Project.objects.filter(owner=self.user, title="Stoic acceptance").exists())
