"""Clean English text with Bekker pages from a Perseus TEI translation.

Input: a Perseus/EpiDoc file laid out as
`div[@subtype='book']` -> `div[@subtype='bekker_page']` (for example
Rackham's Politics, `tlg0086.tlg035.perseus-eng2.xml`). Output: one clean
text per book plus the character offset at which every Bekker page starts.

What is dropped: `<note>` with everything inside it, every element marked
`xml:lang="grc"`, and markup that carries no text (`<milestone>`). The text
after a dropped element (its tail) is kept. `<gap>` becomes " … ".
`<q rend="single|double">` gets its quotation marks back: the file keeps
them in the attribute, not in the text.

Where a page starts: at `<milestone unit="page" n="1285b"/>` when the page
has one, otherwise at the start of its `div`. The file cuts `div`s at the
nearest clause break, so in about two dozen places the `div` boundary is up
to ~100 characters away from the page milestone; the milestone is the Bekker
page (it sits next to `line n="1"`), the `div` is only a container.

No network, no new dependencies: xml.etree.ElementTree. A file with a DTD or
entity declarations is refused rather than expanded.
"""

import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field

EXTRACTOR_VERSION = 1

TEI = "{http://www.tei-c.org/ns/1.0}"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

_BEKKER_RE = re.compile(r"^\d{3,4}[ab]$")
_GREEK_RE = re.compile(r"[\u0370-\u03ff\u1f00-\u1fff]")
# Spaced dots in the transcription mark a lacuna, exactly like <gap>.
_DOTS_RE = re.compile(r"\s*\.(?:\s*\.){2,}\s*")
_ELLIPSIS = " … "

# Private-use sentinels: they let the whitespace rules tell an opening or
# closing quotation mark of <q> from an apostrophe (the file uses ’ for
# both) and mark the join of a paragraph that a page break cut in two.
_Q_OPEN_SINGLE, _Q_CLOSE_SINGLE = "\ue001", "\ue002"
_Q_OPEN_DOUBLE, _Q_CLOSE_DOUBLE = "\ue003", "\ue004"
_JOIN = "\ue005"
# A line or page milestone. The file often has no space around it
# ("that<milestone/>wealth", "individually.<milestone/>For"): there the
# milestone itself is the word boundary.
_MARK = "\ue006"
_MARK_AS_SPACE = re.compile(f"(?<=[^\\W_]|[.,;:?!]){_MARK}+(?=[^\\W\\d_])")
_SENTINELS = {
    _Q_OPEN_SINGLE: "‘",
    _Q_CLOSE_SINGLE: "’",
    _Q_OPEN_DOUBLE: "“",
    _Q_CLOSE_DOUBLE: "”",
}
_Q_OPEN = _Q_OPEN_SINGLE + _Q_OPEN_DOUBLE
_Q_CLOSE = _Q_CLOSE_SINGLE + _Q_CLOSE_DOUBLE

# Elements whose text is part of the translation.
_INLINE = {"title", "placeName", "emph"}
_BLOCK = {"quote", "l"}


class SourceFormatError(Exception):
    """The file is not the structure this extractor understands."""


@dataclass
class BookText:
    number: int
    text: str
    # (Bekker page, offset of its first character in `text`), in order.
    pages: list
    # Offsets at which an indented paragraph of the print starts.
    paragraph_starts: list


@dataclass
class Extraction:
    books: list
    stats: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(book.text for book in self.books)


class _Builder:
    """Raw text of one book with the page and paragraph of every character."""

    def __init__(self):
        self.parts = []
        self.page_of = []
        self.paragraph_of = []
        self.page_names = []
        self.page_divs = []
        self.page_raw = {}
        self.div_raw = {}
        self.paragraph = 0

    def add(self, text):
        if not text:
            return
        text = text.replace("（", "(").replace("）", ")")
        text = _DOTS_RE.sub(_ELLIPSIS, text)
        if not self.page_names:
            if text.strip():
                raise SourceFormatError("Текст до первой страницы Беккера")
            return
        self.parts.append(text)
        self.page_of.extend([len(self.page_names) - 1] * len(text))
        self.paragraph_of.extend([self.paragraph] * len(text))

    def mark_page(self, name):
        if self.page_names and self.page_names[-1] == name:
            return
        self.page_names.append(name)
        self.page_raw[name] = len(self.page_of)

    def mark_paragraph(self):
        self.paragraph += 1

    def open_page_div(self, name, has_milestone):
        self.page_divs.append(name)
        # Without a milestone of its own the page starts with its div; the
        # first page of a book starts with the book in any case.
        if not has_milestone or not self.page_names:
            self.mark_page(name)
        self.div_raw[name] = len(self.page_of)

    def moved_pages(self):
        """Pages whose milestone is not where their div starts."""
        raw = "".join(self.parts)
        moved = []
        for name in self.page_names:
            low, high = sorted((self.page_raw[name], self.div_raw.get(name, self.page_raw[name])))
            if raw[low:high].replace(_JOIN, "").replace(_MARK, "").strip():
                moved.append(name)
        return moved


def _local(element) -> str:
    return element.tag.replace(TEI, "") if isinstance(element.tag, str) else ""


def _is_page_milestone(element) -> bool:
    # One page of the file (1254b) is marked unit="section": the name decides.
    return bool(_BEKKER_RE.match(element.get("n") or ""))


def _walk(element, builder, stats, milestone_pages):
    tag = _local(element)
    if tag == "note":
        stats["notes_removed"] += 1
        stats["greek_removed"] += sum(1 for child in element.iter() if child.get(XML_LANG) == "grc")
        if any(_is_page_milestone(child) for child in element.iter(TEI + "milestone")):
            raise SourceFormatError("Метка страницы внутри сноски")
        return
    if element.get(XML_LANG) == "grc":
        stats["greek_removed"] += 1
        stats["greek_in_text"] += 1
        return
    if tag == "milestone":
        if builder.page_names:
            builder.add(_MARK)
        if _is_page_milestone(element):
            builder.mark_page(element.get("n"))
        return
    if tag == "gap":
        stats["gaps"] += 1
        builder.add(_ELLIPSIS)
        return

    close = ""
    if tag == "div":
        if element.get("subtype") == "bekker_page":
            name = element.get("n") or ""
            if not _BEKKER_RE.match(name):
                raise SourceFormatError(f"Непонятная страница Беккера: {name!r}")
            builder.open_page_div(name, name in milestone_pages)
        builder.add(" ")
        close = " "
    elif tag == "p":
        if "indent" in (element.get("rend") or ""):
            builder.mark_paragraph()
            builder.add(" ")
        else:
            builder.add(_JOIN)
        close = " "
    elif tag in _BLOCK:
        builder.add(" ")
        close = " "
    elif tag == "q":
        double = element.get("rend") == "double"
        builder.add(_Q_OPEN_DOUBLE if double else _Q_OPEN_SINGLE)
        close = _Q_CLOSE_DOUBLE if double else _Q_CLOSE_SINGLE
    elif tag not in _INLINE:
        stats["unknown_elements"][tag] = stats["unknown_elements"].get(tag, 0) + 1

    builder.add(element.text)
    for child in element:
        _walk(child, builder, stats, milestone_pages)
        builder.add(child.tail)
    builder.add(close)


def _delete(state, pattern):
    """Delete group 1 of every match, keeping the parallel arrays in step."""
    text, pages, paragraphs = state
    keep = bytearray(b"\x01") * len(text)
    found = False
    for match in pattern.finditer(text):
        start, end = match.span(1)
        if end > start:
            keep[start:end] = bytes(end - start)
            found = True
    if not found:
        return state
    return (
        "".join(char for char, kept in zip(text, keep) if kept),
        [page for page, kept in zip(pages, keep) if kept],
        [paragraph for paragraph, kept in zip(paragraphs, keep) if kept],
    )


_MARK_RULE = re.compile(f"({_MARK}+)")
_RULES = [
    # A paragraph cut by a page break continues without a space after a dash.
    re.compile(f"—(\\s*{_JOIN}\\s*)"),
    # Greek cut out of the translation may leave its brackets behind.
    re.compile(r"(\(\s*\))"),
    re.compile(r"(?<=\s)(\s+)"),
    re.compile(r"^(\s+)"),
    re.compile(r"(\s+)$"),
    # A removed note leaves "Republic ?" and "Cyrene ;"; "4 : 3" is a ratio.
    re.compile(r"(\s)(?=[,;?!.)”" + _Q_CLOSE + r"])"),
    re.compile(r"(?<!\d)(\s)(?=:)"),
    re.compile(r"(?<=[(“" + _Q_OPEN + r"])(\s)"),
]


def _normalize(builder):
    text = "".join(builder.parts)
    state = (text, builder.page_of, builder.paragraph_of)
    text, joined_words = _MARK_AS_SPACE.subn(lambda match: " " * len(match.group()), text)
    state = _delete((text, builder.page_of, builder.paragraph_of), _MARK_RULE)
    state = _delete(state, _RULES[0])
    text = re.sub(r"\s", " ", state[0].replace(_JOIN, " "))
    state = (text, state[1], state[2])
    for rule in _RULES[1:]:
        state = _delete(state, rule)
    text = state[0]
    for sentinel, mark in _SENTINELS.items():
        text = text.replace(sentinel, mark)
    return text, state[1], state[2], joined_words


def _starts(text, labels):
    """Offset of the first non-space character of every run of one label."""
    starts = []
    previous = None
    pending = None
    for offset, label in enumerate(labels):
        if label != previous:
            pending = label
            previous = label
        if pending is not None and text[offset] != " ":
            starts.append((pending, offset))
            pending = None
    return starts


def _extract_book(book, stats):
    try:
        number = int(book.get("n"))
    except (TypeError, ValueError) as exc:
        raise SourceFormatError(f"Непонятный номер книги: {book.get('n')!r}") from exc
    milestone_pages = {
        element.get("n") for element in book.iter(TEI + "milestone") if _is_page_milestone(element)
    }
    builder = _Builder()
    _walk(book, builder, stats, milestone_pages)
    text, page_of, paragraph_of, joined_words = _normalize(builder)
    stats["milestones_as_spaces"] += joined_words

    if builder.page_names != builder.page_divs:
        raise SourceFormatError(f"Книга {number}: порядок меток страниц не совпал с порядком разделов")
    if len(set(builder.page_names)) != len(builder.page_names):
        raise SourceFormatError(f"Книга {number}: страница Беккера встречается дважды")
    pages = [(builder.page_names[index], offset) for index, offset in _starts(text, page_of)]
    if [name for name, _ in pages] != builder.page_names:
        raise SourceFormatError(f"Книга {number}: страница без текста")
    if not pages or pages[0][1] != 0:
        raise SourceFormatError(f"Книга {number}: текст начинается не с начала страницы")
    paragraph_starts = [offset for _, offset in _starts(text, paragraph_of)]
    stats["pages"] += len(pages)
    stats["pages_without_milestone"] += [f"{number}.{name}" for name in builder.page_names if name not in milestone_pages]
    stats["pages_moved_from_div"] += [f"{number}.{name}" for name in builder.moved_pages()]
    return BookText(number=number, text=text, pages=pages, paragraph_starts=paragraph_starts)


def extract_perseus_tei(data: bytes) -> Extraction:
    """Parse a Perseus TEI translation into clean per-book text with pages."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise SourceFormatError("В файле есть DTD или объявления сущностей — такой файл не разбирается")
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise SourceFormatError(f"XML не разобран: {exc}") from exc
    books = [div for div in root.iter(TEI + "div") if div.get("subtype") == "book"]
    if not books:
        raise SourceFormatError("В файле нет div[@subtype='book']")
    stats = {
        "notes_removed": 0,
        "greek_removed": 0,
        # Of them outside notes, i.e. cut out of the translation itself.
        "greek_in_text": 0,
        "gaps": 0,
        # Milestones that were the only boundary between two words.
        "milestones_as_spaces": 0,
        "pages": 0,
        # Pages that start with their div because they have no milestone.
        "pages_without_milestone": [],
        # Pages whose milestone stands apart from the start of their div.
        "pages_moved_from_div": [],
        "unknown_elements": {},
    }
    extracted = [_extract_book(book, stats) for book in books]
    numbers = [book.number for book in extracted]
    if numbers != sorted(set(numbers)):
        raise SourceFormatError(f"Книги идут не по порядку: {numbers}")
    for book in extracted:
        if _GREEK_RE.search(book.text):
            raise SourceFormatError(f"Книга {book.number}: в очищенном тексте остались греческие буквы")
        if not book.text:
            raise SourceFormatError(f"Книга {book.number}: пустой текст")
    stats["books"] = len(extracted)
    stats["characters"] = sum(len(book.text) for book in extracted)
    stats["words"] = sum(len(book.text.split()) for book in extracted)
    return Extraction(books=extracted, stats=stats)
