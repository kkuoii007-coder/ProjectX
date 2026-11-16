from django.core.management.base import BaseCommand, CommandError

from apps.library.models import Publication
from apps.library.services import PublicationBlocked, publish_revision

from ._editorial import read_json, resolve_admin, submitted_revision


class Command(BaseCommand):
    help = "Publish a submitted and reviewed text through the publication gate (publish_revision)."

    def add_arguments(self, parser):
        parser.add_argument("path", help="content/texts/<slug>.json")
        parser.add_argument("--admin", default="", help="Login of the administrator who publishes")

    def handle(self, *args, **options):
        data = read_json(options["path"])
        revision = submitted_revision(data)
        admin = resolve_admin(options["admin"])
        # A text that is already published keeps its address; a new text
        # gets the address named in its file (the gate refuses a taken one).
        earlier = Publication.objects.filter(revision__project=revision.project).order_by("-published_at").first()
        slug = earlier.slug if earlier else data.get("slug")
        try:
            publication, created = publish_revision(
                revision=revision,
                admin=admin,
                slug=slug,
                reason=f"publish_text by {admin.login}",
            )
        except PublicationBlocked as exc:
            raise CommandError("Публикация отклонена:\n  " + "\n  ".join(str(exc).split("; "))) from exc
        state = "Опубликовано" if created else "Уже опубликовано"
        self.stdout.write(self.style.SUCCESS(f"{state}: /read/{publication.slug}/"))
