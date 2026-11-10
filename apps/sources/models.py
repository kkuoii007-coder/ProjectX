import uuid

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.conf import settings
from django.db import models
from django.db.models.fields.generated import GeneratedField
from django.db.models.functions import Collate
from pgvector.django import VectorField


class SourceWork(models.Model):
    author = models.CharField(max_length=200)
    title = models.CharField(max_length=300)
    kind = models.CharField(max_length=12, choices=[("primary", "primary"), ("secondary", "secondary")], default="primary")
    language = models.CharField(max_length=8, default="en")
    canonical_url = models.URLField(null=True, blank=True)


class SourceEdition(models.Model):
    work = models.ForeignKey(SourceWork, on_delete=models.PROTECT, related_name="editions")
    edition = models.CharField(max_length=200, blank=True)
    translator = models.CharField(max_length=200, blank=True)
    language = models.CharField(max_length=8, default="en")
    rights = models.CharField(max_length=200, default="unknown")
    url = models.URLField(null=True, blank=True)
    retrieved_at = models.DateTimeField(null=True, blank=True)


class SourceDocument(models.Model):
    class Origin(models.TextChoices):
        USER = "user"
        EDITORIAL = "editorial"
        WEB = "web"

    class Visibility(models.TextChoices):
        PRIVATE = "private"
        EDITORIAL = "editorial"

    class Processing(models.TextChoices):
        UPLOADED = "uploaded"
        EXTRACTING = "extracting"
        INDEXING = "indexing"
        READY = "ready"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="source_documents")
    edition = models.ForeignKey(SourceEdition, null=True, blank=True, on_delete=models.PROTECT, related_name="documents")
    lineage_id = models.UUIDField(default=uuid.uuid4)
    version = models.PositiveIntegerField(default=1)
    supersedes = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by")
    origin = models.CharField(max_length=12, choices=Origin.choices, default=Origin.EDITORIAL)
    visibility = models.CharField(max_length=12, choices=Visibility.choices, default=Visibility.EDITORIAL)
    rights_state = models.CharField(max_length=20, default="unknown")
    verification_state = models.CharField(max_length=12, default="unverified")
    content_hash = models.CharField(max_length=64)
    storage_reference = models.CharField(max_length=255, null=True, blank=True)
    normalized_text = models.TextField(blank=True)
    processing_state = models.CharField(max_length=12, choices=Processing.choices, default=Processing.UPLOADED)
    metadata_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lineage_id", "version"], name="unique_document_version"),
            models.UniqueConstraint(fields=["lineage_id", "content_hash"], name="unique_document_content"),
        ]


class SourceChunk(models.Model):
    document = models.ForeignKey(SourceDocument, on_delete=models.CASCADE, related_name="chunks")
    ordinal = models.PositiveIntegerField()
    text = models.TextField()
    locator_json = models.JSONField(default=dict)
    token_count = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=64)
    # Managed by the DB as a generated column (migration 0003). GeneratedField
    # keeps Django from writing it and lets ORM queries read it.
    search_vector = GeneratedField(
        expression=models.Func(models.F("text"), function="to_tsvector", template="%(function)s('english', %(expressions)s)"),
        output_field=SearchVectorField(),
        db_persist=True,
    )

    class Meta:
        ordering = ["ordinal"]
        indexes = [GinIndex(fields=["search_vector"], name="sources_chunk_search_gin")]
        constraints = [models.UniqueConstraint(fields=["document", "ordinal"], name="unique_chunk_ordinal")]

    def save(self, *args, update_fields=None, **kwargs):
        if update_fields:
            update_fields = [f for f in update_fields if f != "search_vector"]
        return super().save(*args, update_fields=update_fields, **kwargs)


class Embedding(models.Model):
    source_chunk = models.ForeignKey(SourceChunk, on_delete=models.CASCADE, related_name="embeddings")
    model = models.CharField(max_length=200)
    dimensions = models.PositiveIntegerField()
    vector = VectorField(dimensions=1536)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source_chunk", "model", "dimensions", "content_hash"], name="unique_chunk_embedding")]


class SourceSearch(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="source_searches")
    query = models.CharField(max_length=500)
    provider = models.CharField(max_length=100, blank=True)
    allowlist_version = models.CharField(max_length=40, blank=True)
    results_json = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)


class SearchResultSelection(models.Model):
    search = models.ForeignKey(SourceSearch, on_delete=models.CASCADE, related_name="selections")
    selected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="source_selections")
    url = models.URLField()
    document = models.ForeignKey(SourceDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="search_selections")
    created_at = models.DateTimeField(auto_now_add=True)
