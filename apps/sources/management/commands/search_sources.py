"""Search source chunks inside the owner's scope (FTS + pgvector hybrid)."""

from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.sources.retrieval import hybrid_search


class Command(BaseCommand):
    help = "Hybrid FTS+pgvector search over indexed sources"

    def add_arguments(self, parser):
        parser.add_argument("--login", required=True, help="Owner account login")
        parser.add_argument("--query", required=True)
        parser.add_argument("--document", default=None, help="Limit scope to one document UUID")
        parser.add_argument("--top", type=int, default=None)

    def handle(self, *args, **options):
        owner = User.objects.filter(login=options["login"]).first()
        if not owner:
            raise CommandError(f"Пользователь не найден: {options['login']}")
        document_ids = None
        if options["document"]:
            try:
                document_ids = [UUID(options["document"])]
            except ValueError as exc:
                raise CommandError("Некорректный UUID документа") from exc
        results = hybrid_search(owner=owner, query=options["query"], document_ids=document_ids, top_n=options["top"])
        if not results:
            self.stdout.write("Ничего не найдено в доступном scope")
            return
        for chunk, scores in results:
            self.stdout.write(
                f"[{chunk.id}] doc={chunk.document_id} ord={chunk.ordinal} "
                f"scores={scores}\n  {chunk.text[:160]}..."
            )