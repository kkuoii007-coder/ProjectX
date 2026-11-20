"""Shared test fixtures.

Tests may build report/card fixtures directly; real content never does:
it goes through submit_text -> live review -> publish_revision.
"""

import hashlib
import io
import os
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.ai.models import QualityReport
from apps.library.editorial import card_version_records
from apps.library.models import Collection, CollectionItem, KnowledgeCard, Publication, Thinker, Topic
from apps.library.services import publish_revision
from apps.sources import retrieval
from apps.sources.embeddings import EmbeddingError
from apps.sources.models import SourceDocument
from apps.sources.retrieval import index_document
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


# --- a book on the shelf, without a paid call -------------------------------------------------

SHELF_MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "shelf", "edition.json")
# Names that are not on the shelf: a question about them points away from the book.
OFF_SHELF_NAMES = ("хайдеггер", "кант", "heidegger", "kant")


def shelf_vectors(texts):
    """Stands in for the paid embedding call (patch
    `apps.sources.retrieval.embed_texts`). Every text points one way, with a
    little deterministic noise; a short text that names a philosopher who is
    not on the shelf points the other way. So a question about the book is
    near every fragment and a question about Kant is near none."""
    vectors = []
    for text in texts:
        seed = hashlib.sha256(text.encode("utf-8")).digest()
        vector = [(seed[index % 32] / 255.0 - 0.5) * 0.02 for index in range(settings.AI_EMBEDDING_DIMENSIONS)]
        off_shelf = len(text) < 600 and any(name in text.lower() for name in OFF_SHELF_NAMES)
        vector[1 if off_shelf else 0] = 1.0
        vectors.append(vector)
    return vectors


def no_embeddings(texts):
    """The embedding call fails: the vector half of the search is down."""
    raise EmbeddingError("not_configured", "нет ключа")


def shelf_search():
    """Patch for a test class or method: the search works, no network."""
    return mock.patch.object(retrieval, "embed_texts", shelf_vectors)


def put_book_on_shelf():
    """The fixture book (Politics 1252b-1253a), imported with the command of
    PR 2 and indexed with `shelf_vectors`. Returns the READY document."""
    call_command("import_shelf_book", manifest=SHELF_MANIFEST, stdout=io.StringIO(), stderr=io.StringIO())
    document = SourceDocument.objects.filter(edition__key="fixture-politics-1252b-1253a").latest("created_at")
    with shelf_search():
        result = index_document(document)
    assert result["state"] == "ready", result
    document.refresh_from_db()
    return document
