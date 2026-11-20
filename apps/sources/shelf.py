"""The editorial shelf as data: which books a reader can ask about.

A book is on the shelf when its edition has an editorial document that is
READY and has not been replaced by another READY document. Nothing here is
a hard-coded list: put a book on the shelf with import_shelf_book +
index_source and it appears; replace its document and the old one leaves.
"""

from dataclasses import dataclass

from .models import SourceDocument, SourceEdition
from .retrieval import superseded_document_ids


@dataclass(frozen=True)
class ShelfBook:
    edition: SourceEdition
    document: SourceDocument
    author: str
    title: str
    label: str  # "Aristotle, Politics"
    attribution: str  # full line: translator, series, digital text, licence
    author_ru: str  # "" if the manifest gives no Russian name
    title_ru: str
    label_ru: str  # "Аристотель, «Политика»" or ""
    related_texts: tuple  # slugs of library texts that retell the book
    # Addresses for the attribution ("" if the manifest gives none): the
    # source file in its repository, the licence, the digital publisher.
    source_url: str = ""
    license_url: str = ""
    digital_publisher_url: str = ""
    # The same as (label, url) pairs ready for a page, http(s) only.
    links: tuple = ()


def _web(url) -> str:
    """An address a page may link to; anything else becomes ""."""
    url = (url or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def _links(edition) -> tuple:
    pairs = (
        (edition.digital_publisher or "Digital text", _web(edition.digital_publisher_url)),
        ("Source file", _web(edition.url)),
        (edition.license or "Licence", _web(edition.license_url)),
    )
    return tuple((label, url) for label, url in pairs if url)


def shelf_books() -> list:
    """Books on the shelf, ordered by author and title; one entry per edition."""
    hidden = superseded_document_ids()
    documents = (
        SourceDocument.objects.filter(
            owner__isnull=True,
            visibility=SourceDocument.Visibility.EDITORIAL,
            processing_state=SourceDocument.Processing.READY,
            edition__isnull=False,
        )
        .exclude(id__in=hidden)
        .select_related("edition__work")
        .order_by("edition__work__author", "edition__work__title", "edition_id", "-version", "-created_at")
    )
    books = []
    seen = set()
    for document in documents:
        if document.edition_id in seen:
            continue
        seen.add(document.edition_id)
        edition = document.edition
        work = edition.work
        books.append(
            ShelfBook(
                edition=edition,
                document=document,
                author=work.author,
                title=work.title,
                label=work.label,
                attribution=edition.attribution(),
                author_ru=work.author_ru,
                title_ru=work.title_ru,
                label_ru=work.label_ru,
                related_texts=tuple(edition.related_texts or ()),
                source_url=_web(edition.url),
                license_url=_web(edition.license_url),
                digital_publisher_url=_web(edition.digital_publisher_url),
                links=_links(edition),
            )
        )
    return books


def book_for_text(slug: str):
    """The shelf book that a library text retells, or None.

    Only a book that is really on the shelf is returned, so a page may offer
    "ask about this book" exactly when a question can be answered from it.
    """
    for book in shelf_books():
        if slug in book.related_texts:
            return book
    return None
