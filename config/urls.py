from django.contrib import admin
from django.db import connection
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    database = "ok"
    vector = "ok"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
            cursor.execute("SELECT '[1,2,3]'::vector(3)")
            cursor.fetchone()
    except Exception:
        database = "error"
        vector = "error"
    status = 200 if database == "ok" else 503
    return JsonResponse({"web": "ok", "database": database, "vector": vector}, status=status)


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", health, name="health"),
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("account/", include("apps.accounts.account_urls", namespace="account")),
    path("", include("apps.workshop.urls", namespace="workshop")),
    path("", include("apps.library.urls", namespace="library")),
]
