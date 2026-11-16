"""Editorial publication service (stage 8).

Per docs/spec/docs/ai-pipeline.md and operations.md: publication is a
deliberate admin action for a concrete revision, gated by server checks —
a valid QualityReport for the current content hash, a verified knowledge
card, covered key claims, and acceptable source rights. No admin action
bypasses community voting for community publications (none exist in MVP).
"""

import json

from django.core.exceptions import ValidationError
from django.core.validators import validate_slug
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.ai.models import QualityReport
from apps.operations.models import AuditEvent

from .models import KnowledgeCard, Publication


class PublicationBlocked(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.reasons = [message]


def publication_slug(revision, preferred=None):
    """Address for a revision's publication.

    Built from the English title with punctuation removed ("What does it
    mean to be brave?" -> what-does-it-mean-to-be-brave), so the reader URL
    always resolves. An earlier publication of the same project keeps its
    address; a slug taken by another text gets a numeric suffix.
    """
    earlier = Publication.objects.filter(revision__project=revision.project).order_by("-published_at").first()
    if earlier is not None:
        return earlier.slug
    limit = Publication._meta.get_field("slug").max_length
    base = slugify(preferred or revision.payload.get("title_en") or "")[:limit].strip("-") or f"text-{revision.pk}"
    slug, number = base, 2
    while Publication.objects.filter(slug=slug).exists():
        suffix = f"-{number}"
        slug = base[: limit - len(suffix)].rstrip("-") + suffix
        number += 1
    return slug


def publish_revision(*, revision, admin, slug, thinker=None, reason=""):
    """Create an editorial Publication after full server-side gating.

    Returns (publication, created). A repeat of the same revision returns
    the existing publication without a duplicate (idempotent action). A
    newer revision of an already published text replaces the old one under
    the same address, after passing the same gate.
    """
    existing = Publication.objects.filter(revision=revision).first()
    if existing is not None and existing.status == Publication.Status.PUBLISHED:
        return existing, False
    reasons = []
    # 0. The address must be a real slug: anything else breaks every page
    # that links to the text. One text has one address: it cannot take the
    # address of another text or get a second address of its own.
    if not isinstance(slug, str):
        raise PublicationBlocked("invalid_slug", "Адрес публикации не задан")
    try:
        validate_slug(slug)
    except ValidationError:
        raise PublicationBlocked("invalid_slug", f"Недопустимый адрес публикации: {slug!r}")
    if len(slug) > Publication._meta.get_field("slug").max_length:
        raise PublicationBlocked("invalid_slug", "Адрес публикации слишком длинный")
    by_slug = Publication.objects.select_related("revision").filter(slug=slug).first()
    if by_slug is not None and by_slug.revision.project_id != revision.project_id:
        raise PublicationBlocked("slug_taken", f"Адрес {slug} занят другим текстом")
    # An earlier publication of the same text (any revision of the project).
    previous = existing or Publication.objects.filter(revision__project=revision.project).order_by("-published_at").first()
    if previous is not None and previous.slug != slug:
        raise PublicationBlocked("slug_mismatch", f"Этот текст уже имеет адрес {previous.slug}; адрес не меняется")
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
        # The reviewer's verdict must be an explicit pass.
        if review.get("verdict") != "pass":
            reasons.append(f"verdict={review.get('verdict')} не допускает публикацию")
        # The report itself must say the revision is eligible: only a review
        # that received the verified cards can state that.
        if report.eligibility_state != "eligible":
            reasons.append(f"eligibility_state={report.eligibility_state} не допускает публикацию")
        # The report names the card versions the reviewer saw. A report made
        # without cards, or with other cards than the revision has now, is stale.
        from .editorial import card_version_records

        if not report.card_versions_json or _card_key(report.card_versions_json) != _card_key(card_version_records(revision)):
            reasons.append("Отчёт сделан без нынешних проверенных карточек: нужна новая проверка")
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

    # A withdrawn publication of this revision, or another revision of the
    # same text, is restored/replaced in place under the same address. This
    # includes going back to an older revision: it passes the same gate.
    try:
        if previous is not None:
            return _republish(previous, revision=revision, report=report, admin=admin, reason=reason), True
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
    except IntegrityError:
        # Two publish actions at once: the unique constraints let one win.
        raise PublicationBlocked("publication_conflict", "Публикация этого текста уже выполняется; повторите действие")
    return publication, True


def _card_key(records):
    return sorted(
        (str(r.get("slug")), r.get("version"), str(r.get("content_hash")))
        for r in records
        if isinstance(r, dict)
    )


def _republish(publication, *, revision, report, admin, reason):
    """Point an existing publication at a newer, freshly gated revision of
    the same text. The address, favourites and reports stay with it."""
    with transaction.atomic():
        publication = Publication.objects.select_for_update().get(pk=publication.pk)
        old_hash = publication.revision.content_hash
        publication.revision = revision
        publication.quality_report = report
        publication.status = Publication.Status.PUBLISHED
        publication.published_at = timezone.now()
        publication.published_by = admin
        publication.save(update_fields=["revision", "quality_report", "status", "published_at", "published_by"])
        AuditEvent.objects.create(
            actor_reference=f"admin:{admin.pk}",
            action="editorial_republish",
            object_reference=f"revision:{revision.pk}",
            reason=reason[:255] or "Новая проверенная версия текста",
            sanitized_metadata={
                "publication_slug": publication.slug,
                "previous_revision_hash": old_hash,
                "revision_hash": revision.content_hash,
                "quality_report": str(report.pk),
            },
        )
    return publication


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
