import uuid

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.ai.models import AIJob
from apps.ai.services import InsufficientCredits, JobConflict, JobError, QueueFull, create_job

from .models import Message, Project, Revision


@require_http_methods(["GET", "POST"])
@login_required
def create_project(request):
    if request.method == "POST":
        mode = request.POST.get("mode", Project.Mode.EXPLAIN)
        if mode not in Project.Mode.values:
            return redirect("library:home")
        title = request.POST.get("topic", "").strip()[:200]
        if not title or len(title) > 2000:
            return render(request, "workshop/create.html", {"error": "Введите тему запроса"})
        try:
            with transaction.atomic():
                project = Project.objects.create(owner=request.user, title=title, mode=mode)
                job, _ = create_job(
                    owner=request.user,
                    kind=AIJob.Kind.GENERATE,
                    idempotency_key=uuid.uuid4().hex,
                    instruction=title,
                    project=project,
                )
        except InsufficientCredits:
            # Атомарный блок уже откатился: проект не создан. Рендерим вне транзакции,
            # иначе контекст-процессор упадёт на запросе в прерванной транзакции.
            return render(request, "workshop/create.html", {"error": "Недостаточно кредитов для этого действия", "topic": title}, status=402)
        except (JobConflict, QueueFull, JobError) as exc:
            return render(request, "workshop/create.html", {"error": str(exc), "topic": title}, status=409)
        return redirect("workshop:project", project_id=project.pk)
    return render(request, "workshop/create.html")


@require_GET
@login_required
def project_detail(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    messages = Message.objects.filter(project=project, owner=request.user).select_related("job")
    revision = project.head_revision
    revisions = project.revisions.order_by("-created_at")[:20]
    return render(
        request,
        "workshop/project.html",
        {
            "project": project,
            "messages_list": messages,
            "revision": revision,
            "revisions": revisions,
            "parts": revision.payload.get("parts", []) if revision else [],
        },
    )


@require_POST
@login_required
def ask(request, project_id):
    project = get_object_or_404(Project, pk=project_id, owner=request.user)
    instruction = request.POST.get("instruction", "").strip()
    if not instruction or len(instruction) > 2000:
        return render(request, "workshop/project.html", {"project": project, "error": "Введите вопрос до 2000 символов"}, status=400)
    key = request.POST.get("idempotency_key", "").strip() or uuid.uuid4().hex
    try:
        create_job(owner=request.user, kind=AIJob.Kind.ASK, idempotency_key=key, instruction=instruction, project=project, base_revision=project.head_revision)
    except InsufficientCredits as exc:
        return render(request, "workshop/project.html", {"project": project, "error": str(exc), "instruction": instruction}, status=402)
    except (JobConflict, QueueFull, JobError) as exc:
        return render(request, "workshop/project.html", {"project": project, "error": str(exc), "instruction": instruction}, status=409)
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
        return render(request, "workshop/project.html", {"project": project, "error": "Введите инструкцию до 1000 символов"}, status=400)
    # Bare part index for edit_part, "part:sentence" for edit_sentence.
    try:
        if kind == AIJob.Kind.EDIT_PART:
            part_idx, sentence_idx = int(target), None
        else:
            part_idx, sentence_idx = (int(piece) for piece in target.split(":", 1))
    except ValueError:
        return render(request, "workshop/project.html", {"project": project, "error": "Некорректная цель правки"}, status=400)
    base = project.head_revision
    if base is None:
        return render(request, "workshop/project.html", {"project": project, "error": "Нечего править: ревизии ещё нет"}, status=400)
    parts = base.payload.get("parts", [])
    if not 0 <= part_idx < len(parts):
        return render(request, "workshop/project.html", {"project": project, "error": "Некорректная цель правки"}, status=400)
    if kind == AIJob.Kind.EDIT_SENTENCE and not 0 <= sentence_idx < len(parts[part_idx].get("sentences", [])):
        return render(request, "workshop/project.html", {"project": project, "error": "Некорректная цель правки"}, status=400)
    key = request.POST.get("idempotency_key", "").strip() or uuid.uuid4().hex
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
        return render(request, "workshop/project.html", {"project": project, "error": str(exc)}, status=402)
    except (JobConflict, QueueFull, JobError) as exc:
        return render(request, "workshop/project.html", {"project": project, "error": str(exc)}, status=409)
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
        return render(request, "workshop/project.html", {"project": project, "error": "Не указана ревизия"}, status=400)
    try:
        candidate = Revision.objects.get(pk=revision_id, project=project)
    except (Revision.DoesNotExist, ValueError, TypeError):
        return render(request, "workshop/project.html", {"project": project, "error": "Ревизия не найдена"}, status=404)
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
