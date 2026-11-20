"""Exact locator of a verbatim quotation in the books of the shelf.

    manage.py locate_quote --quote "man is by nature a political animal"
        [--quote "..."] [--document <uuid>]

No network and no embeddings: the quotation is looked up word for word in
the chunks, and the page comes from the chunk's `segments`. Without
--document the shelf is searched (READY editorial documents that are not
replaced); with it, that document in any state, e.g. right after
import_shelf_book and before index_source.
"""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.sources.locators import locate_quote
from apps.sources.models import SourceChunk, SourceDocument
from apps.sources.shelf import shelf_books


class Command(BaseCommand):
    help = "Print the exact locator (e.g. Politics 1.1253a) of verbatim quotations"

    def add_arguments(self, parser):
        parser.add_argument("--quote", action="append", required=True, help="Verbatim quotation; may be repeated")
        parser.add_argument("--document", default=None, help="SourceDocument UUID (default: the books on the shelf)")

    def handle(self, *args, **options):
        if options["document"]:
            try:
                document_id = UUID(options["document"])
            except ValueError as exc:
                raise CommandError("Некорректный UUID документа") from exc
            if not SourceDocument.objects.filter(id=document_id).exists():
                raise CommandError("Документ не найден")
            document_ids = [document_id]
        else:
            document_ids = [book.document.pk for book in shelf_books()]
            if not document_ids:
                raise CommandError("На полке нет книг: укажите --document")
        missing = []
        for quote in options["quote"]:
            quote = " ".join(quote.split())
            places = []
            chunks = SourceChunk.objects.filter(document_id__in=document_ids, text__contains=quote).order_by(
                "document_id", "ordinal"
            )
            for chunk in chunks:
                place = locate_quote(chunk.text, chunk.locator_json, quote)
                if place:
                    places.append(f"{place} (doc={chunk.document_id} ord={chunk.ordinal})")
            if not places:
                missing.append(quote)
                self.stdout.write(f"не найдено: «{quote}»")
                continue
            self.stdout.write(f"«{quote}»: " + "; ".join(places))
        if missing:
            raise CommandError(f"Не найдено цитат: {len(missing)} из {len(options['quote'])}")
