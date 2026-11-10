from django.contrib import admin, messages
from django.utils import timezone

from apps.operations.models import AuditEvent

from .models import Collection, CollectionItem, Favorite, KnowledgeCard, Publication, SavedWord, Thinker, Topic
from .services import PublicationBlocked, publish_revision


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
    readonly_fields = ("published_at", "published_by")
    actions = ["publish_revision_action"]

    @admin.action(description="Опубликовать выбранную ревизию (редакционный путь)")
    def publish_revision_action(self, request, queryset):
        # The action works on revisions selected through their publications;
        # for a fresh publish the admin selects a Revision in the Revision
        # admin. Here we expose the same gate via RevisionAdmin below.
        for publication in queryset:
            self.message_user(request, f"{publication.slug}: уже связано с ревизией", messages.WARNING)

    def save_model(self, request, obj, form, change):
        if not change and obj.revision_id and not obj.published_at:
            obj.published_at = timezone.now()
            obj.published_by = request.user
            AuditEvent.objects.create(
                actor_reference=f"admin:{request.user.pk}",
                action="editorial_publish",
                object_reference=f"revision:{obj.revision_id}",
                reason="Публикация через админ-форму",
                sanitized_metadata={"publication_slug": obj.slug},
            )
        super().save_model(request, obj, form, change)


@admin.register(KnowledgeCard)
class KnowledgeCardAdmin(admin.ModelAdmin):
    list_display = ("slug", "version", "verification_status", "verified_at")
    list_filter = ("verification_status",)


class RevisionPublicationActionMixin:
    """Admin action on revisions: run the full publication gate."""

    @admin.action(description="Редакционная публикация выбранной ревизии")
    def publish_revision_action(self, request, queryset):
        for revision in queryset:
            try:
                publication, created = publish_revision(
                    revision=revision,
                    admin=request.user,
                    slug=revision.payload.get("title_en", "untitled").lower().replace(" ", "-")[:50] or f"rev-{revision.pk.hex[:8]}",
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
