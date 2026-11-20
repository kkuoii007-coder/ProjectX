"""Put a book on the editorial shelf from its edition manifest.

    manage.py import_shelf_book --manifest content/sources/<book>/edition.json
        [--supersedes <uuid>] [--dry-run] [--print-id]

Writes the work, the edition, the document and its chunks with locators.
No network. Embeddings are built afterwards by `index_source --document`.
A repeated run with the same file creates nothing.
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.sources.shelf_import import ShelfImportError, import_shelf_book


class Command(BaseCommand):
    help = "Import a shelf book (clean text, chunks with locators) from an edition manifest"

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True, help="Path to edition.json")
        parser.add_argument("--supersedes", default=None, help="UUID of the editorial document this one replaces")
        parser.add_argument("--dry-run", action="store_true", help="Parse and report, write nothing")
        parser.add_argument(
            "--print-id", action="store_true",
            help="Print only the document UUID to stdout (the report goes to stderr)",
        )

    def handle(self, *args, **options):
        try:
            report = import_shelf_book(
                options["manifest"], supersedes=options["supersedes"], dry_run=options["dry_run"]
            )
        except ShelfImportError as exc:
            raise CommandError(str(exc)) from exc
        out = self.stderr if options["print_id"] else self.stdout
        say = lambda line: out.write(line, style_func=lambda text: text)  # noqa: E731

        if report.dry_run:
            say("Пробный запуск: в базу ничего не записано")
        if report.created:
            say(f"Документ создан: {report.document_id}")
        elif report.document_id:
            say(f"уже импортировано: {report.document_id}")
        if report.document_id and not report.created:
            say(f"Фрагментов в документе: {report.chunks} (не менялись)")
        else:
            say(f"Фрагментов: {report.chunks}")
        if report.sizes:
            sizes = report.sizes
            say(f"Размер фрагментов, слов: мин {sizes['min']}, медиана {sizes['median']}, макс {sizes['max']}")
            say("Распределение: " + ", ".join(f"{name}: {count}" for name, count in sizes["buckets"].items()))
        stats = report.stats
        if stats:
            say(
                f"Книг: {stats['books']}, страниц Беккера: {stats['pages']}, слов: {stats['words']}, "
                f"знаков: {stats['characters']}"
            )
            say(
                f"Вырезано сносок: {stats['notes_removed']}, греческих вставок: {stats['greek_removed']} "
                f"(из них вне сносок: {stats.get('greek_in_text', 0)}), лакун <gap>: {stats['gaps']}"
            )
            say(f"Меток, служивших единственной границей между словами: {stats.get('milestones_as_spaces', 0)}")
            say(
                f"Страниц, чьё начало взято по метке, а не по началу раздела: {len(stats['pages_moved_from_div'])}"
                + (f" ({', '.join(stats['pages_moved_from_div'])})" if stats["pages_moved_from_div"] else "")
            )
            if stats["pages_without_milestone"]:
                say(f"Страниц без метки (начало по разделу): {', '.join(stats['pages_without_milestone'])}")
        if report.edition_changed:
            say("Метаданные издания обновлены: " + ", ".join(report.edition_changed))
        if report.edition_deferred:
            say(
                "Отложено до готовности этого документа (пока в поиске прежний): "
                + ", ".join(report.edition_deferred)
            )
        if report.supersedes_set:
            say(f"Документ {report.supersedes_set} помечен заменённым (уйдёт из поиска, когда новый станет ready)")
        for note in report.notes:
            say(note)
        if report.created:
            say(f"Дальше: manage.py index_source --document {report.document_id}")
        if options["print_id"] and report.document_id:
            self.stdout.write(str(report.document_id))
        if options["verbosity"] >= 2:
            say(json.dumps(stats, ensure_ascii=False, indent=2))
