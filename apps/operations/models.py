from django.conf import settings
from django.db import models


class Report(models.Model):
    reporter = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reports")
    publication = models.ForeignKey("library.Publication", on_delete=models.CASCADE, related_name="reports")
    category = models.CharField(max_length=40)
    details = models.TextField(max_length=2000)
    status = models.CharField(max_length=20, default="new")
    created_at = models.DateTimeField(auto_now_add=True)


class AuditEvent(models.Model):
    actor_reference = models.CharField(max_length=100, null=True, blank=True)
    action = models.CharField(max_length=100)
    object_reference = models.CharField(max_length=200)
    reason = models.CharField(max_length=255)
    sanitized_metadata = models.JSONField(default=dict)
    timestamp = models.DateTimeField(auto_now_add=True)
