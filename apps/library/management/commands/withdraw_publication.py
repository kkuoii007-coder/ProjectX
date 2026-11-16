from django.core.management.base import BaseCommand, CommandError

from apps.library.models import Publication
from apps.library.services import retract_publication

from ._editorial import resolve_admin


class Command(BaseCommand):
    help = "Withdraw a publication by slug, with an audit record."

    def add_arguments(self, parser):
        parser.add_argument("slug")
        parser.add_argument("--admin", default="", help="Login of the administrator who withdraws")
        parser.add_argument("--reason", default="Снято с публикации владельцем")

    def handle(self, *args, **options):
        publication = Publication.objects.filter(slug=options["slug"]).first()
        if publication is None:
            self.stdout.write(f"Публикации {options['slug']} нет: снимать нечего")
            return
        if publication.status == Publication.Status.WITHDRAWN:
            self.stdout.write(f"{publication.slug}: уже снято")
            return
        retract_publication(publication=publication, admin=resolve_admin(options["admin"]), reason=options["reason"])
        self.stdout.write(self.style.SUCCESS(f"Снято с публикации: {publication.slug}"))
