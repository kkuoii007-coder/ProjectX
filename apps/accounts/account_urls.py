from django.urls import path

from . import views

app_name = "account"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("credits/", views.credits_view, name="credits"),
    path("security/", views.security, name="security"),
    path("change-password/", views.change_password, name="change_password"),
    path("delete/", views.delete_account, name="delete"),
    path("words/", views.words, name="words"),
    path("favorites/", views.favorites, name="favorites"),
    path("favorites/<uuid:publication_id>/toggle/", views.favorite_toggle, name="favorite_toggle"),
    path("export/", views.export_data, name="export"),
]
