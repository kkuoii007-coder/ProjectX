"""A reader's question about a book on the shelf: the one way in.

The form of the workshop and the control run (`shelf_experiment`) both call
`open_question`, so the run asks its questions exactly as the site does.
"""

import uuid

from django.conf import settings
from django.db import transaction

from apps.ai.models import AIJob
from apps.ai.services import create_job
from apps.sources.shelf import shelf_books

from .models import Project

EMPTY_SHELF_MESSAGE = "На полке пока нет книг: задать вопрос о книге сейчас нельзя."


class QuestionRefused(Exception):
    """The question is refused before a project, a job or a reservation."""

    status = 400


class QuestionEmpty(QuestionRefused):
    status = 200  # the form is shown again, as before


class QuestionTooLong(QuestionRefused):
    pass


class ShelfEmpty(QuestionRefused):
    status = 409


def normalize_question(raw: str) -> str:
    """A browser counts a line break in a textarea as one character and
    sends two; the limit counts what the reader saw."""
    return (raw or "").replace("\r\n", "\n").strip()


def open_question(*, owner, question: str, mode=Project.Mode.EXPLAIN):
    """Create the project and its `generate` job for a question.

    Returns (project, job). Raises QuestionRefused for a question that
    cannot be asked and the errors of `create_job` (InsufficientCredits,
    JobConflict, QueueFull) for one that cannot be queued; in both cases
    nothing is created. The whole question goes to the job; the title of
    the project is its first 200 characters. A question longer than
    MAX_QUESTION_CHARS is refused, never cut.
    """
    question = normalize_question(question)
    if not question:
        raise QuestionEmpty("Введите вопрос о книге")
    if len(question) > settings.MAX_QUESTION_CHARS:
        raise QuestionTooLong(
            f"Вопрос слишком длинный: {len(question)} символов при пределе {settings.MAX_QUESTION_CHARS}. Сократите его."
        )
    if not shelf_books():
        raise ShelfEmpty(EMPTY_SHELF_MESSAGE)
    with transaction.atomic():
        project = Project.objects.create(owner=owner, title=question[:200], mode=mode)
        job, _ = create_job(
            owner=owner,
            kind=AIJob.Kind.GENERATE,
            idempotency_key=uuid.uuid4().hex,
            instruction=question,
            project=project,
        )
    return project, job
