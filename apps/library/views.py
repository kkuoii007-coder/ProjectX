from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from apps.operations.models import Report

from .models import Collection, Publication, Thinker

REPORT_CATEGORIES = {
    "factual_error": "Фактическая ошибка",
    "too_complex": "Слишком сложно",
    "bad_translation": "Плохой перевод",
    "inappropriate": "Неприемлемый контент",
    "rights": "Права на текст",
    "other": "Другое",
}

REPORTS_PER_USER_PER_DAY = 3


@require_GET
def home(request):
    publications = Publication.objects.filter(status=Publication.Status.PUBLISHED).select_related("revision", "thinker")[:6]
    collections = Collection.objects.prefetch_related("publications")[:5]
    return render(request, "library/home.html", {"publications": publications, "collections": collections})


@require_GET
def library(request):
    query = request.GET.get("q", "").strip()[:200]
    publications = Publication.objects.filter(status=Publication.Status.PUBLISHED).select_related("revision", "thinker")
    if query:
        publications = publications.filter(Q(slug__icontains=query) | Q(revision__payload__title_ru__icontains=query) | Q(thinker__name_ru__icontains=query))
    return render(request, "library/library.html", {"publications": publications, "query": query})


@require_GET
def read(request, slug):
    publication = get_object_or_404(Publication.objects.select_related("revision", "thinker"), slug=slug, status=Publication.Status.PUBLISHED)
    return render(request, "library/read.html", {"publication": publication, "parts": publication.revision.payload.get("parts", [])})


@require_GET
def thinker(request, slug):
    thinker = get_object_or_404(Thinker, slug=slug)
    publications = Publication.objects.filter(thinker=thinker, status=Publication.Status.PUBLISHED).select_related("revision")
    return render(request, "library/thinker.html", {"thinker": thinker, "publications": publications})


@require_GET
def collection(request, slug):
    collection = get_object_or_404(Collection, slug=slug)
    publications = collection.publications.filter(status=Publication.Status.PUBLISHED).select_related("revision")
    return render(request, "library/collection.html", {"collection": collection, "publications": publications})


@require_GET
def credits_help(request):
    return render(request, "library/credits_help.html", {"costs": settings.CREDIT_COSTS, "welcome": settings.WELCOME_CREDITS})


@require_POST
@csrf_protect
def create_report(request, slug):
    """POST /reports/ per spec: complaint on a public material. A single
    unconfirmed report never hides the publication automatically."""
    publication = get_object_or_404(Publication, slug=slug, status=Publication.Status.PUBLISHED)
    category = request.POST.get("category", "").strip()
    details = request.POST.get("details", "").strip()[:2000]
    if not request.user.is_authenticated:
        return JsonResponse({"code": "auth_required", "message": "Войдите, чтобы отправить жалобу", "request_id": request.META.get("HTTP_X_REQUEST_ID", "")}, status=401)
    if category not in REPORT_CATEGORIES:
        return JsonResponse({"code": "bad_category", "message": "Выберите категорию жалобы", "request_id": ""}, status=400)
    if not details:
        return JsonResponse({"code": "details_required", "message": "Опишите проблему", "request_id": ""}, status=400)
    from django.utils import timezone

    recent = Report.objects.filter(reporter=request.user, created_at__gte=timezone.now() - timezone.timedelta(days=1)).count()
    if recent >= REPORTS_PER_USER_PER_DAY:
        return JsonResponse({"code": "rate_limited", "message": "Лимит жалоб на сутки исчерпан", "request_id": ""}, status=429)
    Report.objects.create(reporter=request.user, publication=publication, category=category, details=details)
    if request.headers.get("Accept", "").startswith("application/json") or request.POST.get("format") == "json":
        return JsonResponse({"code": "ok", "message": "Жалоба принята", "request_id": ""}, status=201)
    messages_content = "Жалоба принята. Она будет рассмотрена модератором."
    from django.contrib import messages as dj_messages

    dj_messages.success(request, messages_content)
    return redirect("library:read", slug=slug)
