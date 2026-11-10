from django.contrib import admin

from apps.library.admin import RevisionPublicationActionMixin

from .models import EvidenceLink, Message, Project, ProjectSource, Revision, RevisionKnowledgeCard, RevisionSource


@admin.register(Revision)
class RevisionAdmin(RevisionPublicationActionMixin, admin.ModelAdmin):
    list_display = ("id", "project", "created_at", "content_hash")
    list_filter = ("project__mode",)
    readonly_fields = [f.name for f in Revision._meta.fields]


admin.site.register(Project)
admin.site.register(ProjectSource)
admin.site.register(RevisionKnowledgeCard)
admin.site.register(RevisionSource)
admin.site.register(EvidenceLink)
admin.site.register(Message)
