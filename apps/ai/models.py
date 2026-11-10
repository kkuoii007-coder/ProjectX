import uuid

from django.conf import settings
from django.db import models


class AIJob(models.Model):
    class Kind(models.TextChoices):
        GENERATE = "generate"
        EDIT_SERIES = "edit_series"
        EDIT_PART = "edit_part"
        EDIT_SENTENCE = "edit_sentence"
        ASK = "ask"
        IMPORT = "import_source"
        INDEX = "index_source"
        SEARCH = "search_sources"

    class Status(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        SUCCEEDED = "succeeded"
        FAILED = "failed"
        CANCELLED = "cancelled"
        RECONCILIATION = "reconciliation_required"

    class Outcome(models.TextChoices):
        DELIVERED = "delivered"
        CLARIFICATION = "needs_clarification"
        REFUSED = "refused"
        BROADER_EDIT = "needs_broader_edit"
        NO_CHARGE_FAILURE = "no_charge_failure"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_jobs")
    project = models.ForeignKey("workshop.Project", null=True, blank=True, on_delete=models.CASCADE, related_name="jobs")
    document = models.ForeignKey("sources.SourceDocument", null=True, blank=True, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    base_revision = models.ForeignKey("workshop.Revision", null=True, blank=True, on_delete=models.PROTECT, related_name="jobs")
    input_json = models.JSONField(default=dict)
    input_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=100)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.QUEUED)
    stage = models.CharField(max_length=20, default="prepare")
    outcome = models.CharField(max_length=25, choices=Outcome.choices, null=True, blank=True)
    reserved_credits = models.PositiveIntegerField(default=0)
    policy_version = models.CharField(max_length=40)
    lease_until = models.DateTimeField(null=True, blank=True)
    lease_token = models.CharField(max_length=64, null=True, blank=True)
    result_reference = models.JSONField(default=dict)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [models.UniqueConstraint(fields=["owner", "idempotency_key"], name="unique_owner_job_key")]


class RetrievalItem(models.Model):
    job = models.ForeignKey(AIJob, on_delete=models.CASCADE, related_name="retrieval_items")
    source_chunk = models.ForeignKey("sources.SourceChunk", on_delete=models.CASCADE, related_name="retrieval_items")
    citation_id = models.CharField(max_length=80)
    rank = models.PositiveIntegerField()
    score_json = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "citation_id"], name="unique_job_citation")]


class ProviderAttempt(models.Model):
    job = models.ForeignKey(AIJob, on_delete=models.CASCADE, related_name="provider_attempts")
    stage_number = models.PositiveIntegerField()
    request_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20)
    provider_request_id = models.CharField(max_length=200, null=True, blank=True)
    model = models.CharField(max_length=200)
    provider = models.CharField(max_length=100, blank=True)
    pricing_snapshot = models.JSONField(default=dict)
    usage_json = models.JSONField(default=dict)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    max_reserved_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "stage_number"], name="unique_provider_stage")]


class SourceAttempt(models.Model):
    job = models.ForeignKey(AIJob, on_delete=models.CASCADE, related_name="source_attempts")
    stage_number = models.PositiveIntegerField()
    operation = models.CharField(max_length=12)
    request_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20)
    provider = models.CharField(max_length=100, blank=True)
    usage_json = models.JSONField(default=dict)
    cost_usd = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["job", "stage_number"], name="unique_source_stage")]


class QualityReport(models.Model):
    revision = models.ForeignKey("workshop.Revision", on_delete=models.PROTECT, related_name="quality_reports")
    revision_content_hash = models.CharField(max_length=64)
    deterministic_json = models.JSONField(default=dict)
    review_json = models.JSONField(default=dict)
    review_model = models.CharField(max_length=200, blank=True)
    prompt_version = models.CharField(max_length=40, blank=True)
    schema_version = models.CharField(max_length=40, blank=True)
    retrieval_snapshot_hash = models.CharField(max_length=64, blank=True)
    card_versions_json = models.JSONField(default=list)
    factual_state = models.CharField(max_length=15, default="unverified")
    eligibility_state = models.CharField(max_length=15, default="ineligible")
    created_at = models.DateTimeField(auto_now_add=True)
