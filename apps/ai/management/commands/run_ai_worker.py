import time

from django.core.management.base import BaseCommand

from apps.ai.services import claim_next_job, execute_job


class Command(BaseCommand):
    help = "Sequentially process AI jobs from PostgreSQL"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--idle-seconds", type=float, default=2.0)

    def handle(self, *args, **options):
        while True:
            job = claim_next_job()
            if job:
                execute_job(job)
                self.stdout.write(f"Processed {job.pk}: {job.status}")
            elif options["once"]:
                break
            else:
                time.sleep(options["idle_seconds"])
