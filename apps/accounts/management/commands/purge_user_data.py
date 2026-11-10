"""Purge one user's private data (stage 9).

Ledger and audit survive without personal identifiers; the wallet is kept
with anonymized totals for accounting integrity.
"""

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.accounts.purge_service import purge_user_data


class Command(BaseCommand):
    help = "Purge a user's private data (deactivate, anonymize, keep ledger)"

    def add_arguments(self, parser):
        parser.add_argument("--login", required=True)
        parser.add_argument("--reason", default="Удаление аккаунта по запросу владельца")

    def handle(self, *args, **options):
        user = User.objects.filter(login=options["login"]).first()
        if not user:
            raise CommandError(f"Пользователь не найден: {options['login']}")
        if user.is_staff and user.is_active:
            raise CommandError("Нельзя удалить активного staff-аккаунта этой командой")
        stats = purge_user_data(user, reason=options["reason"])
        self.stdout.write(self.style.SUCCESS(f"Аккаунт удалён: {options['login']} — {stats}"))
