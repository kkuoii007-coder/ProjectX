import time

from django.core.management.base import BaseCommand, CommandError

from apps.ai.models import AIJob, QualityReport
from apps.ai.services import JobError, claim_job, create_review_job, execute_job

from ._editorial import print_report, read_json, submitted_revision


class Command(BaseCommand):
    help = "Run one live review of a submitted text (one paid reviewer call) and print the remarks. The text is never changed."

    def add_arguments(self, parser):
        parser.add_argument("path", help="content/texts/<slug>.json")
        parser.add_argument("--wait", type=int, default=300, help="Seconds to wait for the AI worker (default 300)")
        parser.add_argument("--inline", action="store_true", help="Run the review in this process instead of waiting for the worker")

    def handle(self, *args, **options):
        revision = submitted_revision(read_json(options["path"]))
        try:
            job, created = create_review_job(revision=revision, actor="cli:review_text")
        except JobError as exc:
            raise CommandError(f"Проверка не запущена: {exc}") from exc
        self.stdout.write(f"Задача проверки {job.pk}: {'создана' if created else 'уже в очереди'}")
        if options["inline"]:
            claimed = claim_job(job)
            if claimed is not None:
                execute_job(claimed)
        deadline = time.monotonic() + options["wait"]
        while True:
            job.refresh_from_db()
            if job.status not in (AIJob.Status.QUEUED, AIJob.Status.RUNNING):
                break
            if time.monotonic() >= deadline:
                raise CommandError(f"Задача {job.pk} ещё не выполнена (статус {job.status}). Проверьте, что AI-воркер запущен, и повторите команду.")
            time.sleep(2)
        if job.status != AIJob.Status.SUCCEEDED:
            raise CommandError(f"Проверка не выполнена: {job.error_code} {job.result_reference.get('message', '')}")
        report = QualityReport.objects.get(pk=job.result_reference["quality_report_id"])
        print_report(self.stdout, report)
        if report.eligibility_state == "eligible":
            self.stdout.write(self.style.SUCCESS("Отчёт допускает публикацию (publish_text)."))
        else:
            self.stdout.write(self.style.WARNING("Отчёт не допускает публикацию. Текст не изменён; после правки файла подайте его заново и повторите проверку."))
