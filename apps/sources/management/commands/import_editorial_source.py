"""Import a file as a shared editorial source document.

Editorial corpus channel (spec: sources-and-rag.md): a saved document with
verified metadata and rights, visible to every account in retrieval. Unlike
import_source it takes no owner, applies no user upload limits and stores
work/edition metadata. The text is normalized unchanged; indexing is a
separate step (index_source).
"""

import os
import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.sources.models import SourceDocument, SourceEdition, SourceWork
from apps.sources.services import ExtractionError, extract_text, text_hash


class Command(BaseCommand):
    help = "Import a TXT/MD/PDF file as a shared editorial source document"

    def add_arguments(self, parser):
        parser.add_argument("--path", required=True, help="Path to the file on disk")
        parser.add_argument("--title", required=True, help="Human-readable title")
        parser.add_argument("--author", required=True, help="Author of the work")
        parser.add_argument("--work-title", required=True, help="Title of the work")
        parser.add_argument("--kind", default="primary", choices=["primary", "secondary"])
        parser.add_argument("--language", default="en")
        parser.add_argument("--canonical-url", default="", help="Canonical URL of the work")
        parser.add_argument("--edition", default="", help="Edition description")
        parser.add_argument("--translator", default="", help="Translator of this edition")
        parser.add_argument("--rights", default="allowed", help="rights_state: allowed or unknown")
        parser.add_argument("--edition-url", default="", help="URL the file was retrieved from")
        parser.add_argument("--retrieved-at", default="", help="Retrieval date, YYYY-MM-DD")

    def handle(self, *args, **options):
        path = options["path"]
        if not os.path.isfile(path):
            raise CommandError(f"Файл не найден: {path}")
        if options["rights"] not in ("allowed", "unknown"):
            raise CommandError("--rights: допустимы allowed и unknown")
        with open(path, "rb") as handle:
            data = handle.read()
        try:
            normalized = extract_text(os.path.basename(path), data)
        except ExtractionError as exc:
            raise CommandError(f"Импорт отклонён ({exc.code}): {exc}") from exc
        content_hash = text_hash(normalized)
        existing = SourceDocument.objects.filter(
            owner__isnull=True, origin=SourceDocument.Origin.EDITORIAL, content_hash=content_hash
        ).first()
        if existing:
            self.stdout.write(f"Дубликат по hash, существующий документ: {existing.pk}")
            return
        work = SourceWork.objects.create(
            author=options["author"][:200],
            title=options["work_title"][:300],
            kind=options["kind"],
            language=options["language"][:8],
            canonical_url=options["canonical_url"] or None,
        )
        edition = SourceEdition.objects.create(
            work=work,
            edition=options["edition"][:200],
            translator=options["translator"][:200],
            language=options["language"][:8],
            rights=options["rights"][:200],
            url=options["edition_url"] or None,
            retrieved_at=options["retrieved_at"] or None,
        )
        document = SourceDocument.objects.create(
            owner=None,
            edition=edition,
            lineage_id=uuid.uuid4(),
            origin=SourceDocument.Origin.EDITORIAL,
            visibility=SourceDocument.Visibility.EDITORIAL,
            rights_state=options["rights"],
            content_hash=content_hash,
            normalized_text=normalized,
            processing_state=SourceDocument.Processing.UPLOADED,
            metadata_json={"filename": os.path.basename(path)[:255], "title": options["title"][:255]},
        )
        self.stdout.write(self.style.SUCCESS(f"Редакционный документ создан: {document.pk}"))
