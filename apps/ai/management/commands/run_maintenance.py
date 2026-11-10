from django.core.management.base import BaseCommand

from apps.ai.services import recover_stale_jobs


class Command(BaseCommand):
    help = "Recover expired AI leases and release user credit reservations"

    def handle(self, *args, **options):
        count = recover_stale_jobs()
        self.stdout.write(self.style.SUCCESS(f"Recovered stale jobs: {count}"))
