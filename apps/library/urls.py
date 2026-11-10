from django.urls import path

from . import views

app_name = "library"

urlpatterns = [
    path("", views.home, name="home"),
    path("library/", views.library, name="library"),
    path("read/<slug:slug>/", views.read, name="read"),
    path("reports/<slug:slug>/", views.create_report, name="create_report"),
    path("thinkers/<slug:slug>/", views.thinker, name="thinker"),
    path("collections/<slug:slug>/", views.collection, name="collection"),
    path("help/credits/", views.credits_help, name="credits_help"),
]
