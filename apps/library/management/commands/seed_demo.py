import hashlib
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ai.models import QualityReport
from apps.library.models import Collection, CollectionItem, KnowledgeCard, Publication, Thinker, Topic
from apps.workshop.models import Project, Revision


PAYLOAD = {
    "title_ru": "Сократ: зачем спрашивать и проверять",
    "lead_ru": "Введение в сократический метод: признание незнания, вопросы вместо лекции, проверка убеждений.",
    "parts": [
        {
            "title_en": "A different kind of wisdom",
            "title_ru": "Другой вид мудрости",
            "sentences": [
                {"en": "Socrates lived in Athens almost twenty-five centuries ago and wrote no books.", "ru": "Сократ жил в Афинах почти двадцать пять веков назад и не написал книг."},
                {"en": "We know his thought mainly from the dialogues of Plato, his student, who made him a thinker that asks rather than proclaims.", "ru": "Его мысль известна по диалогам Платона, его ученика, который сделал его мыслителем, спрашивающим, а не провозглашающим."},
                {"en": "He began by admitting that he did not know, and called this awareness the beginning of wisdom.", "ru": "Он начинал с признания, что не знает, и называл это осознание началом мудрости."},
            ],
        },
        {
            "title_en": "Questions instead of a lecture",
            "title_ru": "Вопросы вместо лекции",
            "sentences": [
                {"en": "He moved by questions: he asked what people meant by words like justice or courage.", "ru": "Он двигался вопросами: спрашивал, что человек понимает под словами вроде справедливости или мужества."},
                {"en": "A first answer usually sounded convincing, but one more question exposed its weak point.", "ru": "Первый ответ обычно звучал убедительно, но следующий вопрос обнаруживал его слабое место."},
                {"en": "The purpose was not to defeat the speaker but to test whether a belief survives careful examination.", "ru": "Цель состояла не в том, чтобы победить собеседника, а в том, чтобы проверить, выдерживает ли убеждение проверку."},
            ],
        },
        {
            "title_en": "The examined life",
            "title_ru": "Исследованная жизнь",
            "sentences": [
                {"en": "For Socrates, philosophy was not a school subject but a way of life, tied to care for one's character.", "ru": "Для Сократа философия была не школьным предметом, а образом жизни, связанным с заботой о своём характере."},
                {"en": "Examination does not require an answer to every question; it requires honesty about our reasons.", "ru": "Исследование не требует ответа на каждый вопрос; оно требует честности в отношении наших оснований."},
            ],
        },
    ],
    "terms": [
        {"en": "ignorance", "ru": "невежество, незнание"},
        {"en": "examination", "ru": "критическое исследование, проверка"},
        {"en": "virtue", "ru": "добродетель"},
    ],
    "simplification_note_ru": "Редакционное введение, а не перевод одного диалога. Исторический Сократ и персонаж Платона различаются.",
}


class Command(BaseCommand):
    help = "Create an idempotent editorial demo publication in English and Russian"

    def handle(self, *args, **options):
        thinker, _ = Thinker.objects.update_or_create(slug="socrates", defaults={"name_en": "Socrates", "name_ru": "Сократ"})
        topic, _ = Topic.objects.update_or_create(slug="socratic-method", defaults={"name_en": "Socratic method", "name_ru": "Сократический метод"})
        # Editorial revisions live in a dedicated inactive system project,
        # independent of any real user account.
        from django.contrib.auth import get_user_model
        owner = get_user_model().objects.filter(login="editorial").first()
        if owner is None:
            owner = get_user_model().objects.create_user(login="editorial", password=None, is_active=False)
        project, _ = Project.objects.get_or_create(owner=owner, title="Editorial: Socratic method", mode=Project.Mode.EXPLAIN)
        canonical_hash = hashlib.sha256(json.dumps(PAYLOAD, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        revision = project.revisions.filter(content_hash=canonical_hash).first()
        if revision is None:
            revision = Revision.objects.create(project=project, payload=PAYLOAD)
        project.head_revision = revision
        project.save(update_fields=["head_revision"])
        card, _ = KnowledgeCard.objects.update_or_create(
            slug="socratic-method", version=1,
            defaults={"title": "Socratic method", "facts_json": [{"fact_id": "socrates-no-books", "text": "Socrates wrote no books", "type": "historical"}], "verification_status": KnowledgeCard.Status.VERIFIED, "verified_at": timezone.now()},
        )
        report = QualityReport.objects.filter(revision=revision, revision_content_hash=canonical_hash).first()
        if report is None:
            report = QualityReport.objects.create(revision=revision, revision_content_hash=canonical_hash, deterministic_json={"sentences_en_ru": True}, factual_state="supported", eligibility_state="eligible")
        publication, _ = Publication.objects.update_or_create(slug="socrates-questions", defaults={"revision": revision, "quality_report": report, "thinker": thinker, "status": Publication.Status.PUBLISHED, "published_at": timezone.now(), "published_by": owner})
        publication.topics.set([topic])
        collection, _ = Collection.objects.update_or_create(slug="first-steps", defaults={"title_ru": "Первые шаги в философии", "description_ru": "Короткие редакционные введения для спокойного чтения."})
        CollectionItem.objects.get_or_create(collection=collection, publication=publication, defaults={"position": 1})
        self.stdout.write(self.style.SUCCESS(f"Seeded publication: {publication.slug}"))
