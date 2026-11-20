"""Put a book on the editorial shelf from an edition manifest.

The manifest (`content/sources/<book>/edition.json`) is data: who wrote and
translated the book, which printing, where the digital text comes from and
under which licence, the sha256 of the source file that lies next to it,
the Russian-to-English query lexicon and the library texts that retell the
book. The importer refuses a file whose sha256 differs from the manifest.

One import writes the work, the edition, the document and its chunks with
locators in one transaction, without any network call. Embeddings are a
separate step (`index_source`), which keeps these chunks as they are.

A repeated import of the same file with the same extraction settings
creates nothing and touches no chunk or embedding; it only brings the
edition's metadata in line with the manifest.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from django.conf import settings
from django.db import transaction

from .models import SourceChunk, SourceDocument, SourceEdition, SourceWork
from .perseus import EXTRACTOR_VERSION, SourceFormatError, extract_perseus_tei
from .retrieval import STRUCTURED
from .services import text_hash
from .structured import build_chunks, size_distribution

FORMATS = {"perseus-tei-bekker": extract_perseus_tei}

_WORK_FIELDS = ("author", "title", "author_ru", "title_ru", "kind", "language", "canonical_url")
_EDITION_FIELDS = (
    "edition", "translator", "language", "rights", "url", "retrieved_at", "series",
    "first_published_year", "printing_year", "digital_publisher", "license", "license_url",
)


class ShelfImportError(Exception):
    """The import cannot proceed; the message is for the operator."""


@dataclass
class ImportReport:
    document_id: object = None
    created: bool = False
    dry_run: bool = False
    edition_changed: list = field(default_factory=list)
    supersedes_set: object = None
    notes: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    sizes: dict = field(default_factory=dict)
    chunks: int = 0


def _require(mapping, keys, where):
    missing = [key for key in keys if not mapping.get(key)]
    if missing:
        raise ShelfImportError(f"В манифесте не заполнено ({where}): {', '.join(missing)}")


def load_manifest(path: str) -> dict:
    if not os.path.isfile(path):
        raise ShelfImportError(f"Манифест не найден: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ShelfImportError(f"Манифест не прочитан: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ShelfImportError("Манифест должен быть объектом JSON")
    _require(manifest, ("key", "title", "work", "edition", "source", "locator"), "верхний уровень")
    _require(manifest["work"], ("key", "author", "title"), "work")
    _require(manifest["edition"], ("translator", "license", "rights"), "edition")
    _require(manifest["source"], ("file", "sha256", "format"), "source")
    _require(manifest["locator"], ("work",), "locator")
    if manifest["source"]["format"] not in FORMATS:
        raise ShelfImportError(f"Неизвестный формат источника: {manifest['source']['format']}")
    if manifest["edition"]["rights"] not in ("allowed", "unknown"):
        raise ShelfImportError("edition.rights: допустимы allowed и unknown")
    for entry in manifest.get("query_lexicon", {}).get("ru", []):
        if not entry.get("en") or not (entry.get("stems") or entry.get("words")):
            raise ShelfImportError(f"Запись словаря без основ или без английских слов: {entry}")
    related = manifest.get("related_texts", [])
    if not isinstance(related, list) or not all(isinstance(slug, str) for slug in related):
        raise ShelfImportError("related_texts: ожидается список slug")
    return manifest


def read_source(manifest: dict, manifest_path: str) -> bytes:
    path = os.path.join(os.path.dirname(os.path.abspath(manifest_path)), manifest["source"]["file"])
    if not os.path.isfile(path):
        raise ShelfImportError(f"Файл источника не найден: {path}")
    with open(path, "rb") as handle:
        data = handle.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != manifest["source"]["sha256"]:
        raise ShelfImportError(
            f"sha256 файла не совпал с манифестом: в файле {actual}, в манифесте {manifest['source']['sha256']}"
        )
    return data


def _signature() -> dict:
    return {
        "extractor": EXTRACTOR_VERSION,
        "chunk_words_min": settings.SOURCE_CHUNK_WORDS_MIN,
        "chunk_words_max": settings.SOURCE_CHUNK_WORDS_MAX,
    }


def _retrieved_at(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ShelfImportError(f"edition.retrieved_at: ожидается ГГГГ-ММ-ДД, получено {value!r}") from exc


def _apply(instance, values: dict) -> list:
    changed = [name for name, value in values.items() if getattr(instance, name) != value]
    for name in changed:
        setattr(instance, name, values[name])
    return changed


def _sync_edition(manifest: dict):
    """Find the work and the edition by their manifest keys or create them."""
    work_values = {name: manifest["work"].get(name, "") for name in _WORK_FIELDS}
    work_values["kind"] = work_values["kind"] or "primary"
    work_values["language"] = work_values["language"] or "en"
    work_values["canonical_url"] = work_values["canonical_url"] or None
    work = SourceWork.objects.filter(key=manifest["work"]["key"]).first() or SourceWork(key=manifest["work"]["key"])
    changed = [f"work.{name}" for name in _apply(work, work_values)]
    if changed or work.pk is None:
        work.save()

    source = manifest["edition"]
    values = {name: source.get(name, "") for name in _EDITION_FIELDS}
    values["language"] = values["language"] or "en"
    values["url"] = values["url"] or None
    values["retrieved_at"] = _retrieved_at(source.get("retrieved_at"))
    values["first_published_year"] = source.get("first_published_year")
    values["printing_year"] = source.get("printing_year")
    values["source_sha256"] = manifest["source"]["sha256"]
    values["query_lexicon"] = manifest.get("query_lexicon", {})
    values["related_texts"] = manifest.get("related_texts", [])
    edition = SourceEdition.objects.filter(key=manifest["key"]).first()
    created = edition is None
    if created:
        edition = SourceEdition(key=manifest["key"], work=work)
    elif edition.work_id != work.pk:
        raise ShelfImportError(f"Издание {manifest['key']} уже принадлежит другой работе")
    edition_changed = _apply(edition, values)
    if created or edition_changed:
        edition.save()
    return edition, ([] if created else changed + edition_changed)


def _resolve_supersedes(value):
    if not value:
        return None
    try:
        document_id = uuid.UUID(str(value))
    except ValueError as exc:
        raise ShelfImportError(f"--supersedes: некорректный UUID: {value}") from exc
    document = SourceDocument.objects.filter(id=document_id).first()
    if document is None:
        raise ShelfImportError(f"--supersedes: документ {document_id} не найден")
    if document.owner_id is not None or document.visibility != SourceDocument.Visibility.EDITORIAL:
        raise ShelfImportError(f"--supersedes: документ {document_id} не редакционный, заменять его нельзя")
    return document


def _ancestors(document) -> set:
    seen = set()
    current = document.supersedes
    while current is not None and current.pk not in seen:
        seen.add(current.pk)
        current = current.supersedes
    return seen


def _link(document, old, report):
    """Mark `old` as replaced by `document`; a repeated request changes nothing."""
    if old is None:
        return
    if old.pk == document.pk:
        raise ShelfImportError("--supersedes: документ не может заменять сам себя")
    if document.pk in _ancestors(old):
        raise ShelfImportError(f"--supersedes: документ {old.pk} сам заменяет этот документ")
    if document.supersedes_id == old.pk or old.pk in _ancestors(document):
        report.notes.append(f"Документ {old.pk} уже заменён этим изданием")
        return
    if document.supersedes_id is not None:
        raise ShelfImportError(
            f"Документ {document.pk} уже заменяет {document.supersedes_id}; "
            f"второй заменяемый документ ({old.pk}) указать нельзя"
        )
    document.supersedes = old
    document.save(update_fields=["supersedes"])
    report.supersedes_set = old.pk


def import_shelf_book(manifest_path: str, *, supersedes=None, dry_run: bool = False) -> ImportReport:
    manifest = load_manifest(manifest_path)
    data = read_source(manifest, manifest_path)
    signature = _signature()
    report = ImportReport(dry_run=dry_run)

    existing = None
    edition = SourceEdition.objects.filter(key=manifest["key"]).first()
    if edition is not None:
        for candidate in edition.documents.order_by("-version"):
            metadata = candidate.metadata_json or {}
            if metadata.get("source_sha256") == manifest["source"]["sha256"] and metadata.get("extraction") == signature:
                existing = candidate
                break

    if existing is not None:
        report.document_id = existing.pk
        report.chunks = existing.chunks.count()
        report.stats = (existing.metadata_json or {}).get("stats", {})
        if dry_run:
            _resolve_supersedes(supersedes)
            return report
        with transaction.atomic():
            _, report.edition_changed = _sync_edition(manifest)
            _link(existing, _resolve_supersedes(supersedes), report)
        return report

    try:
        extraction = FORMATS[manifest["source"]["format"]](data)
    except SourceFormatError as exc:
        raise ShelfImportError(f"Источник не разобран: {exc}") from exc
    chunks = build_chunks(
        extraction.books,
        work=manifest["locator"]["work"],
        min_words=signature["chunk_words_min"],
        max_words=signature["chunk_words_max"],
    )
    report.stats = extraction.stats
    report.sizes = size_distribution(chunks, signature["chunk_words_min"], signature["chunk_words_max"])
    report.chunks = len(chunks)
    if extraction.stats["unknown_elements"]:
        report.notes.append(f"Неизвестные элементы разметки (текст сохранён): {extraction.stats['unknown_elements']}")
    normalized = extraction.text
    content_hash = text_hash(normalized)

    if dry_run:
        _resolve_supersedes(supersedes)
        return report

    with transaction.atomic():
        edition, report.edition_changed = _sync_edition(manifest)
        old = _resolve_supersedes(supersedes)
        previous = edition.documents.order_by("-version").first()
        same_text = edition.documents.filter(content_hash=content_hash).first()
        if same_text is not None:
            raise ShelfImportError(
                f"Текст совпадает с документом {same_text.pk}, но настройки извлечения другие "
                f"(было {same_text.metadata_json.get('extraction')}, сейчас {signature}). "
                "Второй документ с тем же текстом в одной линии версий не создаётся."
            )
        document = SourceDocument.objects.create(
            owner=None,
            edition=edition,
            lineage_id=previous.lineage_id if previous else uuid.uuid4(),
            version=previous.version + 1 if previous else 1,
            # A new extraction of the same edition replaces the previous one.
            supersedes=previous,
            origin=SourceDocument.Origin.EDITORIAL,
            visibility=SourceDocument.Visibility.EDITORIAL,
            rights_state=manifest["edition"]["rights"],
            content_hash=content_hash,
            normalized_text=normalized,
            processing_state=SourceDocument.Processing.UPLOADED,
            metadata_json={
                "title": manifest["title"][:255],
                "filename": manifest["source"]["file"][:255],
                "chunking": STRUCTURED,
                "format": manifest["source"]["format"],
                "manifest_key": manifest["key"],
                "source_sha256": manifest["source"]["sha256"],
                "source_commit": manifest["source"].get("commit", ""),
                "extraction": signature,
                "stats": extraction.stats,
            },
        )
        SourceChunk.objects.bulk_create(
            [
                SourceChunk(
                    document=document,
                    ordinal=chunk.ordinal,
                    text=chunk.text,
                    token_count=chunk.words,
                    content_hash=text_hash(chunk.text),
                    locator_json=chunk.locator,
                )
                for chunk in chunks
            ],
            batch_size=200,
        )
        _link(document, old, report)
    report.document_id = document.pk
    report.created = True
    return report
