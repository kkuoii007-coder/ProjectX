from django.contrib import admin

from .models import AIJob, ProviderAttempt, QualityReport, RetrievalItem, SourceAttempt


@admin.register(AIJob)
class AIJobAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "kind", "status", "outcome", "reserved_credits", "created_at")
    list_filter = ("status", "kind")
    readonly_fields = [f.name for f in AIJob._meta.fields]


@admin.register(QualityReport)
class QualityReportAdmin(admin.ModelAdmin):
    """Read-only: a report is evidence produced by a review job. A report
    typed or edited by hand would open the publication gate."""

    list_display = ("id", "revision", "factual_state", "eligibility_state", "review_model", "created_at")
    list_filter = ("factual_state", "eligibility_state")
    readonly_fields = [f.name for f in QualityReport._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(ProviderAttempt)
admin.site.register(RetrievalItem)
admin.site.register(SourceAttempt)
