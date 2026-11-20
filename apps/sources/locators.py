"""Locators for the reader: from `SourceChunk.locator_json` to `Politics 1.1253a`.

A chunk of a shelf book knows its book, the Bekker pages it covers and the
offset at which each page starts inside its text (`segments`, written by
import_shelf_book). A chunk without a locator gives an empty string.
"""

import re

_SPACE = re.compile(r"\s+")


def _place(locator: dict, first: str, last: str) -> str:
    pages = first if first == last else f"{first}–{last}"
    book = locator.get("book")
    return f"{book}.{pages}" if book else pages


def _reference(locator: dict, first: str, last: str) -> str:
    return f"{locator.get('work', '')} {_place(locator, first, last)}".strip()


def format_place(locator: dict) -> str:
    """`1.1252b–1253a`: the place of a whole chunk without the work's name."""
    if not locator or not locator.get("bekker_start"):
        return ""
    return _place(locator, locator["bekker_start"], locator.get("bekker_end") or locator["bekker_start"])


def span_place(locator: dict, start: int, end: int) -> str:
    """`1.1253a` for the characters [start, end) of a chunk text: the place
    without the work's name (`1.1252b–1253a` across a page turn). Empty
    string if the chunk has no page marks."""
    if not locator or not locator.get("segments") or end <= start:
        return ""
    return _place(locator, page_at(locator, start), page_at(locator, end - 1))


def format_locator(locator: dict) -> str:
    """`Politics 1.1253a`, or `Politics 1.1252b–1253a` for a chunk on two pages."""
    if not locator or not locator.get("bekker_start"):
        return ""
    return _reference(locator, locator["bekker_start"], locator.get("bekker_end") or locator["bekker_start"])


def page_at(locator: dict, offset: int) -> str:
    """Bekker page of the character at `offset` of the chunk text."""
    page = ""
    for segment in (locator or {}).get("segments", []):
        if segment["start"] > offset:
            break
        page = segment["bekker"]
    return page


def find_quote(text: str, quote: str):
    """(start, end) of a verbatim quotation in a chunk text, or None.

    Spaces and line breaks in the quotation are not significant; letters,
    punctuation and case are.
    """
    words = _SPACE.sub(" ", quote).strip()
    if not words:
        return None
    start = text.find(words)
    if start < 0:
        return None
    return start, start + len(words)


def locate_quote(text: str, locator: dict, quote: str) -> str:
    """Locator of a verbatim quotation: the exact page, not the chunk's range.

    `Politics 1.1253a`; a quotation that crosses a page turn gives
    `Politics 1.1252b–1253a`. Empty string if the quotation is not in the
    text word for word or the chunk has no locator.
    """
    span = find_quote(text, quote)
    if span is None or not locator or not locator.get("segments"):
        return ""
    return _reference(locator, page_at(locator, span[0]), page_at(locator, span[1] - 1))
