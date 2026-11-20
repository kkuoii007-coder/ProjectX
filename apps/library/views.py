from django.conf import settings
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_POST

from apps.operations.models import Report

from .models import Collection, Favorite, Publication, Thinker
from .presenters import about_text, cover_lines, fork_scope, outline, publication_item, reader_parts, reader_terms, titles, tone

REPORT_CATEGORIES = {
    "factual_error": "Фактическая ошибка",
    "too_complex": "Слишком сложно",
    "bad_translation": "Плохой перевод",
    "inappropriate": "Неприемлемый контент",
    "rights": "Права на текст",
    "other": "Другое",
}

REPORTS_PER_USER_PER_DAY = 3
HOME_OTHERS_LIMIT = 12
# The existing search is offered only when there is something to search in.
LIBRARY_SEARCH_MIN = 6


def _published():
    return Publication.objects.filter(status=Publication.Status.PUBLISHED).select_related("revision", "thinker")


@require_GET
def home(request):
    """The cover: the question of the start reading, two of its sentences, one button.

    The start reading is named in settings.START_READING_SLUG. If it is not
    published, the cover says so; the latest publication is never used instead.
    """
    start = None
    start_publication = _published().filter(slug=settings.START_READING_SLUG).first() if settings.START_READING_SLUG else None
    if start_publication is not None:
        start = publication_item(start_publication)
        start["lines"] = cover_lines(reader_parts(start_publication.revision.payload))
    others = _published().order_by("-published_at", "slug")
    if start_publication is not None:
        others = others.exclude(pk=start_publication.pk)
    context = {"start": start, "others": [publication_item(p) for p in others[:HOME_OTHERS_LIMIT]]}
    return render(request, "library/home.html", context)


@require_GET
def library(request):
    query = request.GET.get("q", "").strip()[:200]
    publications = _published().order_by("-published_at", "slug")
    total = publications.count()
    if query:
        publications = publications.filter(
            Q(slug__icontains=query)
            | Q(revision__payload__title_en__icontains=query)
            | Q(revision__payload__title_ru__icontains=query)
            | Q(thinker__name_ru__icontains=query)
            | Q(thinker__name_en__icontains=query)
        )
    context = {
        "publications": publications,
        "items": [publication_item(p) for p in publications],
        "query": query,
        "show_search": bool(query) or total >= LIBRARY_SEARCH_MIN,
    }
    return render(request, "library/library.html", context)


def _next_publication(publication):
    """Another published text to read after this one, or None."""
    others = _published().exclude(pk=publication.pk)
    later = others.filter(published_at__gt=publication.published_at).order_by("published_at", "slug").first() if publication.published_at else None
    return later or others.order_by("published_at", "slug").first()


@require_GET
def read(request, slug):
    publication = get_object_or_404(_published(), slug=slug)
    payload = publication.revision.payload
    is_favorite = request.user.is_authenticated and Favorite.objects.filter(user=request.user, publication=publication).exists()
    following = _next_publication(publication)
    shown = titles(payload)
    if not shown["title_en"]:
        shown["title_en"] = publication.slug
    parts = reader_parts(payload)
    context = {
        "publication": publication,
        **shown,
        "tone": tone(publication),
        "parts": parts,
        "fork_scope": fork_scope(publication, parts),
        "outline": outline(payload),
        "terms": reader_terms(payload),
        "about": about_text(payload),
        "next": publication_item(following) if following else None,
        "is_favorite": is_favorite,
        "report_categories": REPORT_CATEGORIES,
    }
    return render(request, "library/read.html", context)


@require_GET
def thinker(request, slug):
    thinker = get_object_or_404(Thinker, slug=slug)
    publications = _published().filter(thinker=thinker).order_by("-published_at", "slug")
    if not publications.exists():
        # A thinker with no published text has no page: an empty shell
        # would stay reachable after the last text is withdrawn.
        raise Http404("No published texts for this thinker")
    context = {"thinker": thinker, "publications": publications, "items": [publication_item(p) for p in publications]}
    return render(request, "library/thinker.html", context)


@require_GET
def collection(request, slug):
    collection = get_object_or_404(Collection, slug=slug)
    publications = collection.publications.filter(status=Publication.Status.PUBLISHED).select_related("revision", "thinker")
    if not publications.exists():
        raise Http404("No published texts in this collection")
    context = {"collection": collection, "publications": publications, "items": [publication_item(p) for p in publications]}
    return render(request, "library/collection.html", context)


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
