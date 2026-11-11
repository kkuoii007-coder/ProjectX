from django.conf import settings
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from apps.operations.models import Report

from .models import Collection, Favorite, Publication, Thinker
from .presenters import outline, pick_specimen, reader_parts, reader_terms

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
    published = Publication.objects.filter(status=Publication.Status.PUBLISHED)
    publications = list(published.select_related("revision", "thinker").order_by("-published_at", "slug")[:6])
    featured = None
    if publications:
        payload = publications[0].revision.payload
        parts = reader_parts(payload)
        featured = {"publication": publications[0], "outline": outline(payload), "specimen": pick_specimen(parts)}
    others = [{"publication": p, "outline": outline(p.revision.payload)} for p in publications[1:]]
    collections = (
        Collection.objects.annotate(published_count=Count("publications", filter=Q(publications__status=Publication.Status.PUBLISHED)))
        .filter(published_count__gt=0)[:5]
    )
    thinkers = Thinker.objects.filter(publications__status=Publication.Status.PUBLISHED).distinct().order_by("name_ru")[:12]
    context = {
        "featured": featured,
        "others": others,
        "collections": collections,
        "thinkers": thinkers,
        "costs": settings.CREDIT_COSTS,
        "welcome": settings.WELCOME_CREDITS,
    }
    return render(request, "library/home.html", context)


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
    payload = publication.revision.payload
    is_favorite = request.user.is_authenticated and Favorite.objects.filter(user=request.user, publication=publication).exists()
    context = {
        "publication": publication,
        "parts": reader_parts(payload),
        "outline": outline(payload),
        "terms": reader_terms(payload),
        "is_favorite": is_favorite,
        "report_categories": REPORT_CATEGORIES,
    }
    return render(request, "library/read.html", context)


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
