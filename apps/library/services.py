"""Editorial publication service (stage 8).

Per docs/spec/docs/ai-pipeline.md and operations.md: publication is a
deliberate admin action for a concrete revision, gated by server checks —
a valid QualityReport for the current content hash, a verified knowledge
card, covered key claims, and acceptable source rights. No admin action
bypasses community voting for community publications (none exist in MVP).
"""

import json

from django.db import transaction
from django.utils import timezone

from apps.ai.models import QualityReport
from apps.operations.models import AuditEvent

from .models import KnowledgeCard, Publication


class PublicationBlocked(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.reasons = [message]


def publish_revision(*, revision, admin, slug, thinker=None, reason=""):
    """Create an editorial Publication after full server-side gating.

    Returns (publication, created). A repeat of the same revision returns
    the existing publication without a duplicate (idempotent action).
    """
    existing = Publication.objects.filter(revision=revision).first()
    if existing is not None:
        return existing, False
    reasons = []
    # 1. A quality report bound to the *current* content hash of this revision.
    report = QualityReport.objects.filter(
        revision=revision, revision_content_hash=revision.content_hash
    ).order_by("-created_at").first()
    if report is None:
        reasons.append("Нет QualityReport для текущего content hash ревизии")
    elif report.revision_content_hash != revision.content_hash:
        reasons.append("QualityReport относится к другой версии текста")
    # 2. Verified knowledge card linked to the revision.
    cards = KnowledgeCard.objects.filter(revisionknowledgecard__revision=revision)
    verified_cards = cards.filter(verification_status=KnowledgeCard.Status.VERIFIED)
    if not verified_cards.exists():
        reasons.append("Нет проверенной карточки знаний, связанной с ревизией")
    # 3. Review state: factual_state must be supported (or unverified draft
    # is never eligible), and the report must not carry blocking issues.
    if report is not None:
        review = report.review_json or {}
        if report.factual_state not in ("supported",):
            reasons.append(f"factual_state={report.factual_state} не допускает публикацию")
        blocking = [
            issue for issue in review.get("issues", [])
            if isinstance(issue, dict) and issue.get("severity") == "blocking"
        ]
        if blocking:
            reasons.append("В отчёте есть блокирующие замечания reviewer")
    # 4. Source rights: every linked source document must be usable
    # (editorial corpus or rights_state allowed); unknown rights block.
    from apps.sources.models import SourceDocument

    linked_documents = SourceDocument.objects.filter(revision_links__revision=revision).distinct()
    for document in linked_documents:
        if document.rights_state not in ("allowed",):
            if document.visibility == SourceDocument.Visibility.PRIVATE and document.rights_state == "unknown":
                reasons.append(f"Права на источник {document.pk} не подтверждены (rights_state=unknown)")
    if reasons:
        raise PublicationBlocked("publication_blocked", "; ".join(reasons))

    with transaction.atomic():
        publication = Publication.objects.create(
            slug=slug,
            revision=revision,
            quality_report=report,
            source_type="editorial",
            status=Publication.Status.PUBLISHED,
            thinker=thinker,
            published_at=timezone.now(),
            published_by=admin,
        )
        AuditEvent.objects.create(
            actor_reference=f"admin:{admin.pk}",
            action="editorial_publish",
            object_reference=f"revision:{revision.pk}",
            reason=reason[:255] or "Редакционная публикация по проверке качества",
            sanitized_metadata={
                "publication_slug": slug,
                "revision_hash": revision.content_hash,
                "quality_report": str(report.pk) if report else None,
            },
        )
    return publication, True


def retract_publication(*, publication, admin, reason):
    """Withdraw or hide a publication with an audit record."""
    publication.status = Publication.Status.WITHDRAWN
    publication.save(update_fields=["status"])
    AuditEvent.objects.create(
        actor_reference=f"admin:{admin.pk}",
        action="editorial_withdraw",
        object_reference=f"publication:{publication.pk}",
        reason=reason[:255],
        sanitized_metadata={"publication_slug": publication.slug},
    )
    return publication
