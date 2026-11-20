# Aristotle, Politics — translated by H. Rackham (Perseus Digital Library)

Files in this folder:

- `tlg0086.tlg035.perseus-eng2.xml` — the source text, unchanged, byte for byte.
- `edition.json` — the edition manifest read by `manage.py import_shelf_book`
  (data of this project, not part of the Perseus text).

## Source and licence

Aristotle. *Politics*. Translated by H. Rackham. Aristotle in 23 Volumes,
Vol. 21. Loeb Classical Library 264. Cambridge, MA: Harvard University Press;
London: William Heinemann Ltd. First published 1932; the digital text follows
the 1944 printing.

Digital text: **Perseus Digital Library**, <https://www.perseus.tufts.edu/>
(Perseus Project, Tufts University).

- Repository: <https://github.com/PerseusDL/canonical-greekLit>
- File: `data/tlg0086/tlg035/tlg0086.tlg035.perseus-eng2.xml`
- Commit: `8375d5b7f542fd28790d05895c380d9c2620ca9d` (2026-11-12, the last
  commit that changed this file when it was retrieved on 2026-11-20)
- Exact address:
  <https://github.com/PerseusDL/canonical-greekLit/blob/8375d5b7f542fd28790d05895c380d9c2620ca9d/data/tlg0086/tlg035/tlg0086.tlg035.perseus-eng2.xml>
- CTS URN: `urn:cts:greekLit:tlg0086.tlg035.perseus-eng2`
- sha256: `c4546ed5e2ab75a1ab33f2c84a32cec53fa98a537dfc6714882e5859d638f92b`
  (775 866 bytes)

Licence: Creative Commons Attribution-ShareAlike 4.0 International
(**CC BY-SA 4.0**), <https://creativecommons.org/licenses/by-sa/4.0/>.

The file's own statement of terms (`teiHeader/fileDesc/publicationStmt/availability`):

> Available under a Creative Commons Attribution-ShareAlike (CC BY-SA) license.
> Reuse must credit the Perseus Digital Library in a standard bibliographic
> format, including a link to https://www.perseus.tufts.edu/. Each text reused
> should be individually identified and linked to its source GitHub repository
> whenever possible, either in its citation or in an accompanying source list.
> All corrections must be offered to the Perseus Digital Library.

Line shown to a reader wherever the book is named or quoted:

> Aristotle, Politics. Translated by H. Rackham. Loeb Classical Library (first
> published 1932). Digital text: Perseus Digital Library, from the 1944
> printing. CC BY-SA 4.0.

## What this project changes

The XML file is not edited. The text that the reader's questions are answered
from is derived from it by `apps/sources/perseus.py` and is therefore an
adaptation under the same licence, CC BY-SA 4.0. Changes made in the derived
text:

- the translator's footnotes are left out: 587 `<note>` elements, with
  everything inside them;
- Greek is left out: 69 elements with `xml:lang="grc"`, all of them inside the
  footnotes (none in the translation itself);
- markup is removed; `<q>` becomes ‘…’ or “…” as its `rend` says; the 5
  `<gap>` elements and spaced dots become “ … ”; spaces before punctuation left
  by removed notes are closed up;
- a `<milestone>` standing between two words with no space (154 places, for
  example `that<milestone/>wealth`) is read as the space between them;
- misprints of the digital text (`educationand`, `necessarys o that`,
  `for Ihand it on`, `master,the`, `tochastity`) are kept as they are.

No correction of the Perseus text is made here, so there is nothing to offer
back; the misprints above can be reported to the repository.

## Bekker pages

The text is cut into 189 containers `div[@subtype='bekker_page']`, and the
page itself is marked by `<milestone unit="page" n="1285b"/>` standing next to
line 1. In 22 places the container is cut at the nearest clause break, up to
about a hundred characters away from the milestone: 1.1258b, 1.1259a,
1.1259b, 2.1266b, 2.1271b, 2.1272b, 2.1273b, 2.1274a, 3.1278b, 3.1283b,
3.1285b, 4.1290b, 5.1302a, 5.1303b, 5.1305a, 5.1306b, 5.1311b, 6.1322a,
7.1329b, 7.1332a, 7.1334b, 7.1336a. This project takes the milestone as the
start of the page and the container only as a container. Page 1254b is marked
`unit="section"` instead of `unit="page"`; it is read as a page by its name.
Pages at a book boundary (for example 1260b) occur in two books; a locator
always names the book: `Politics 1.1260b`, `Politics 2.1260b`.
