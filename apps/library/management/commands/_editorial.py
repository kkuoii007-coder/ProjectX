"""Shared helpers for the editorial commands (submit/review/publish)."""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError


def read_json(path):
    path = Path(path)
    if not path.is_file():
        raise CommandError(f"Файл не найден: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"Не удалось прочитать {path}: {exc}") from exc


def cards_dir(option):
    return Path(option) if option else Path(settings.BASE_DIR) / "content" / "cards"


def submitted_revision(data):
    from apps.library.editorial import find_submitted_revision

    revision = find_submitted_revision(data)
    if revision is None:
        raise CommandError("Этот текст ещё не подан или файл изменён после подачи: сначала выполните submit_text")
    return revision


def print_report(stdout, report):
    """Reviewer remarks in Russian, as stored in the quality report."""
    review = report.review_json or {}
    stdout.write(f"Отчёт {report.pk}: verdict={review.get('verdict')}, factual_state={report.factual_state}, модель={report.review_model or 'нет (тестовый провайдер)'}")
    for warning in (report.deterministic_json or {}).get("warnings_ru", []):
        stdout.write(f"  [длина, не блокирует] {warning}")
    for issue in review.get("issues", []):
        mark = "БЛОКИРУЕТ" if issue.get("severity") == "blocking" else "замечание"
        where = " ".join(str(issue[k]) for k in ("part_key", "sentence_key") if issue.get(k))
        stdout.write(f"  [{mark}] {where + ': ' if where else ''}{issue.get('reason_ru', '')}")
        if issue.get("proposed_fix_ru"):
            stdout.write(f"      предложение: {issue['proposed_fix_ru']}")
    for claim in review.get("unsupported_claims", []):
        stdout.write(f"  [нет опоры] {claim.get('claim_text', '')} — {claim.get('reason', '')}")
    for key, label in (("translation_issues", "перевод"), ("language_notes", "язык"), ("uncertainty_notes", "сомнение")):
        for note in review.get(key, []):
            stdout.write(f"  [{label}] {note}")


def resolve_admin(login):
    """The administrator on whose behalf the gate is run and audited."""
    from django.contrib.auth import get_user_model

    admins = get_user_model().objects.filter(is_superuser=True, is_active=True)
    if login:
        admin = admins.filter(login=login).first()
        if admin is None:
            raise CommandError(f"Нет активного администратора с логином {login}")
        return admin
    if admins.count() != 1:
        raise CommandError("Укажите --admin <логин>: активных администраторов не один")
    return admins.first()
