"""User data purge (stage 9).

Per docs/spec/docs/auth-privacy-security.md "Экспорт и удаление" and
data-model.md: no blind CASCADE over financial events or audit; deactivate
immediately, cancel queued jobs, release reservations, remove private data,
keep the ledger and legally necessary records minimal and separate.
"""

from django.db import transaction
from django.utils import timezone


def purge_user_data(user, *, reason: str) -> dict:
    """Deactivate, anonymize and purge one account's private data.

    The account row survives (auth/ledger FK integrity) but is deactivated,
    stripped of identifiers and marked purged. Financial ledger entries and
    audit events are preserved without personal identifiers.
    """
    from apps.ai.models import AIJob
    from apps.accounts.models import LoginAttempt, RegistrationEvent
    from apps.credits.models import settle_reservation
    from apps.library.models import Favorite, SavedWord
    from apps.sources.models import Embedding, SourceChunk, SourceDocument
    from apps.workshop.models import EvidenceLink, Message, Project, Revision, RevisionSource

    stats = {}
    with transaction.atomic():
        AIJob.objects.filter(owner=user, status=AIJob.Status.QUEUED).update(status=AIJob.Status.CANCELLED)
        for reservation_user_jobs in AIJob.objects.filter(owner=user, status=AIJob.Status.RUNNING):
            settle_reservation(job=reservation_user_jobs, charge=False)
        AIJob.objects.filter(owner=user, status=AIJob.Status.RUNNING).update(status=AIJob.Status.FAILED, error_code="account_deleted", completed_at=timezone.now())

        # Release any dangling credit reservation before wallet anonymization.
        stats["jobs_cancelled"] = AIJob.objects.filter(owner=user, status=AIJob.Status.CANCELLED).count()

        # User projects and everything hanging off them. AIJob rows survive
        # (financial references are PROTECT) but lose the project link first.
        from apps.ai.models import ProviderAttempt, RetrievalItem

        user_jobs = AIJob.objects.filter(owner=user)
        stats["projects"] = Project.objects.filter(owner=user).count()
        revision_ids = list(Revision.objects.filter(project__owner=user).values_list("pk", flat=True))
        stats["revisions"] = len(revision_ids)
        EvidenceLink.objects.filter(revision_id__in=revision_ids).delete()
        RetrievalItem.objects.filter(job__in=user_jobs).delete()
        ProviderAttempt.objects.filter(job__in=user_jobs).delete()
        user_jobs.update(project=None, document=None, base_revision=None)
        Project.objects.filter(owner=user).delete()

        # Own source documents: chunks, embeddings, metadata.
        documents = SourceDocument.objects.filter(owner=user)
        stats["documents"] = documents.count()
        chunk_ids = list(SourceChunk.objects.filter(document__in=documents).values_list("pk", flat=True))
        Embedding.objects.filter(source_chunk_id__in=chunk_ids).delete()
        SourceChunk.objects.filter(document__in=documents).delete()
        documents.delete()

        Favorite.objects.filter(user=user).delete()
        stats["words"] = SavedWord.objects.filter(user=user).delete()[0]
        Message.objects.filter(owner=user).delete()

        # Pseudonymized network identifiers in auth events.
        LoginAttempt.objects.filter(login=user.login).delete()
        RegistrationEvent.objects.filter(network_hash__isnull=False).delete()

        # The user row stays for ledger/audit FK integrity; strip identifiers.
        user.is_active = False
        user.auth_epoch += 1
        user.login = f"deleted-{user.pk.hex[:12]}"
        user.login_normalized = user.login
        user.public_alias = "Автор удалён"
        user.set_unusable_password()
        user.legal_ack_version = f"purged-{timezone.now().date().isoformat()}"
        user.save()

        from apps.operations.models import AuditEvent

        AuditEvent.objects.create(
            actor_reference="system",
            action="account_purge",
            object_reference=f"user:{user.pk}",
            reason=reason[:255],
            sanitized_metadata=stats,
        )
    return stats
