"""Search source chunks (text half + pgvector hybrid).

    manage.py search_sources --query "..." [--login <login>] [--document <uuid>]
        [--top N] [--timing]

Without --login the editorial shelf is searched (it is the same for every
reader); with --login the scope is the owner's documents plus the shelf.
"""

import resource
import sys
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.sources.retrieval import search_scope


class Command(BaseCommand):
    help = "Hybrid text+pgvector search over indexed sources"

    def add_arguments(self, parser):
        parser.add_argument("--login", default=None, help="Owner account login; without it only the shelf is searched")
        parser.add_argument("--query", required=True)
        parser.add_argument("--document", default=None, help="Limit scope to one document UUID")
        parser.add_argument("--top", type=int, default=None)
        parser.add_argument("--timing", action="store_true", help="Print search time (ms) and peak memory of the process")

    def handle(self, *args, **options):
        owner = None
        if options["login"]:
            owner = User.objects.filter(login=options["login"]).first()
            if not owner:
                raise CommandError(f"Пользователь не найден: {options['login']}")
        document_ids = None
        if options["document"]:
            try:
                document_ids = [UUID(options["document"])]
            except ValueError as exc:
                raise CommandError("Некорректный UUID документа") from exc
        result = search_scope(owner=owner, query=options["query"], document_ids=document_ids, top_n=options["top"])

        if not result.vector_available:
            self.stdout.write(
                f"Векторная половина недоступна ({result.vector_error}): выдача только по текстовой половине"
            )
        if result.fts_terms:
            self.stdout.write("Слова текстового запроса: " + ", ".join(result.fts_terms))
        if result.missing_terms:
            self.stdout.write("Нет в источниках: " + ", ".join(result.missing_terms))
        if result.unmapped_terms:
            self.stdout.write("Русские слова без перевода в словаре полки: " + ", ".join(result.unmapped_terms))
        if not result.hits:
            self.stdout.write("Ничего не найдено в доступном scope")
        attributions = {}
        for number, hit in enumerate(result.hits, start=1):
            chunk = hit.chunk
            document = chunk.document
            if document.pk not in attributions:
                edition = document.edition
                attributions[document.pk] = edition.attribution() if edition else ""
            self.stdout.write(
                f"{number}. {hit.locator or 'без указателя'} [{chunk.id}] doc={chunk.document_id} ord={chunk.ordinal}\n"
                f"  scores={hit.scores}\n"
                + (f"  {attributions[document.pk]}\n" if attributions[document.pk] else "")
                + f"  {chunk.text[:160]}..."
            )
        if options["timing"]:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # ru_maxrss: kilobytes on Linux, bytes on macOS.
            megabytes = peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
            self.stdout.write(f"Время поиска: {result.elapsed_ms} мс; пик памяти процесса: {megabytes:.1f} МБ")
