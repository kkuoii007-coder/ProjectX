"""Export one user's data to stdout as JSON (stage 9)."""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.export_service import export_user_data
from apps.accounts.models import User


class Command(BaseCommand):
    help = "Export a user's data as JSON (owner's own data only)"

    def add_arguments(self, parser):
        parser.add_argument("--login", required=True)
        parser.add_argument("--output", default="", help="Write to this file instead of stdout")

    def handle(self, *args, **options):
        user = User.objects.filter(login=options["login"]).first()
        if not user:
            raise CommandError(f"Пользователь не найден: {options['login']}")
        data = export_user_data(user)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as handle:
                handle.write(payload)
            self.stdout.write(self.style.SUCCESS(f"Экспорт записан: {options['output']}"))
        else:
            self.stdout.write(payload)
