"""Stage 9: user data export and purge."""

import json
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.export_service import export_user_data
from apps.accounts.purge_service import purge_user_data
from apps.ai.models import AIJob
from apps.ai.services import claim_next_job, create_job
from apps.credits.models import CreditTransaction, Wallet, grant_welcome_credits
from apps.library.models import Favorite, Publication, SavedWord
from apps.operations.models import AuditEvent
from apps.sources.models import Embedding, SourceChunk, SourceDocument
from apps.workshop.models import Message, Project, Revision

PAYLOAD = {
    "title_en": "A series",
    "title_ru": "Серия",
    "parts": [{"title_en": "P", "sentences": [{"en": "One.", "ru": "Один."}]}],
    "terms": [],
    "glossary": [],
}


class ExportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="expuser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="Тема", mode=Project.Mode.EXPLAIN)
        Revision.objects.create(project=self.project, payload=PAYLOAD)
        Message.objects.create(project=self.project, owner=self.user, role="user", kind="note", body="Вопрос")
        SavedWord.objects.create(user=self.user, term="virtue", translation_ru="добродетель")

    def test_export_contains_own_data_only(self):
        data = export_user_data(self.user)
        self.assertEqual(data["account"]["login"], "expuser")
        self.assertEqual(len(data["projects"]), 1)
        self.assertEqual(data["projects"][0]["revisions"][0]["payload"], PAYLOAD)
        self.assertTrue(data["projects"][0]["messages"])
        self.assertTrue(data["credits"]["transactions"])
        self.assertEqual(data["words"][0]["term"], "virtue")

    def test_export_excludes_other_users_data(self):
        other = User.objects.create_user(login="otheruser", password="TestPassword123!")
        other_project = Project.objects.create(owner=other, title="Чужой", mode=Project.Mode.EXPLAIN)
        Revision.objects.create(project=other_project, payload=PAYLOAD)
        data = export_user_data(self.user)
        titles = [project["title"] for project in data["projects"]]
        self.assertEqual(titles, ["Тема"])

    def test_http_export_attachment(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("account:export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        body = json.loads(response.content.decode("utf-8"))
        self.assertEqual(body["account"]["login"], "expuser")

    def test_management_command_export(self):
        import io

        out = io.StringIO()
        call_command("export_user_data", "--login", "expuser", stdout=out)
        data = json.loads(out.getvalue())
        self.assertEqual(data["account"]["login"], "expuser")


class PurgeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="purgeuser", password="TestPassword123!")
        grant_welcome_credits(self.user)
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.revision = Revision.objects.create(project=self.project, payload=PAYLOAD)
        Message.objects.create(project=self.project, owner=self.user, role="user", kind="note", body="x")
        SavedWord.objects.create(user=self.user, term="term", translation_ru="т")
        self.document = SourceDocument.objects.create(
            owner=self.user, content_hash="hash1", normalized_text="текст",
            origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
        )
        chunk = SourceChunk.objects.create(
            document=self.document, ordinal=0, text="текст", content_hash="hash1c",
        )
        # Vector of 1536 dims for the Embedding row.
        Embedding.objects.create(source_chunk=chunk, model="m", dimensions=1536, vector=[0.0] * 1536, content_hash="h")
        self.wallet = Wallet.objects.get(user=self.user)

    def test_purge_removes_private_data_keeps_ledger(self):
        stats = purge_user_data(self.user, reason="test purge")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(self.user.has_usable_password())
        self.assertTrue(self.user.login.startswith("deleted-"))
        self.assertEqual(self.user.public_alias, "Автор удалён")
        # Private data gone.
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(Revision.objects.filter(pk=self.revision.pk).exists())
        self.assertFalse(SourceDocument.objects.filter(pk=self.document.pk).exists())
        self.assertFalse(SourceChunk.objects.exists())
        self.assertFalse(Embedding.objects.exists())
        self.assertFalse(SavedWord.objects.filter(user=self.user).exists())
        self.assertFalse(Message.objects.exists())
        # Ledger preserved and not owned by a reusable login.
        self.assertTrue(CreditTransaction.objects.filter(wallet=self.wallet).exists())
        self.assertTrue(AuditEvent.objects.filter(action="account_purge", object_reference=f"user:{self.user.pk}").exists())

    def test_purge_cancels_queued_jobs_and_releases_reservation(self):
        job, _ = create_job(
            owner=self.user, kind=AIJob.Kind.GENERATE, idempotency_key=uuid4().hex,
            instruction="x", project=self.project,
        )
        claim_next_job()
        purge_user_data(self.user, reason="test purge 2")
        job.refresh_from_db()
        self.user.refresh_from_db()
        wallet = Wallet.objects.get(user=self.user)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(job.status, AIJob.Status.FAILED)
        self.assertEqual(job.error_code, "account_deleted")

    def test_management_command_refuses_staff(self):
        User.objects.create_user(login="staffuser", password="TestPassword123!", is_staff=True)
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("purge_user_data", "--login", "staffuser")

    def test_http_delete_purges_and_logs_out(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("account:delete"), {"password": "TestPassword123!"})
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertFalse(Project.objects.filter(owner_id=self.user.pk).exists())
        # Session ended.
        response = self.client.get(reverse("account:dashboard"))
        self.assertEqual(response.status_code, 302)
