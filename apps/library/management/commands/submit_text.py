from django.core.management.base import BaseCommand, CommandError

from apps.library.editorial import EditorialError, check_editorial_payload, load_card, submit_text

from ._editorial import cards_dir, read_json


class Command(BaseCommand):
    help = "Submit a ready-made text (JSON file) as an editorial revision, unchanged. Loads its knowledge cards first."

    def add_arguments(self, parser):
        parser.add_argument("path", help="content/texts/<slug>.json")
        parser.add_argument("--cards-dir", default="", help="Folder with <slug>-v<version>.json cards (default: content/cards)")

    def handle(self, *args, **options):
        data = read_json(options["path"])
        folder = cards_dir(options["cards_dir"])
        try:
            for ref in data.get("knowledge_cards") or []:
                card_file = folder / f"{ref.get('slug')}-v{ref.get('version')}.json"
                if card_file.is_file():
                    card, created = load_card(read_json(card_file), actor="cli:submit_text")
                    state = "загружена" if created else "без изменений"
                    self.stdout.write(f"Карточка {card.slug} v{card.version}: {state}, статус {card.verification_status}")
            revision, created = submit_text(data, actor="cli:submit_text")
        except EditorialError as exc:
            raise CommandError("Текст не принят:\n  " + "\n  ".join(exc.messages)) from exc
        _, diagnostics = check_editorial_payload(revision.payload)
        for warning in diagnostics["warnings_ru"]:
            self.stdout.write(f"  [длина, не блокирует] {warning}")
        state = "создана" if created else "уже существует"
        self.stdout.write(self.style.SUCCESS(f"Ревизия {revision.pk} {state}: {revision.content_hash}"))
        if created:
            self.stdout.write("У новой ревизии нет отчёта качества: перед публикацией нужна проверка (review_text).")
