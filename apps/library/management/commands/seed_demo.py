import hashlib
import json

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.ai.models import QualityReport
from apps.library.models import Collection, CollectionItem, KnowledgeCard, Publication, Thinker, Topic
from apps.workshop.models import Project, Revision


PAYLOAD = {
    "title_ru": "Сократ: почему вопросы помогают мыслить",
    "lead_ru": "Короткое введение в сократический метод: признание незнания, вопросы и проверка убеждений.",
    "parts": [
        {
            "title_en": "A different kind of wisdom",
            "title_ru": "Другой вид мудрости",
            "sentences": [
                {"en": "Socrates lived in Athens in the fifth century BCE.", "ru": "Сократ жил в Афинах в пятом веке до нашей эры."},
                {"en": "He did not write books, so we know him mainly through other authors.", "ru": "Он не писал книг, поэтому мы знаем его главным образом по работам других авторов."},
                {"en": "Plato was his student and made him a central figure in many dialogues.", "ru": "Платон был его учеником и сделал его центральной фигурой многих диалогов."},
                {"en": "Socrates said that knowing one's ignorance was the beginning of wisdom.", "ru": "Сократ считал, что осознание своего невежества — начало мудрости."},
            ],
        },
        {
            "title_en": "Questions instead of a lecture",
            "title_ru": "Вопросы вместо лекции",
            "sentences": [
                {"en": "Socrates asked people what they meant by words such as justice or courage.", "ru": "Сократ спрашивал людей, что они понимают под такими словами, как справедливость или мужество."},
                {"en": "A first answer often sounded clear but failed after a few more questions.", "ru": "Первый ответ часто звучал ясно, но оказывался недостаточным после нескольких дополнительных вопросов."},
                {"en": "The aim was not to win an argument at any price.", "ru": "Цель состояла не в том, чтобы любой ценой победить в споре."},
                {"en": "The aim was to test whether a belief could survive careful examination.", "ru": "Цель состояла в том, чтобы проверить, выдерживает ли убеждение внимательное исследование."},
            ],
        },
        {
            "title_en": "The examined life",
            "title_ru": "Исследованная жизнь",
            "sentences": [
                {"en": "For Socrates, philosophy concerned the way a person should live.", "ru": "Для Сократа философия касалась того, как человеку следует жить."},
                {"en": "He connected knowledge with care for one's character and choices.", "ru": "Он связывал знание с заботой о своём характере и выборе."},
                {"en": "An examined life does not require having an answer to everything.", "ru": "Исследованная жизнь не требует иметь ответ на всё."},
                {"en": "It requires honesty about our reasons and readiness to correct them.", "ru": "Она требует честности в отношении наших оснований и готовности их исправлять."},
            ],
        },
    ],
    "terms": [
        {"en": "ignorance", "ru": "невежество, незнание"},
        {"en": "examination", "ru": "критическое исследование, проверка"},
        {"en": "virtue", "ru": "добродетель"},
    ],
    "simplification_note_ru": "Редакционное введение, а не перевод одного диалога. Исторический Сократ и персонаж Платона не полностью совпадают.",
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
