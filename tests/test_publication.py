"""Stage 8: editorial publication gates and complaints (reports)."""

from uuid import uuid4

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.ai.models import QualityReport
from apps.library.models import KnowledgeCard, Publication, Thinker
from apps.library.services import PublicationBlocked, publish_revision
from apps.operations.models import AuditEvent, Report
from apps.sources.models import SourceDocument
from apps.workshop.models import Project, Revision, RevisionKnowledgeCard

PAYLOAD = {
    "title_en": "A series",
    "title_ru": "Серия",
    "parts": [{"title_en": "P", "sentences": [{"en": "One.", "ru": "Один."}]}],
    "terms": [],
    "glossary": [],
}


class PublicationGateTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(login="pubadmin", password="TestPassword123!", is_staff=True, is_superuser=True)
        self.user = User.objects.create_user(login="pubuser", password="TestPassword123!")
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.revision = Revision.objects.create(project=self.project, payload=PAYLOAD)

    def _make_report(self, factual_state="supported", blocking=False):
        return QualityReport.objects.create(
            revision=self.revision,
            revision_content_hash=self.revision.content_hash,
            review_json={"issues": [{"code": "x", "severity": "blocking" if blocking else "nonblocking", "reason_ru": "r"}]},
            factual_state=factual_state,
        )

    def _link_card(self, status=KnowledgeCard.Status.VERIFIED):
        card, _ = KnowledgeCard.objects.update_or_create(
            slug="test", version=1,
            defaults={"title": "Test", "verification_status": status, "verified_at": timezone.now()},
        )
        RevisionKnowledgeCard.objects.create(revision=self.revision, knowledge_card=card)
        return card

    def test_publish_requires_report_card_and_rights(self):
        # No report, no card -> blocked.
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=self.revision, admin=self.admin, slug="x1")
        self._make_report()
        # Report exists but no verified card -> blocked.
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=self.revision, admin=self.admin, slug="x2")
        self._link_card()
        # Blocking issue in the report -> blocked.
        self._make_report(blocking=True)
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=self.revision, admin=self.admin, slug="x3")
        report = self._make_report(blocking=False)
        publication, created = publish_revision(revision=self.revision, admin=self.admin, slug="socrates-questions")
        self.assertTrue(created)
        self.assertEqual(publication.status, Publication.Status.PUBLISHED)
        self.assertEqual(publication.source_type, "editorial")
        self.assertEqual(publication.quality_report, report)
        # Audit record saved.
        self.assertTrue(AuditEvent.objects.filter(action="editorial_publish", object_reference=f"revision:{self.revision.pk}").exists())

    def test_repeat_action_is_idempotent(self):
        self._make_report()
        self._link_card()
        first, created1 = publish_revision(revision=self.revision, admin=self.admin, slug="same-slug")
        second, created2 = publish_revision(revision=self.revision, admin=self.admin, slug="same-slug")
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Publication.objects.filter(revision=self.revision).count(), 1)

    def test_stale_report_does_not_authorize_new_content(self):
        self._make_report()
        self._link_card()
        # The revision payload is immutable: changing content means creating
        # a new revision whose hash no existing report matches.
        payload = dict(PAYLOAD)
        payload["title_ru"] = "Серия изменена"
        new_revision = Revision.objects.create(project=self.project, payload=payload)
        with self.assertRaises(PublicationBlocked):
            publish_revision(revision=new_revision, admin=self.admin, slug="x4")
        # Direct payload mutation on a saved revision is refused (immutability).
        with self.assertRaises(ValueError):
            self.revision.payload = {**PAYLOAD, "title_ru": "другая"}
            self.revision.save()

    def test_publication_survives_head_change(self):
        self._make_report()
        self._link_card()
        publication, _ = publish_revision(revision=self.revision, admin=self.admin, slug="stable")
        # Move head elsewhere; the publication keeps pointing at the revision.
        other = Revision.objects.create(project=self.project, payload={**PAYLOAD, "title_ru": "Другая"})
        self.project.head_revision = other
        self.project.save(update_fields=["head_revision"])
        publication.refresh_from_db()
        self.assertEqual(publication.revision, self.revision)
        self.assertEqual(publication.status, Publication.Status.PUBLISHED)

    def test_private_unknown_rights_block_publication(self):
        self._make_report()
        self._link_card()
        document = SourceDocument.objects.create(
            owner=self.user, content_hash="abc", normalized_text="text",
            origin=SourceDocument.Origin.USER, visibility=SourceDocument.Visibility.PRIVATE,
            rights_state="unknown",
        )
        from apps.workshop.models import RevisionSource

        RevisionSource.objects.create(revision=self.revision, document=document, role="context")
        with self.assertRaises(PublicationBlocked) as ctx:
            publish_revision(revision=self.revision, admin=self.admin, slug="x5")
        self.assertIn("rights", str(ctx.exception).lower())


class ReportTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(login="reporter", password="TestPassword123!")
        self.client.force_login(self.user)
        # A published editorial revision like seed_demo.
        self.project = Project.objects.create(owner=self.user, title="P", mode=Project.Mode.EXPLAIN)
        self.revision = Revision.objects.create(project=self.project, payload=PAYLOAD)
        self.publication = Publication.objects.create(
            slug="reportable", revision=self.revision, status=Publication.Status.PUBLISHED,
            published_at=timezone.now(), published_by=self.user,
        )

    def test_report_created_with_valid_category(self):
        url = reverse("library:create_report", args=[self.publication.slug])
        response = self.client.post(url, {"category": "factual_error", "details": "Здесь ошибка"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Report.objects.filter(publication=self.publication, category="factual_error").exists())

    def test_report_requires_auth_and_category(self):
        url = reverse("library:create_report", args=[self.publication.slug])
        self.client.logout()
        response = self.client.post(url, {"category": "other", "details": "x"})
        self.assertEqual(response.status_code, 401)
        self.client.force_login(self.user)
        response = self.client.post(url, {"category": "nope", "details": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Report.objects.exists())

    def test_single_report_does_not_hide_publication(self):
        url = reverse("library:create_report", args=[self.publication.slug])
        self.client.post(url, {"category": "other", "details": "x"})
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.status, Publication.Status.PUBLISHED)

    def test_rate_limit_three_per_day(self):
        url = reverse("library:create_report", args=[self.publication.slug])
        for _ in range(3):
            self.assertEqual(self.client.post(url, {"category": "other", "details": "x"}).status_code, 302)
        response = self.client.post(url, {"category": "other", "details": "x"})
        self.assertEqual(response.status_code, 429)
