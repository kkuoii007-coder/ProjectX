"""Shared test fixtures.

Tests may build report/card fixtures directly; real content never does:
it goes through submit_text -> live review -> publish_revision.
"""

from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.ai.models import QualityReport
from apps.library.editorial import card_version_records
from apps.library.models import Collection, CollectionItem, KnowledgeCard, Publication, Thinker, Topic
from apps.library.services import publish_revision
from apps.workshop.models import Project, RevisionKnowledgeCard


def publish_demo_fixture():
    """The seed_demo draft, published through the gate with fixture
    report and card. Returns the Publication (slug socrates-questions)."""
    call_command("seed_demo", verbosity=0)
    project = Project.objects.get(title="Editorial: Socratic method")
    revision = project.head_revision
    admin = User.objects.filter(login="fixtureadmin").first() or User.objects.create_user(
        login="fixtureadmin", password="TestPassword123!", is_staff=True, is_superuser=True
    )
    card, _ = KnowledgeCard.objects.update_or_create(
        slug="socratic-method", version=1,
        defaults={"title": "Socratic method", "verification_status": KnowledgeCard.Status.VERIFIED, "verified_at": timezone.now()},
    )
    RevisionKnowledgeCard.objects.get_or_create(revision=revision, knowledge_card=card)
    QualityReport.objects.get_or_create(
        revision=revision,
        revision_content_hash=revision.content_hash,
        defaults={
            "factual_state": "supported",
            "eligibility_state": "eligible",
            "review_json": {"verdict": "pass", "issues": []},
            "card_versions_json": card_version_records(revision),
        },
    )
    thinker = Thinker.objects.get(slug="socrates")
    publication, _ = publish_revision(revision=revision, admin=admin, slug="socrates-questions", thinker=thinker)
    publication.topics.set([Topic.objects.get(slug="socratic-method")])
    collection, _ = Collection.objects.update_or_create(
        slug="first-steps",
        defaults={"title_ru": "Первые шаги в философии", "description_ru": "Короткие редакционные введения для спокойного чтения."},
    )
    CollectionItem.objects.get_or_create(collection=collection, publication=publication, defaults={"position": 1})
    return Publication.objects.get(pk=publication.pk)
