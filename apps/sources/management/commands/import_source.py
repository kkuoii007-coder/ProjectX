"""Import a TXT/MD/PDF file as a private user source document."""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.sources.services import ExtractionError, create_document_from_upload


class Command(BaseCommand):
    help = "Import a TXT/MD/PDF file as a private user source document"

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True, help="Path to the file on disk")
        parser.add_argument("--login", required=True, help="Owner account login")
        parser.add_argument("--title", default="", help="Human-readable title")

    def handle(self, *args, **options):
        path = options["path"]
        if not os.path.isfile(path):
            raise CommandError(f"Файл не найден: {path}")
        size = os.path.getsize(path)
        if size > settings.SOURCE_UPLOAD_MAX_BYTES:
            raise CommandError(f"Файл {size} байт превышает лимит {settings.SOURCE_UPLOAD_MAX_BYTES}")
        owner = User.objects.filter(login=options["login"]).first()
        if not owner:
            raise CommandError(f"Пользователь не найден: {options['login']}")
        with open(path, "rb") as handle:
            data = handle.read()
        try:
            document, created = create_document_from_upload(
                owner=owner, filename=os.path.basename(path), data=data, title=options["title"]
            )
        except ExtractionError as exc:
            raise CommandError(f"Импорт отклонён ({exc.code}): {exc}") from exc
        if created:
            self.stdout.write(self.style.SUCCESS(f"Документ создан: {document.pk}"))
        else:
            self.stdout.write(f"Дубликат по hash, существующий документ: {document.pk}")
        self.stdout.write(f"Состояние: {document.processing_state}")