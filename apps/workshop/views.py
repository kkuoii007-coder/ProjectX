import uuid

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.ai import grounding
from apps.ai.grounding import has_sources
from apps.ai.models import AIJob
from apps.ai.services import EDIT_NOT_SUPPORTED_MESSAGE, InsufficientCredits, JobConflict, JobError, QueueFull, create_job
from apps.credits.models import CreditReservation
from apps.sources.shelf import shelf_books

from .asking import CONTROL_CHARACTERS_MESSAGE, QuestionRefused, has_control_characters, normalize_question, open_question, without_control_characters
from .models import Message, Project, Revision


# Presentation-only examples of a question in the workshop entry form. The
# books themselves come from data (apps.sources.shelf.shelf_books) and are
# listed above the field; the examples are shown only when the shelf is not
# empty and must be answerable from it.
TOPIC_EXAMPLES = (
    "Почему Аристотель называет человека политическим животным?",
    "Кого Аристотель считает гражданином?",
    "Какие виды государственного устройства различает Аристотель?",
)


def _create_page(request, status=200, **extra):
    """The workshop entry page (the form used to live on the home page)."""
    return render(
        request,
        "workshop/create.html",
        {
            "costs": settings.CREDIT_COSTS,
            "placeholder_examples": TOPIC_EXAMPLES,
            "shelf_books": shelf_books(),
            "max_question_chars": settings.MAX_QUESTION_CHARS,
            **extra,
        },
        status=status,
    )


@require_http_methods(["GET", "POST"])
@login_required
def create_project(request):
    if request.method == "POST":
        mode = request.POST.get("mode", Project.Mode.EXPLAIN)
        if mode not in Project.Mode.values:
            return redirect("library:home")
        raw = request.POST.get("topic", "")
        try:
            project, _ = open_question(owner=request.user, question=raw, mode=mode)
        except QuestionRefused as exc:
            # Refused before a project, a job or a reservation; a question
            # that is too long is not cut: the reader keeps what he typed.
            return _create_page(request, status=exc.status, error=str(exc), topic=without_control_characters(normalize_question(raw)))
        except InsufficientCredits:
            # Атомарный блок уже откатился: проект не создан. Рендерим вне транзакции,
            # иначе контекст-процессор упадёт на запросе в прерванной транзакции.
            return _create_page(request, status=402, error="Недостаточно кредитов для этого действия", topic=normalize_question(raw))
        except (JobConflict, QueueFull, JobError) as exc:
            return _create_page(request, status=409, error=str(exc), topic=normalize_question(raw))
        return redirect("workshop:project", project_id=project.pk)
    return _create_page(request)


@require_GET
@login_required
def project_detail(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    return _project_page(request, project)


def _snapshot_links(edition) -> list:
    """(label, url) pairs stored in a revision; only web addresses are shown."""
    pairs = []
    for item in edition.get("links") or []:
        if isinstance(item, (list, tuple)) and len(item) == 2 and str(item[1]).startswith(("https://", "http://")):
            pairs.append((str(item[0]), str(item[1])))
    return pairs


def _parts_with_sources(payload):
    """Parts of a revision for the page; every source line also gets the
    attribution of its edition and its links (digital publisher, source
    file, licence). Everything comes from the stored snapshot, not from
    the shelf of today."""
    editions = {
        edition.get("book"): {"attribution": edition.get("attribution", ""), "links": _snapshot_links(edition)}
        for edition in payload.get("source_editions") or []
        if isinstance(edition, dict)
    }
    nothing = {"attribution": "", "links": []}
    return [
        {**part, "sources": [{**source, **editions.get(source.get("book"), nothing)} for source in part.get("sources") or []]}
        for part in payload.get("parts", [])
    ]


def _creation_job(project):
    """The latest job that was asked to make the text of the project."""
    return project.jobs.filter(kind=AIJob.Kind.GENERATE).order_by("-created_at").first()


def _no_text_state(job):
    """Why a project has no text, from its `generate` job: `waiting`,
    `no_source`, `search_unavailable`, `source_validation_failed`,
    `explained` (an old job where the model asked to clarify or refused;
    its words are not shown) or `failed`."""
    if job is None or job.status in (AIJob.Status.QUEUED, AIJob.Status.RUNNING):
        return "waiting"
    if job.outcome == AIJob.Outcome.NO_SOURCE:
        return "no_source"
    if job.error_code in ("search_unavailable", "source_validation_failed"):
        return job.error_code
    if job.outcome in (AIJob.Outcome.CLARIFICATION, AIJob.Outcome.REFUSED):
        return "explained"
    return "failed"


# The second line of a final screen without a text: a fixed server text
# chosen by the state of the job. Nothing the model wrote is shown.
REFUSAL_LINES = {
    "source_validation_failed": grounding.VALIDATION_FAILED_MESSAGE,
    "unexpected_model_outcome": grounding.UNEXPECTED_OUTCOME_MESSAGE,
}


def _nothing_was_charged(job) -> bool:
    """True when the job is over and its reservation was given back (or
    was never made): only then the page may say «No credits were taken»."""
    if job is None or job.status in (AIJob.Status.QUEUED, AIJob.Status.RUNNING, AIJob.Status.RECONCILIATION):
        return False
    reservation = CreditReservation.objects.filter(job=job).first()
    return reservation is None or reservation.state == CreditReservation.State.RELEASED


def _project_context(request, project):
    revision = project.head_revision
    payload = revision.payload if revision else {}
    sourced = has_sources(payload)
    job = _creation_job(project)
    state = "text" if revision else _no_text_state(job)
    # The reader's question in full (the title of the project may be cut).
    # A text made before «answer with a source» has a topic, not a question.
    question = (job.input_json.get("instruction") or "") if job is not None and (sourced or revision is None) else ""
    context = {
        "project": project,
        "messages_list": Message.objects.filter(project=project, owner=request.user).select_related("job"),
        "revision": revision,
        "revisions": project.revisions.order_by("-created_at")[:20],
        "parts": _parts_with_sources(payload),
        # An answer with sources: no edit forms (see edit_fragment).
        "sourced": sourced,
        "source_editions": payload.get("source_editions") or [],
        "state": state,
        "question": question,
    }
    if state not in ("text", "waiting"):
        # A final screen without a text: no versions, no discussion.
        context["shelf_books"] = shelf_books()
        context["no_charge"] = _nothing_was_charged(job)
        context["refusal_line"] = REFUSAL_LINES.get(job.error_code if job is not None else "", "")
    return context


def _project_page(request, project, status=200, **extra):
    """The page of a project, also when an action on it was refused: the
    text, its versions and the discussion stay where they were."""
    return render(request, "workshop/project.html", {**_project_context(request, project), **extra}, status=status)


@require_POST
@login_required
def ask(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    if not settings.ASK_ENABLED:
        # Switched off: a direct POST gets the same page with the text and
        # no job, reservation or message is created.
        return _project_page(request, project, status=403)
    instruction = request.POST.get("instruction", "").strip()
    if not instruction or len(instruction) > 2000:
        return _project_page(request, project, status=400, error="Введите вопрос до 2000 символов")
    key = request.POST.get("idempotency_key", "").strip() or uuid.uuid4().hex
    if has_control_characters(instruction) or has_control_characters(key):
        return _project_page(request, project, status=400, error=CONTROL_CHARACTERS_MESSAGE)
    try:
        create_job(owner=request.user, kind=AIJob.Kind.ASK, idempotency_key=key, instruction=instruction, project=project, base_revision=project.head_revision)
    except InsufficientCredits as exc:
        return _project_page(request, project, status=402, error=str(exc), instruction=instruction)
    except (JobConflict, QueueFull, JobError) as exc:
        return _project_page(request, project, status=409, error=str(exc), instruction=instruction)
    return redirect("workshop:project", project_id=project.pk)


@require_POST
@login_required
def edit_fragment(request, project_id):
    """Start an edit job for a sentence or part target ("part:sentence")."""
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    instruction = request.POST.get("instruction", "").strip()
    target = request.POST.get("target", "").strip()
    kind = request.POST.get("kind", "").strip()
    if kind not in (AIJob.Kind.EDIT_SENTENCE, AIJob.Kind.EDIT_PART):
        return redirect("workshop:project", project_id=project.pk)
    if not instruction or len(instruction) > 1000:
        return _project_page(request, project, status=400, error="Введите инструкцию до 1000 символов")
    # Bare part index for edit_part, "part:sentence" for edit_sentence.
    try:
        if kind == AIJob.Kind.EDIT_PART:
            part_idx, sentence_idx = int(target), None
        else:
            part_idx, sentence_idx = (int(piece) for piece in target.split(":", 1))
    except ValueError:
        return _project_page(request, project, status=400, error="Некорректная цель правки")
    base = project.head_revision
    if base is None:
        return _project_page(request, project, status=400, error="Нечего править: ревизии ещё нет")
    if has_sources(base.payload):
        # Refused here and in create_job: no job, no reservation.
        return _project_page(request, project, status=409, error=EDIT_NOT_SUPPORTED_MESSAGE)
    parts = base.payload.get("parts", [])
    if not 0 <= part_idx < len(parts):
        return _project_page(request, project, status=400, error="Некорректная цель правки")
    if kind == AIJob.Kind.EDIT_SENTENCE and not 0 <= sentence_idx < len(parts[part_idx].get("sentences", [])):
        return _project_page(request, project, status=400, error="Некорректная цель правки")
    key = request.POST.get("idempotency_key", "").strip() or uuid.uuid4().hex
    if has_control_characters(instruction) or has_control_characters(key):
        return _project_page(request, project, status=400, error=CONTROL_CHARACTERS_MESSAGE)
    try:
        create_job(
            owner=request.user,
            kind=kind,
            idempotency_key=key,
            instruction=instruction,
            project=project,
            base_revision=base,
            target_id=target,
        )
    except InsufficientCredits as exc:
        return _project_page(request, project, status=402, error=str(exc))
    except (JobConflict, QueueFull, JobError) as exc:
        return _project_page(request, project, status=409, error=str(exc))
    return redirect("workshop:project", project_id=project.pk)


@require_GET
@login_required
def revision_diff(request, project_id, revision_id):
    """Server-side compare of a candidate revision against the head."""
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    try:
        candidate = Revision.objects.get(pk=revision_id, project=project)
    except (Revision.DoesNotExist, ValueError, TypeError):
        return JsonResponse({"error": "Ревизия не найдена"}, status=404)
    head = project.head_revision
    diff = []
    if head is not None:
        head_parts = head.payload.get("parts", [])
        cand_parts = candidate.payload.get("parts", [])
        for part_idx, (head_part, cand_part) in enumerate(zip(head_parts, cand_parts)):
            head_sents = head_part.get("sentences", [])
            cand_sents = cand_part.get("sentences", [])
            for sent_idx in range(max(len(head_sents), len(cand_sents))):
                old = head_sents[sent_idx] if sent_idx < len(head_sents) else None
                new = cand_sents[sent_idx] if sent_idx < len(cand_sents) else None
                if old != new:
                    diff.append({"part": part_idx, "sentence": sent_idx, "old": old, "new": new})
        if head.payload.get("title_ru") != candidate.payload.get("title_ru"):
            diff.insert(0, {"part": -1, "sentence": -1, "old": head.payload.get("title_ru"), "new": candidate.payload.get("title_ru")})
    return JsonResponse({"diff": diff, "same_hash": head.content_hash == candidate.content_hash if head else False})


@require_POST
@login_required
def apply_head(request, project_id):
    """Explicitly apply a stored revision as the project head (spec: head
    changes only after "apply"; a repeat of an already-applied revision is
    a no-op, not a rollback)."""
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    revision_id = request.POST.get("revision_id", "").strip()
    if not revision_id:
        return _project_page(request, project, status=400, error="Не указана ревизия")
    try:
        candidate = Revision.objects.get(pk=revision_id, project=project)
    except (Revision.DoesNotExist, ValueError, TypeError):
        return _project_page(request, project, status=404, error="Ревизия не найдена")
    if project.head_revision_id == candidate.pk:
        return redirect("workshop:project", project_id=project.pk)
    project.head_revision = candidate
    project.save(update_fields=["head_revision"])
    return redirect("workshop:project", project_id=project.pk)


@require_GET
@login_required
def job_status(request, job_id):
    job = get_object_or_404(AIJob, pk=job_id, owner=request.user)
    return JsonResponse({"status": job.status, "stage": job.stage, "outcome": job.outcome, "error": job.error_code})
