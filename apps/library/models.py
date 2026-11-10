import uuid

from django.conf import settings
from django.db import models


class Thinker(models.Model):
    slug = models.SlugField(unique=True)
    name_en = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120)

    def __str__(self):
        return self.name_ru


class Topic(models.Model):
    slug = models.SlugField(unique=True)
    name_en = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120)

    def __str__(self):
        return self.name_ru


class KnowledgeCard(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft"
        VERIFIED = "verified"
        RETIRED = "retired"

    slug = models.SlugField()
    version = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=200)
    facts_json = models.JSONField(default=list)
    pitfalls_json = models.JSONField(default=list)
    verification_status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="verified_cards")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["slug", "version"], name="unique_card_version")]


class Publication(models.Model):
    class Status(models.TextChoices):
        PUBLISHED = "published"
        HIDDEN = "hidden"
        WITHDRAWN = "withdrawn"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(unique=True)
    revision = models.OneToOneField("workshop.Revision", on_delete=models.PROTECT, related_name="publication")
    quality_report = models.OneToOneField("ai.QualityReport", null=True, blank=True, on_delete=models.PROTECT, related_name="publication")
    source_type = models.CharField(max_length=20, default="editorial")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PUBLISHED)
    thinker = models.ForeignKey(Thinker, null=True, blank=True, on_delete=models.PROTECT, related_name="publications")
    topics = models.ManyToManyField(Topic, blank=True, related_name="publications")
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="published_materials")

    @property
    def title(self):
        return self.revision.payload.get("title_ru", self.slug)


class Collection(models.Model):
    slug = models.SlugField(unique=True)
    title_ru = models.CharField(max_length=200)
    description_ru = models.TextField(blank=True)
    publications = models.ManyToManyField(Publication, through="CollectionItem", related_name="collections")


class CollectionItem(models.Model):
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE)
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["collection", "publication"], name="unique_collection_publication")]


class Favorite(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="favorites")
    publication = models.ForeignKey(Publication, on_delete=models.CASCADE, related_name="favorited_by")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "publication"], name="unique_user_favorite")]


class SavedWord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_words")
    term = models.CharField(max_length=100)
    normalized_term = models.CharField(max_length=100, editable=False)
    translation_ru = models.CharField(max_length=200)
    context_en = models.TextField(blank=True)
    source_reference = models.ForeignKey(Publication, null=True, blank=True, on_delete=models.SET_NULL, related_name="saved_words")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "term"], name="unique_user_word")]

    def save(self, *args, **kwargs):
        self.normalized_term = self.term.strip().lower()
        super().save(*args, **kwargs)
