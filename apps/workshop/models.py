import hashlib
import json
import uuid

from django.conf import settings
from django.db import models


class Project(models.Model):
    class Mode(models.TextChoices):
        SIMPLIFY = "simplify_source"
        EXPLAIN = "explain_topic"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="projects")
    title = models.CharField(max_length=200)
    mode = models.CharField(max_length=20, choices=Mode.choices)
    head_revision = models.ForeignKey("Revision", null=True, blank=True, on_delete=models.SET_NULL, related_name="head_for")
    created_at = models.DateTimeField(auto_now_add=True)


class ProjectSource(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="project_sources")
    document = models.ForeignKey("sources.SourceDocument", on_delete=models.PROTECT, related_name="project_links")
    role = models.CharField(max_length=12, choices=[("primary", "primary"), ("context", "context")], default="context")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)


class Revision(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="revisions")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")
    payload = models.JSONField()
    content_hash = models.CharField(max_length=64, editable=False)
    created_by_job = models.OneToOneField("ai.AIJob", null=True, blank=True, on_delete=models.PROTECT, related_name="created_revision")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        canonical = json.dumps(self.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        if not self.content_hash:
            self.content_hash = content_hash
        elif self.content_hash != content_hash:
            raise ValueError("Revision payload is immutable; create a new revision instead")
        super().save(*args, **kwargs)


class RevisionKnowledgeCard(models.Model):
    revision = models.ForeignKey(Revision, on_delete=models.CASCADE, related_name="card_links")
    knowledge_card = models.ForeignKey("library.KnowledgeCard", on_delete=models.PROTECT)


class RevisionSource(models.Model):
    revision = models.ForeignKey(Revision, on_delete=models.CASCADE, related_name="source_links")
    document = models.ForeignKey("sources.SourceDocument", null=True, blank=True, on_delete=models.SET_NULL, related_name="revision_links")
    provenance_json = models.JSONField(default=dict)
    role = models.CharField(max_length=12, default="context")


class EvidenceLink(models.Model):
    revision = models.ForeignKey(Revision, on_delete=models.CASCADE, related_name="evidence_links")
    locator_json = models.JSONField(default=dict)
    kind = models.CharField(max_length=20, choices=[("claim_support", "claim_support"), ("source_alignment", "source_alignment")])
    claim_text = models.TextField(null=True, blank=True)
    citation_id = models.CharField(max_length=80)
    fact_id = models.CharField(max_length=80, null=True, blank=True)
    source_chunk = models.ForeignKey("sources.SourceChunk", null=True, blank=True, on_delete=models.CASCADE, related_name="evidence_links")
    support_type = models.CharField(max_length=20, default="paraphrase")


class Message(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="messages")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="workshop_messages")
    role = models.CharField(max_length=12, choices=[("user", "user"), ("assistant", "assistant"), ("system", "system")])
    kind = models.CharField(max_length=20, default="note")
    body = models.TextField()
    job = models.ForeignKey("ai.AIJob", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages")
    created_at = models.DateTimeField(auto_now_add=True)
