from django.contrib import admin

from .models import AIJob, ProviderAttempt, QualityReport, RetrievalItem, SourceAttempt


@admin.register(AIJob)
class AIJobAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "kind", "status", "outcome", "reserved_credits", "created_at")
    list_filter = ("status", "kind")
    readonly_fields = [f.name for f in AIJob._meta.fields]


admin.site.register(ProviderAttempt)
admin.site.register(RetrievalItem)
admin.site.register(SourceAttempt)
admin.site.register(QualityReport)
