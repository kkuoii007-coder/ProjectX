"""A reader's question about a book on the shelf: the one way in.

The form of the workshop and the control run (`shelf_experiment`) both call
`open_question`, so the run asks its questions exactly as the site does.
"""

import unicodedata
import uuid

from django.conf import settings
from django.db import transaction

from apps.ai.models import AIJob
from apps.ai.services import create_job
from apps.sources.shelf import shelf_books

from .models import Project

TITLE_CHARS = 200  # Project.title; the whole question lives in the job
EMPTY_SHELF_MESSAGE = "На полке пока нет книг: задать вопрос о книге сейчас нельзя."


class QuestionRefused(Exception):
    """The question is refused before a project, a job or a reservation."""

    status = 400


class QuestionEmpty(QuestionRefused):
    status = 200  # the form is shown again, as before


class QuestionTooLong(QuestionRefused):
    pass


class QuestionUnreadable(QuestionRefused):
    """NUL and other control characters: PostgreSQL refuses NUL in text, and
    none of them is something a reader types."""


class ShelfEmpty(QuestionRefused):
    status = 409


# The same refusal for an edit instruction and a question about a text (views.py).
CONTROL_CHARACTERS_MESSAGE = "В тексте есть служебные символы, которые нельзя сохранить. Уберите их и отправьте ещё раз."


def has_control_characters(text: str) -> bool:
    """Control characters (Unicode category Cc) other than a line break
    and a tab. A NUL byte is one of them."""
    return any(unicodedata.category(char) == "Cc" and char not in "\n\t" for char in text or "")


def without_control_characters(text: str) -> str:
    """For showing a refused text again in the form."""
    return "".join(char for char in text or "" if unicodedata.category(char) != "Cc" or char in "\n\t")


def normalize_question(raw: str) -> str:
    """A browser counts a line break in a textarea as one character and
    sends two; the limit counts what the reader saw."""
    return (raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()


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
    if has_control_characters(question):
        raise QuestionUnreadable("В вопросе есть служебные символы. Уберите их и отправьте вопрос ещё раз.")
    if len(question) > settings.MAX_QUESTION_CHARS:
        raise QuestionTooLong(
            f"Вопрос слишком длинный: {len(question)} символов при пределе {settings.MAX_QUESTION_CHARS}. Сократите его."
        )
    if not shelf_books():
        raise ShelfEmpty(EMPTY_SHELF_MESSAGE)
    with transaction.atomic():
        project = Project.objects.create(owner=owner, title=question[:TITLE_CHARS], mode=mode)
        job, _ = create_job(
            owner=owner,
            kind=AIJob.Kind.GENERATE,
            idempotency_key=uuid.uuid4().hex,
            instruction=question,
            project=project,
        )
    return project, job
