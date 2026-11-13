from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.ai.models import AIJob
from apps.credits.models import CreditTransaction, Wallet, grant_welcome_credits
from apps.library.models import Favorite, Publication, SavedWord
from apps.workshop.models import Project

from .forms import RegisterForm, WordForm
from .models import InviteCode, LoginAttempt, RegistrationEvent
from .network import network_hash
from .purge_service import purge_user_data

LOGIN_ATTEMPTS_PER_ACCOUNT_15_MIN = 10
LOGIN_ATTEMPTS_PER_NETWORK_15_MIN = 30
REGISTRATIONS_PER_NETWORK_PER_DAY = 3
REGISTRATIONS_GLOBAL_PER_DAY = 50


def _within_login_limits(request, login):
    """Cheap pre-checks before the expensive password hash."""
    from django.utils import timezone

    since = timezone.now() - timezone.timedelta(minutes=15)
    nh = network_hash(request)
    account_failures = LoginAttempt.objects.filter(login=login, succeeded=False, created_at__gte=since).count()
    if account_failures >= LOGIN_ATTEMPTS_PER_ACCOUNT_15_MIN:
        return False
    network_failures = LoginAttempt.objects.filter(network_hash=nh, succeeded=False, created_at__gte=since).count()
    if network_failures >= LOGIN_ATTEMPTS_PER_NETWORK_15_MIN:
        return False
    return True


def _record_login_attempt(request, login, succeeded):
    LoginAttempt.objects.create(login=login[:32], network_hash=network_hash(request), succeeded=succeeded)


def _within_registration_limits(request):
    from django.utils import timezone

    now = timezone.now()
    day_ago = now - timezone.timedelta(days=1)
    nh = network_hash(request)
    if RegistrationEvent.objects.filter(network_hash=nh, created_at__gte=day_ago).count() >= REGISTRATIONS_PER_NETWORK_PER_DAY:
        return False
    if RegistrationEvent.objects.filter(created_at__gte=day_ago).count() >= REGISTRATIONS_GLOBAL_PER_DAY:
        return False
    return True


@require_http_methods(["GET", "POST"])
def register(request):
    if settings.REGISTRATION_MODE == "closed":
        return render(request, "accounts/registration_closed.html", status=403)
    # A successful POST re-renders this page with an explicit success notice:
    # the reader must see clear confirmation that the account exists.
    registered_user = None
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not _within_registration_limits(request):
            return render(request, "accounts/register.html", {"form": form, "error": "Превышен лимит регистраций с этого адреса. Попробуйте позже."}, status=429)
        login_value = form.cleaned_data["login"]
        with transaction.atomic():
            if settings.REGISTRATION_MODE == "invite":
                code = form.cleaned_data["invite_code"]
                invite = InviteCode.objects.select_for_update().filter(code=code, used_at__isnull=True).first()
                if invite is None:
                    form.add_error("invite_code", "Недействительный или использованный код")
                    return render(request, "accounts/register.html", {"form": form})
            user = get_user_model().objects.create_user(login=login_value, password=form.cleaned_data["password"])
            grant_welcome_credits(user)
            RegistrationEvent.objects.create(network_hash=network_hash(request))
            if settings.REGISTRATION_MODE == "invite":
                invite.used_by = user
                invite.used_at = timezone.now()
                invite.save(update_fields=["used_by", "used_at"])
        login(request, user, backend="apps.accounts.backends.LoginBackend")
        registered_user = user
    if registered_user is not None:
        return render(
            request,
            "accounts/register_success.html",
            {"registered_login": registered_user.login, "welcome_credits": settings.WELCOME_CREDITS},
        )
    return render(request, "accounts/register.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request):
    error = None
    if request.method == "POST":
        login_value = request.POST.get("login", "").lower()[:32]
        if _within_login_limits(request, login_value):
            user = authenticate(request, login=login_value, password=request.POST.get("password", ""))
            _record_login_attempt(request, login_value, bool(user))
            if user:
                login(request, user, backend="apps.accounts.backends.LoginBackend")
                return redirect("account:dashboard")
        else:
            _record_login_attempt(request, login_value, False)
        error = "Неверный логин или пароль"
    return render(request, "accounts/login.html", {"error": error})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("library:home")


@require_POST
@login_required
def logout_all(request):
    user = request.user
    user.auth_epoch += 1
    user.save(update_fields=["auth_epoch"])
    logout(request)
    messages.info(request, "Все сеансы отозваны")
    return redirect("library:home")


@require_GET
@login_required
def dashboard(request):
    jobs = AIJob.objects.filter(owner=request.user)[:20]
    projects = Project.objects.filter(owner=request.user)[:20]
    return render(request, "accounts/dashboard.html", {"jobs": jobs, "projects": projects})


@require_GET
@login_required
def credits_view(request):
    wallet = get_object_or_404(Wallet, user=request.user)
    transactions = CreditTransaction.objects.filter(wallet=wallet).select_related("job")[:100]
    return render(request, "accounts/credits.html", {"transactions": transactions, "topup_mode": settings.TOPUP_MODE})


@require_GET
@login_required
def security(request):
    return render(request, "accounts/security.html")


@require_POST
@login_required
def change_password(request):
    user = request.user
    if not user.check_password(request.POST.get("current_password", "")):
        messages.error(request, "Текущий пароль неверен")
    elif request.POST.get("new_password") != request.POST.get("new_password_confirm"):
        messages.error(request, "Новые пароли не совпадают")
    else:
        from django.contrib.auth import password_validation

        try:
            password_validation.validate_password(request.POST["new_password"], user)
        except Exception as exc:
            messages.error(request, "; ".join(exc.messages))
        else:
            user.set_password(request.POST["new_password"])
            user.auth_epoch += 1
            user.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Пароль изменён, остальные сеансы отозваны")
    return redirect("account:security")


@require_http_methods(["GET", "POST"])
@login_required
def words(request):
    form = WordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        publication = None
        if form.cleaned_data.get("publication_id"):
            publication = get_object_or_404(Publication, pk=form.cleaned_data["publication_id"], status=Publication.Status.PUBLISHED)
        SavedWord.objects.update_or_create(
            user=request.user,
            term=form.cleaned_data["term"],
            defaults={"translation_ru": form.cleaned_data["translation_ru"], "context_en": form.cleaned_data["context_en"], "source_reference": publication},
        )
        messages.success(request, "Слово сохранено")
        return redirect("account:words")
    return render(request, "accounts/words.html", {"words": SavedWord.objects.filter(user=request.user), "form": form})


@require_GET
@login_required
def favorites(request):
    favorites = Favorite.objects.filter(user=request.user).select_related("publication", "publication__revision")
    return render(request, "accounts/favorites.html", {"favorites": favorites})


@require_POST
@login_required
def favorite_toggle(request, publication_id):
    publication = get_object_or_404(Publication, pk=publication_id, status=Publication.Status.PUBLISHED)
    favorite = Favorite.objects.filter(user=request.user, publication=publication).first()
    if favorite:
        favorite.delete()
    else:
        Favorite.objects.create(user=request.user, publication=publication)
    next_url = request.POST.get("next", "").strip()
    if url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(next_url)
    return redirect("account:favorites")


@require_GET
@login_required
def export_data(request):
    """Download the user's own data as JSON. No other user's data included."""
    from django.http import JsonResponse

    from .export_service import export_user_data

    data = export_user_data(request.user)
    response = JsonResponse(data, json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = 'attachment; filename="philosophy-reader-export.json"'
    return response


@require_POST
@login_required
def delete_account(request):
    if not request.user.check_password(request.POST.get("password", "")):
        messages.error(request, "Пароль неверен")
        return redirect("account:security")
    user = request.user
    purge_user_data(user, reason="Удаление аккаунта пользователем через кабинет")
    logout(request)
    messages.info(request, "Аккаунт деактивирован, приватные данные удалены")
    return redirect("library:home")
