from django.urls import path

from . import views

app_name = "workshop"

urlpatterns = [
    path("projects/new/", views.create_project, name="create_project"),
    path("workshop/<uuid:project_id>/", views.project_detail, name="project"),
    path("workshop/<uuid:project_id>/ask/", views.ask, name="ask"),
    path("workshop/<uuid:project_id>/edit/", views.edit_fragment, name="edit_fragment"),
    path("workshop/<uuid:project_id>/revisions/<str:revision_id>/diff/", views.revision_diff, name="revision_diff"),
    path("projects/<uuid:project_id>/head/", views.apply_head, name="apply_head"),
    path("jobs/<uuid:job_id>/", views.job_status, name="job_status"),
]
