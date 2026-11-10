"""User data export (stage 9).

Implements docs/spec/docs/auth-privacy-security.md "Экспорт и удаление":
own texts, versions, messages, words, favorites, own documents and their
metadata, sources of results, credit history. No other user's private data
and no secrets.
"""

from django.utils import timezone


def export_user_data(user) -> dict:
    """Build the full JSON export for one account."""
    from apps.ai.models import AIJob
    from apps.credits.models import CreditTransaction, Wallet
    from apps.library.models import Favorite, SavedWord
    from apps.sources.models import SourceChunk, SourceDocument
    from apps.workshop.models import Project

    wallet = Wallet.objects.filter(user=user).first()
    documents = []
    for document in SourceDocument.objects.filter(owner=user):
        documents.append(
            {
                "id": str(document.pk),
                "lineage_id": str(document.lineage_id),
                "version": document.version,
                "origin": document.origin,
                "visibility": document.visibility,
                "rights_state": document.rights_state,
                "verification_state": document.verification_state,
                "processing_state": document.processing_state,
                "metadata": document.metadata_json,
                "created_at": document.created_at.isoformat(),
                "text": document.normalized_text,
                "chunks": list(SourceChunk.objects.filter(document=document).values_list("text", flat=True)),
            }
        )
    data = {
        "export_version": "1",
        "generated_at": timezone.now().isoformat(),
        "account": {
            "login": user.login,
            "public_alias": user.public_alias,
            "created_at": user.created_at.isoformat(),
            "is_active": user.is_active,
        },
        "credits": {
            "total_credits": wallet.total_credits if wallet else 0,
            "reserved_credits": wallet.reserved_credits if wallet else 0,
                "transactions": [
                    dict(row, created_at=row["created_at"].isoformat())
                    for row in CreditTransaction.objects.filter(wallet__user=user).values(
                        "kind", "signed_amount", "operation_key", "reason", "created_at"
                    )
                ],
        },
        "documents": documents,
        "projects": [
            {
                "id": str(project.pk),
                "title": project.title,
                "mode": project.mode,
                "created_at": project.created_at.isoformat(),
                "revisions": [
                    {"payload": revision.payload, "created_at": revision.created_at.isoformat(), "content_hash": revision.content_hash}
                    for revision in project.revisions.all()
                ],
                "messages": [
                    dict(row, created_at=row["created_at"].isoformat())
                    for row in project.messages.filter(owner=user).values("role", "kind", "body", "created_at")
                ],
                "jobs": [
                    dict(row, created_at=row["created_at"].isoformat())
                    for row in AIJob.objects.filter(project=project, owner=user).values(
                        "kind", "status", "outcome", "error_code", "created_at"
                    )
                ],
            }
            for project in Project.objects.filter(owner=user)
        ],
        "favorites": [
            dict(row, created_at=row["created_at"].isoformat())
            for row in Favorite.objects.filter(user=user).values("publication__slug", "created_at")
        ],
        "words": [
            dict(row, created_at=row["created_at"].isoformat())
            for row in SavedWord.objects.filter(user=user).values("term", "translation_ru", "context_en", "created_at")
        ],
    }
    return data
