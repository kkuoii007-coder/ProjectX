"""Index an imported source document: chunking + OpenRouter embeddings."""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.sources.models import SourceDocument
from apps.sources.retrieval import index_document


class Command(BaseCommand):
    help = "Chunk a source document and create embeddings via OpenRouter"

    def add_arguments(self, parser):
        parser.add_argument("--document", required=True, help="SourceDocument UUID")
        parser.add_argument("--reindex", action="store_true", help="Re-index even if ready")

    def handle(self, *args, **options):
        try:
            document_id = UUID(options["document"])
        except ValueError as exc:
            raise CommandError("Некорректный UUID документа") from exc
        document = SourceDocument.objects.filter(id=document_id).first()
        if not document:
            raise CommandError("Документ не найден")
        if document.processing_state == SourceDocument.Processing.READY and not options["reindex"]:
            self.stdout.write(f"Документ уже ready, используйте --reindex: {document.pk}")
            return
        result = index_document(document)
        if result["state"] == "failed":
            raise CommandError(f"Индексация не удалась ({result.get('error')}): {result.get('message')}")
        self.stdout.write(self.style.SUCCESS(f"Проиндексировано фрагментов: {result['indexed']}"))