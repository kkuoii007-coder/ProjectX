from django.contrib import admin, messages

from .models import Collection, CollectionItem, Favorite, KnowledgeCard, Publication, SavedWord, Thinker, Topic
from .services import PublicationBlocked, publication_slug, publish_revision, retract_publication


class CollectionItemInline(admin.TabularInline):
    model = CollectionItem
    extra = 0


@admin.register(Collection)
class CollectionAdmin(admin.ModelAdmin):
    prepopulated_fields = {"slug": ("title_ru",)}
    inlines = [CollectionItemInline]


@admin.register(Publication)
class PublicationAdmin(admin.ModelAdmin):
    list_display = ("slug", "status", "source_type", "published_at")
    list_filter = ("status", "source_type")
    readonly_fields = ("slug", "revision", "quality_report", "source_type", "published_at", "published_by")
    actions = ["withdraw_action"]

    def has_add_permission(self, request):
        # A publication is created only by publish_revision (the gate):
        # the "Редакционная публикация" action on a revision or the
        # publish_text command. A hand-made record would bypass the checks.
        return False

    @admin.action(description="Снять с публикации")
    def withdraw_action(self, request, queryset):
        for publication in queryset:
            if publication.status == Publication.Status.WITHDRAWN:
                self.message_user(request, f"{publication.slug}: уже снято", messages.WARNING)
                continue
            retract_publication(publication=publication, admin=request.user, reason=f"Admin action by {request.user.login}")
            self.message_user(request, f"Снято с публикации: {publication.slug}", messages.SUCCESS)


@admin.register(KnowledgeCard)
class KnowledgeCardAdmin(admin.ModelAdmin):
    list_display = ("slug", "version", "verification_status", "verified_at")
    list_filter = ("verification_status",)


class RevisionPublicationActionMixin:
    """Admin actions on revisions: live review and the full publication gate."""

    @admin.action(description="Проверить ревизию (живая проверка, платный вызов)")
    def review_revision_action(self, request, queryset):
        from apps.ai.services import JobError, create_review_job

        for revision in queryset:
            try:
                job, created = create_review_job(revision=revision, actor=f"admin:{request.user.pk}")
            except JobError as exc:
                self.message_user(request, f"Ревизия {revision.pk}: {exc}", messages.ERROR)
                continue
            text = "поставлена в очередь" if created else "уже в очереди"
            self.message_user(request, f"Ревизия {revision.pk}: проверка {text} (задача {job.pk})", messages.SUCCESS)

    @admin.action(description="Редакционная публикация выбранной ревизии")
    def publish_revision_action(self, request, queryset):
        for revision in queryset:
            try:
                publication, created = publish_revision(
                    revision=revision,
                    admin=request.user,
                    slug=publication_slug(revision),
                    reason=f"Admin action by {request.user.login}",
                )
            except PublicationBlocked as exc:
                self.message_user(request, f"Отказ: {exc}", messages.ERROR)
                continue
            if created:
                self.message_user(request, f"Опубликовано: {publication.slug}", messages.SUCCESS)
            else:
                self.message_user(request, f"Уже опубликовано: {publication.slug}", messages.WARNING)


admin.site.register(Thinker)
admin.site.register(Topic)
admin.site.register(Favorite)
admin.site.register(SavedWord)
