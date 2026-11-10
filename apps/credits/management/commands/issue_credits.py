"""Manual credit top-up for the closed test (TOPUP_MODE=manual_test).

Requires an explicit unique operation key and an audit reason; repeating the
command with the same key is a no-op instead of a second grant.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from apps.credits.models import Wallet


class Command(BaseCommand):
    help = "Issue credits to a user with a unique operation key and audit reason"

    def add_arguments(self, parser):
        parser.add_argument("--login", required=True)
        parser.add_argument("--amount", type=int, required=True)
        parser.add_argument("--operation-key", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        from django.conf import settings

        if settings.TOPUP_MODE not in {"manual_test", "payments"}:
            raise CommandError("Top-up is disabled by configuration")
        from apps.credits.models import CreditTransaction

        amount = options["amount"]
        if amount <= 0:
            raise CommandError("Amount must be positive")
        user = get_user_model().objects.filter(login=options["login"].lower()).first()
        if user is None:
            raise CommandError("User not found")
        operation_key = options["operation_key"][:100]
        with transaction.atomic():
            existing = CreditTransaction.objects.filter(operation_key=operation_key).first()
            if existing:
                self.stdout.write(self.style.WARNING(f"Operation key already used, no changes made ({existing.kind}, {existing.signed_amount})"))
                return
            wallet, _ = Wallet.objects.select_for_update().get_or_create(user=user)
            CreditTransaction.objects.create(
                wallet=wallet,
                kind=CreditTransaction.Kind.TOPUP,
                signed_amount=amount,
                operation_key=operation_key,
                reason=options["reason"][:255],
            )
            Wallet.objects.filter(pk=wallet.pk).update(total_credits=models.F("total_credits") + amount)
        self.stdout.write(self.style.SUCCESS(f"Issued {amount} credits to {user.login}"))
