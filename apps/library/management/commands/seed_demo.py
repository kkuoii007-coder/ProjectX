import hashlib
import json

from django.core.management.base import BaseCommand

from apps.library.editorial import editorial_owner
from apps.library.models import Thinker, Topic
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
    help = "Create an idempotent demo draft (Socrates) for local development; nothing is published"

    def handle(self, *args, **options):
        thinker, _ = Thinker.objects.update_or_create(slug="socrates", defaults={"name_en": "Socrates", "name_ru": "Сократ"})
        Topic.objects.update_or_create(slug="socratic-method", defaults={"name_en": "Socratic method", "name_ru": "Сократический метод"})
        # Editorial revisions live in a dedicated inactive system project,
        # independent of any real user account.
        owner = editorial_owner()
        project, _ = Project.objects.get_or_create(owner=owner, title="Editorial: Socratic method", mode=Project.Mode.EXPLAIN)
        canonical_hash = hashlib.sha256(json.dumps(PAYLOAD, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        revision = project.revisions.filter(content_hash=canonical_hash).first()
        if revision is None:
            revision = Revision.objects.create(project=project, payload=PAYLOAD)
        project.head_revision = revision
        project.save(update_fields=["head_revision"])
        # The demo text is a draft. It used to be published here directly,
        # bypassing publish_revision; texts now reach the library only
        # through submit_text -> review_text -> publish_text. An existing
        # publication is left as it is: withdraw it with
        # `withdraw_publication socrates-questions`.
        self.stdout.write(self.style.SUCCESS(f"Seeded draft revision {revision.pk} ({thinker.name_en}); not published"))
