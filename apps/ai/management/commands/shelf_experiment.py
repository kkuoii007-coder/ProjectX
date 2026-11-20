"""The control run of «answer with a source»: ten questions, one report.

Asks the workshop the owner's ten questions without changes, the way the
site does (apps.workshop.asking.open_question -> the worker), and writes a
report for a human to read. The command makes no decision: the rule for it
is written into the header of the report before the first question.

By default the command only queues each question and waits until a running
worker (`run_ai_worker`) has finished it. `--inline` executes the job inside
the command: for a local run with the fake provider, or on a server where
the worker is stopped. Do not use `--inline` while a worker is running.

With AI_PROVIDER=openrouter every answered question is a paid call of the
owner's budget; with the fake provider nothing is paid.
"""

import os
import subprocess
import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.ai.models import AIJob
from apps.ai.services import JobError, claim_job, execute_job
from apps.credits.models import Wallet
from apps.library.editorial import EditorialError, load_cefr_list, profile_vocabulary
from apps.sources.shelf import shelf_books
from apps.workshop.asking import QuestionRefused, open_question
from apps.workshop.models import Message

# The owner's questions, word for word. 1-8 are answered by the book on the
# shelf (Aristotle, Politics); 9 and 10 are not and must be refused.
QUESTIONS = (
    "Почему Аристотель называет человека политическим животным?",
    "Чем, по Аристотелю, государство отличается от семьи?",
    "Что Аристотель говорит о рабстве «по природе»?",
    "Кого Аристотель считает гражданином?",
    "Какие виды государственного устройства он различает и какие считает неправильными?",
    "Почему Аристотель критикует общность имущества у Платона?",
    "Почему Аристотель считает сильный средний слой полезным для государства?",
    "Зачем, по Аристотелю, государству нужно общее воспитание?",
    "Что такое заброшенность у Хайдеггера?",
    "Что такое категорический императив у Канта?",
)

# Written down before the run; the command does not apply it.
DECISION_RULE = (
    "Продолжаем, если в восьми ответах каждая строка источника подтверждает свою часть, а вопросы 9 и 10 получили отказ без списания. "
    "В третьем вопросе ответ излагает позицию Аристотеля как его позицию: не одобряет её и не смягчает"
)

THRESHOLD_NOTE = (
    "Фрагмент считается опорой, если `vector_raw` ≥ SHELF_MIN_VECTOR_SIMILARITY ИЛИ `fts_coverage` ≥ SHELF_MIN_FTS_COVERAGE; "
    "модели передаются только опоры. Значения по умолчанию осторожно низкие. 0.5 для текстовой половины выбрано по замеру на "
    "полной книге без векторной половины: контрольные вопросы 1–5, 7 и 8 дают лучшее покрытие 1.0, вопросы не по книге — "
    "не выше 0.46. Вопрос 6 по текстовой половине даёт 0.21 (имени Платона в словаре книги нет, и оно весит больше остальных "
    "слов) — он должен проходить по векторной половине; если она недоступна, по нему будет `search_unavailable`, а не ответ. "
    "0.20 для векторной половины не откалибровано — локально настоящих эмбеддингов нет. Порог — первая линия отказа; вторая — "
    "исход `no_source` от самой модели. По строкам «best vector_raw / best fts_coverage» ниже видно, как порог сработал на "
    "каждом вопросе."
)

FINAL = (AIJob.Status.SUCCEEDED, AIJob.Status.FAILED, AIJob.Status.CANCELLED, AIJob.Status.RECONCILIATION)
REFUSAL_LINES = {"threshold": "порог до модели (модель не вызывалась)", "model": "модель вернула no_source"}


def _commit() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "неизвестен"
    return done.stdout.strip() or "неизвестен"


def _number(value, digits=3) -> str:
    return "нет" if value is None else f"{value:.{digits}f}"


class Command(BaseCommand):
    help = "Ask the workshop the ten control questions and write docs/shelf-experiment/report.md (makes no decision)."

    def add_arguments(self, parser):
        parser.add_argument("--login", required=True, help="Учётная запись, от имени которой задаются вопросы")
        parser.add_argument("--inline", action="store_true", help="Выполнить задачи в процессе команды (не использовать при работающем воркере)")
        parser.add_argument("--out", default=os.path.join("docs", "shelf-experiment", "report.md"), help="Куда записать отчёт")
        parser.add_argument("--timeout", type=float, default=600.0, help="Сколько секунд ждать воркер на один вопрос")
        parser.add_argument("--poll", type=float, default=2.0, help="Пауза между проверками задачи, секунд")

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(login=options["login"].lower()).first()
        if user is None:
            raise CommandError(f"Учётная запись «{options['login']}» не найдена")
        books = shelf_books()
        if not books:
            raise CommandError("На полке нет книг: сначала import_shelf_book и index_source")
        cost = settings.CREDIT_COSTS["generate"]
        needed = cost * len(QUESTIONS)
        wallet, _ = Wallet.objects.get_or_create(user=user)
        if wallet.available_credits < needed:
            raise CommandError(
                f"Кредитов не хватает: доступно {wallet.available_credits}, на {len(QUESTIONS)} вопросов нужно {needed} "
                f"({cost} за вопрос; отказы не списываются, но резерв нужен на каждый). Выдайте недостающее: "
                f"python manage.py issue_credits --login {user.login} --amount {needed - wallet.available_credits} "
                f"--operation-key shelf-experiment-{timezone.now():%Y%m%d-%H%M} --reason \"Контрольный прогон shelf_experiment\""
            )
        try:
            cefr_levels = load_cefr_list()
        except EditorialError as exc:
            raise CommandError(str(exc)) from exc

        out = options["out"] if os.path.isabs(options["out"]) else os.path.join(settings.BASE_DIR, options["out"])
        header = self._header(user, books, options)
        records, stopped = [], ""
        for number, question in enumerate(QUESTIONS, start=1):
            self.stdout.write(f"Вопрос {number}/{len(QUESTIONS)}: {question}")
            try:
                record = self._ask(user, number, question, options)
            except (QuestionRefused, JobError) as exc:
                stopped = f"Вопрос {number} не удалось задать: {exc}"
                break
            records.append(record)
            self.stdout.write(f"  -> {record['headline']}")
            if record["timed_out"]:
                stopped = (
                    f"Вопрос {number}: задача {record['job'].pk} не завершилась за {options['timeout']:g} с. "
                    "Проверьте, что воркер (run_ai_worker) работает, или запустите команду с --inline при остановленном воркере."
                )
                break
        if records:
            self._write(out, header, records, cefr_levels, stopped)
            self.stdout.write(f"Отчёт: {out}")
        if stopped:
            raise CommandError(stopped + (f" Неполный отчёт: {out}" if records else ""))
        self.stdout.write("Команда решения не выносит: отчёт читает человек.")

    # --- one question ---------------------------------------------------------------

    def _ask(self, user, number, question, options) -> dict:
        before = Wallet.objects.get(user=user)
        started = time.monotonic()
        project, job = open_question(owner=user, question=question)
        if options["inline"]:
            claimed = claim_job(job)
            if claimed is None:
                raise CommandError(f"Задачу {job.pk} уже взял воркер: --inline нельзя использовать при работающем воркере")
            execute_job(claimed)
        deadline = started + options["timeout"]
        job.refresh_from_db()
        while job.status not in FINAL and time.monotonic() < deadline:
            time.sleep(options["poll"])
            job.refresh_from_db()
        elapsed = time.monotonic() - started
        after = Wallet.objects.get(user=user)
        project.refresh_from_db()
        record = {
            "number": number,
            "question": question,
            "project": project,
            "job": job,
            "timed_out": job.status not in FINAL,
            "elapsed": elapsed,
            "before": before.total_credits,
            "after": after.total_credits,
            "reserved_after": after.reserved_credits,
            "attempts": list(job.provider_attempts.order_by("stage_number")),
            "search": (job.result_reference or {}).get("search") or {},
        }
        record["headline"] = self._outcome_line(record)
        return record

    @staticmethod
    def _outcome_line(record) -> str:
        job = record["job"]
        if record["timed_out"]:
            return f"нет (задача `{job.status}`: не завершилась за время ожидания)"
        if job.outcome:
            line = f"`{job.outcome}`"
            if job.error_code:
                line += f", код `{job.error_code}`"
            refusal = (job.result_reference or {}).get("refusal_line")
            if refusal:
                line += f"; линия отказа: {REFUSAL_LINES.get(refusal, refusal)}"
            return line
        return f"нет (задача `{job.status}`, код `{job.error_code or 'нет'}`)"

    # --- the report -----------------------------------------------------------------

    def _header(self, user, books, options) -> list:
        lines = [
            "# Контрольный прогон «Ответ с источником»",
            "",
            f"- Дата: {timezone.now():%Y-%m-%d %H:%M} UTC",
            f"- Commit: {_commit()}",
            f"- Провайдер: {settings.AI_PROVIDER}" + (" (поддельный: ответы не от живой модели, расход нулевой)" if settings.AI_PROVIDER == "fake" else ""),
            f"- Модели: генерация `{settings.AI_GENERATION_MODEL}`, проверка `{settings.AI_REVIEW_MODEL}`, эмбеддинги `{settings.AI_EMBEDDING_MODEL}`",
            f"- Учётная запись: {user.login}",
            f"- Выполнение: {'в процессе команды (--inline)' if options['inline'] else 'работающим воркером'}",
            f"- Пороги опоры: SHELF_MIN_VECTOR_SIMILARITY = {settings.SHELF_MIN_VECTOR_SIMILARITY:g}, SHELF_MIN_FTS_COVERAGE = {settings.SHELF_MIN_FTS_COVERAGE:g}",
            f"- О порогах: {THRESHOLD_NOTE}",
            f"- Цена вопроса: {settings.CREDIT_COSTS['generate']} кредитов; предел вызовов модели на задачу: {settings.AI_MAX_PROVIDER_CALLS_PER_JOB}",
            "- Книги на полке:",
        ]
        lines += [f"  - {book.attribution}" for book in books]
        lines += [
            "",
            "## Правило решения (записано до прогона)",
            "",
            f"> {DECISION_RULE}.",
            "",
            "Команда решения не выносит: она только задаёт вопросы и записывает, что получилось. Подтверждает ли строка источника "
            "свою часть и как изложена позиция Аристотеля в третьем вопросе, решает человек, читая ответы и отрывки ниже.",
            "",
            "Как читать `vector_available`: «да» — обе половины поиска работали, отказ `no_source` означает «на полке нет опоры». "
            "«НЕТ» — эмбеддинг вопроса получить не удалось; тогда задача без текстовой опоры завершается ошибкой `search_unavailable`, "
            "и это не отказ «источника нет»: по такому вопросу прогон надо повторить, а не засчитывать.",
            "",
        ]
        return lines

    def _write(self, out, header, records, cefr_levels, stopped):
        lines = list(header)
        lines += [
            "## Сводка",
            "",
            "| № | Исход | vector_available | best vector_raw | best fts_coverage | Опор | Вызовов модели | USD | Кредитов списано | Время, с |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for record in records:
            search = record["search"]
            lines.append(
                f"| {record['number']} | {record['headline']} | {self._vector_word(search)} | {_number(search.get('best_vector_raw'))} | "
                f"{_number(search.get('best_fts_coverage'))} | {search.get('supports', 'нет')} | {len(record['attempts'])} | "
                f"{self._usd(record['attempts'])} | {record['before'] - record['after']} | {record['elapsed']:.1f} |"
            )
        total_usd = sum((attempt.cost_usd or 0) for record in records for attempt in record["attempts"])
        lines += [
            "",
            f"Всего: расход владельца {total_usd:.6f} USD, списано кредитов {records[0]['before'] - records[-1]['after']}, "
            f"время {sum(record['elapsed'] for record in records):.1f} с.",
            "",
        ]
        if stopped:
            lines += [f"**Прогон остановлен, отчёт неполный.** {stopped}", ""]
        for record in records:
            lines += self._section(record, cefr_levels)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines).rstrip("\n") + "\n")

    @staticmethod
    def _vector_word(search) -> str:
        if "vector_available" not in search:
            return "нет данных"
        return "да" if search["vector_available"] else "НЕТ"

    @staticmethod
    def _usd(attempts) -> str:
        costs = [attempt.cost_usd for attempt in attempts if attempt.cost_usd is not None]
        return f"{sum(costs):.6f}" if costs else "0"

    def _section(self, record, cefr_levels) -> list:
        job, search = record["job"], record["search"]
        lines = [f"## Вопрос {record['number']}", "", f"Вопрос: {record['question']}", "", f"- Исход: {record['headline']}"]
        vector = self._vector_word(search)
        if search:
            line = (
                f"- Поиск: vector_available: {vector}; найдено фрагментов {search.get('found')}, из них опор {search.get('supports')}; "
                f"best vector_raw {_number(search.get('best_vector_raw'))} (порог {search.get('min_vector_similarity'):g}); "
                f"best fts_coverage {_number(search.get('best_fts_coverage'))} (порог {search.get('min_fts_coverage'):g}); "
                f"поиск занял {search.get('search_ms', 0):.0f} мс"
            )
            if not search.get("vector_available"):
                line += f"; ошибка эмбеддинга: `{search.get('vector_error') or 'нет кода'}`"
            lines.append(line)
            unknown = [*(search.get("missing_terms") or []), *(search.get("unmapped_terms") or [])]
            if unknown:
                lines.append(
                    f"- Слова вопроса, которых текстовая половина не нашла на полке (снижают fts_coverage): {', '.join(unknown)}"
                )
        else:
            lines.append("- Поиск: нет данных (задача не дошла до поиска)")
        if job.error_code == "search_unavailable":
            lines.append("- Внимание: поиск был недоступен — это не отказ «источника нет». Вопрос не засчитывается, прогон по нему надо повторить.")
        lines.append(f"- Время: {record['elapsed']:.1f} с")
        if record["attempts"]:
            calls = "; ".join(
                f"{attempt.model}: {_number(attempt.cost_usd, 6)} USD, токены {(attempt.usage_json or {}).get('prompt_tokens', '?')}"
                f"/{(attempt.usage_json or {}).get('completion_tokens', '?')}"
                for attempt in record["attempts"]
            )
            lines.append(f"- Расход владельца: {self._usd(record['attempts'])} USD, вызовов модели {len(record['attempts'])} ({calls})")
        else:
            lines.append("- Расход владельца: 0 USD, вызовов модели 0")
        lines.append(
            f"- Кредиты: списано {record['before'] - record['after']} (баланс до {record['before']}, после {record['after']}; "
            f"в резерве после {record['reserved_after']})"
        )
        message = (job.result_reference or {}).get("message")
        if message:
            lines.append(f"- Сообщение об ошибке: {message}")
        for violation in (job.result_reference or {}).get("violations") or []:
            lines.append(f"- Нарушение проверки источников: {violation}")
        explanation = Message.objects.filter(project=record["project"], role="assistant", kind="explanation", job=job).order_by("-created_at").first()
        if explanation:
            lines.append(f"- Объяснение читателю: {explanation.body}")

        revision = record["project"].head_revision
        if revision is None:
            return lines + ["", "Текст не создан.", ""]
        payload = revision.payload
        profile = profile_vocabulary(payload, cefr_levels=cefr_levels)
        shares = ", ".join(f"{level} {profile['share'][level]:.1f} %" for level in profile["levels"] if profile["levels"][level])
        above = ", ".join(
            f"{word} ({info['level'] or 'нет в списке'})" for word, info in sorted(profile["above_a2"].items())
        )
        lines.append(f"- Словарный профиль: слов {profile['total_words']}; {shares or 'нет слов'}; слова выше A2: {above or 'нет'}")
        if payload.get("note"):
            lines.append(f"- Заметка проверки (видна читателю): {payload['note']}")
        lines += ["", f"### Ответ: {payload.get('title_en', '')} / {payload.get('title_ru', '')}", ""]
        if payload.get("based_on"):
            lines += [f"AI draft. Based on: {'; '.join(payload['based_on'])}.", ""]
        for index, part in enumerate(payload.get("parts", []), start=1):
            lines += [f"#### Часть {index}. {part.get('title_en', '')}", ""]
            for position, sentence in enumerate(part.get("sentences", []), start=1):
                lines += [f"{position}. {sentence.get('en', '')}", f"   {sentence.get('ru', '')}"]
            lines.append("")
            for source in part.get("sources") or []:
                lines += [f"- Source: {source.get('label', '')} [{source.get('citation_id', '')}]", f"  > {source.get('excerpt', '')}"]
            lines.append("")
        for edition in payload.get("source_editions") or []:
            lines += [f"Издание: {edition.get('attribution', '')}", ""]
        return lines
