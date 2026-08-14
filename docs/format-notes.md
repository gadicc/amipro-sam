# Lotus Ami Pro SAM format notes

These notes distinguish observations from hypotheses. They describe enough of
the format to explain the parser; they are not a complete vendor specification.

## Evidence and provenance

Confirmed behavior was cross-checked among:

- 13 SAM documents and 108 standalone SDW graphics from a locally owned Ami Pro
  3.1 installation, inspected only for interoperability and never copied into
  this repository;
- a private, Git-ignored corpus of 384 SAM files, reported only as aggregate
  structure and sanitized identifiers;
- the UK National Archives [PRONOM x-fmt/191 record](https://www.nationalarchives.gov.uk/pronom/x-fmt/191);
- Ariya Hidayat's LGPL KOffice/KWord
  [Ami Pro filter and reverse-engineering notes](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/);
- Günter Born, *Das AMI Pro Dateiformat (Version 3.0/4.0)*, a detailed
  secondary reverse-engineering reference.

No proprietary source, documentation, or sample content was copied into the
implementation or fixtures.

## Confirmed container structure

A typical version-4 document is a mixed text/binary stream:

```text
[ver] / [sty] and metadata
repeated [tag] style records
frames, tables, page layout
[edoc]
paragraph text and inline commands
optional binary payloads and companion data
[Embedded]
object directory
zero-padded decimal directory offset
```

Records normally use CRLF and tab indentation. Section names repeat and their
indentation matters, so the header is not an INI file. All 13 vendor samples and
383 non-empty private samples use format version `4`. One private document has a
16-byte textual preamble before `[ver]`; its embedded offsets are relative to the
post-preamble stream.

The common charset record is:

```text
[charset]
    82
    ANSI (Windows, IBM CP 1252)
```

`82` is an Ami Pro identifier, not decimal code page 82. The human-readable
description confirms CP1252. Two vendor macro samples omit `[charset]`. The
current decoder honors BOMs and explicit code-page descriptions, accepts a user
override, otherwise defaults conservatively to CP1252, and preserves undecodable
bytes using Python's reversible `surrogateescape` representation.

## Unicode PDF preservation strategy

Unicode text is not inferred from the surveyed corpus alone. The parser also
supports declared legacy code pages and BOM-marked Unicode streams, so PDF
conversion must handle characters that do not occur in the locally inspected
documents. The implementation follows ReportLab's public
[Arabic/RTL integration guidance](https://docs.reportlab.com/rl-arabic/) and
[4.4 release notes](https://docs.reportlab.com/releases/notes/whats-new-44/),
while treating its shaping support as experimental rather than claiming a
complete Unicode layout engine.

The PDF renderer is reproducible and host-independent:

- it registers exactly four name-only-renamed DejaVu Sans 2.37 faces and one
  deterministic BMP subset of Noto Sans CJK SC 2.004, all loaded from package
  resources in a fixed order;
- it pins ReportLab 4.4.10, python-bidi 0.6.11, and uharfbuzz 0.55.0;
- a source font family is only an inert presentation hint; it never becomes a
  file, URL, or host-font lookup;
- LTR text is segmented into coalesced fixed-font spans. The current CJK face
  uses Simplified-Chinese default unified-Han forms and deliberately makes no
  claim for locale-specific, vertical, ruby, variation-sequence, or non-BMP
  typography;
- paragraphs containing strong Hebrew or Arabic characters use a bounded
  custom line flowable. It applies the public python-bidi algorithm and
  HarfBuzz shaping through ReportLab's canvas API, then writes each bounded,
  sanitized logical visual line as PDF `ActualText`. Inline font/style distinctions are flattened
  to the best-coverage DejaVu face and every scalar is checked against its cmap.
  Direction changes inside one whitespace-free token are visibly marked because
  ReportLab 4.4 cannot shape that token without reversing its LTR segment. Mixed
  RTL/CJK paragraphs and unmapped Hebrew/Arabic extensions are not claimed as
  supported and missing characters become visible replacements;
- U+2066 through U+2069 bidi isolates are not supported by python-bidi's public
  mapped algorithm and become U+FFFD. The older embedding/override controls are
  bounded and handled structurally. Lone surrogates, noncharacters, C0/C1
  controls other than tab/newline, and every scalar above U+FFFF also become
  visible U+FFFD. The BMP restriction avoids ReportLab 4.4's incorrect
  non-BMP `ToUnicode` mapping rather than emitting an apparently valid but
  unextractable glyph.

PDF `ActualText` is a standards-level preservation aid, not a promise about
every extraction library. Poppler uses it in the tested files, while some
versions of pypdf and pdfplumber expose the visually ordered glyph stream
instead. JSON, plain text, ODT, and DOCX therefore remain the preferred
logical-text outputs for downstream analysis of RTL documents.

Work is bounded before shaping and font subsetting. The source-work budgets are
65,536 code points per paragraph, 1,024 per unbroken token, 4,096 runs per
paragraph, 4,000,000 source text code points, 8,192 distinct source scalars, 64
consecutive combining marks, 4,096 bidi controls, and 4,096 fixed-font spans
per text unit. One bounded omission/replacement marker may be added beyond a
source budget. Reaching a ceiling emits that marker instead of silently dropping
the rest or expanding one marker per source
character. PDF font sizes are clamped to 72 points, output is capped at 128
pages and 64 MiB, and the encoded-byte cap remains a final backstop rather than
the first work-control boundary.

The installation set and parseable private regression set are all declared or
decoded as CP1252. Across them, renderer-facing text contains only the
U+0081-U+00FF non-ASCII range and no strong RTL, Greek, Cyrillic, CJK,
combining, or non-BMP characters. Unicode script coverage is therefore based
on documented font coverage and invented synthetic fixtures; it is not
misrepresented as corpus evidence. Exact font hashes, licenses, source commits,
coverage, and the deterministic CJK derivation recipe are in
`src/amipro_sam/assets/fonts/NOTICE.md`.

## Styles and measurements

`[tag]` records define named styles. Observed subrecords include `[fnt]`,
`[algn]`, `[spc]`, `[brk]`, `[line]`, `[spec]`, and `[nfmt]`. Font sizes and
layout dimensions use twips: 20 twips per point and 1,440 per inch. Packed style
colors use red in bits 0-7, green in 8-15, and blue in 16-23.

Confirmed character flag bits include bold, italic, underline, word underline,
and double underline. Confirmed alignment bits represent left, right, center, and
justified paragraphs. Break flags can request page/column breaks and paragraph
keep behavior; only the currently verified subset is rendered.

The public KOffice notes also identify the top-level style envelope as shortcut
key, following-style name, and two zero sentinels. The following style controls
what Ami Pro chooses after a break; it is not an inheritance parent. The same
notes place `[algn]` values in the order flags, unit, all-indent, first-line
position, and rest-lines position. The IR therefore stores the rest-lines value
as the left indent and the first-minus-rest delta as its renderer-relative
first-line indent. A nonzero all-indent value remains a semantic-loss diagnostic
because its both-side behavior is not yet reproduced.

Observed font flag bits `0xc000` are baseline class markers rather than visible
formatting. The common `[spc]` tail `1,100` is a structural sentinel plus default
text tightness. In contrast, spacing flags `0x10`/`0x20`, nondefault tightness,
and other output-affecting fields remain raw semantic diagnostics. Style
metadata diagnostics are not inserted into the document's body flow: JSON,
HTML's diagnostic appendix, and strict mode retain the loss accounting without
making thousands of pre-body warning paragraphs.

Unsupported, duplicate, incomplete, or malformed style subrecords remain in the
raw section model and receive an explicit semantic-loss diagnostic instead of
being mistaken for fully interpreted formatting. Nonempty fields after the
interpreted `[fnt]`, `[algn]`, and `[spc]` prefixes are likewise retained and
classified as opaque rather than silently treated as supported.

## Text stream

`[edoc]` begins the main text. Blank physical lines delimit paragraphs and a
lone `>` ends a text stream. Named styles appear as `@Style Name@`; `@@` is a
literal at sign.

Confirmed inline formatting:

| Command | Meaning |
|---|---|
| `<+!>` / `<-!>` | bold on/off |
| `<+">` / `<-">` | italic on/off |
| `<+#>` / `<-#>` | underline on/off |
| `<+)>` / `<-)>` | double underline on/off |
| `<+$>` / `<-$>` | word underline on/off |
| `<+&>` / `<-&>` | superscript on/off |
| `<+'>` / `<-'>` | subscript on/off |
| `<+%>` / `<-%>` | strikeout on/off |
| `<+@>`, `<+A>`, `<+B>`, `<+C>` | left, right, center, justify |
| `<:f...>` / `<:f>` | set/reset font |
| `<:S+-1>`, `<:S+-2>`, `<:S+-3>` | single, 1.5, double spacing |
| `<:s>` | nonprinting spelling state |

Confirmed literal escapes include `<<` for `<`, `<;>` for `>`, `<[>` for `[`,
`@@` for `@`, and `</R>` for an apostrophe. Two additional four-byte escape
families encode characters outside safe 7-bit ASCII.

Other observed commands cover tabs, indents, page breaks, anchors, tables,
dates/page numbers, bookmarks, fields, merge data, spelling state, notes,
headers/footers, and revisions. Multiline note, footnote, and header/footer
containers use their own standalone `>` close inside `[edoc]`; this must not be
mistaken for the outer document terminator. A close is standalone only when the
physical line contains `>` and whitespace. A line such as `>trailing` is text,
not a terminator.

Within text content, a blank physical line terminates a paragraph. Consecutive
nonblank physical lines are storage continuations and are concatenated without
inventing a space or line break; real files can split a word at that boundary.
The private corpus contains 2,529 such boundaries in 1,682 parsed paragraphs,
including 574 alphanumeric word splits across 133 files.

The private corpus contains 11,446 `<:#x,width>` forms across 299 files. In
8,860 corpus cases with usable page geometry, the second value is within five
twips of the body measure; two-column files reuse a half-width measure at
different x positions. Treating that second value as a left indent caused the
right-edge sliver: 6,641 paragraphs acquired left indents above three inches.
The parser now retains the two values as distinct twip fields. When the measure
matches the explicit body/cell width within three source-rounding twips, the
first value is applied as a first-line position and never as a whole-paragraph
left margin. A narrower region that fits the known container resolves to left
and right base margins. Other geometry keeps ordinary indentation and the
document receives one semantic `paragraph-region-reflowed` diagnostic. Reflowed
frames and automatically sized HTML cells deliberately do not borrow the page
body as a false container. Some observed column coordinates exceed the known
body by more than the tolerance, suggesting an unmodeled coordinate origin;
those safely fall back pending an Ami Pro rendering oracle.

All 750 observed `<:I...>` commands have four bounded numeric fields and a zero
fourth field. That establishes a canonical shape, not the meanings of the first
three values. They are therefore retained atomically in typed IR and diagnosed
as unapplied semantics without a body marker; malformed variants remain visible
and cannot partially mutate paragraph state. Compact font forms such as
`<:f240,Wingdings,>` apply their present size/family fields with an empty color
tail. A matched `:X~...` field close is nonprinting after the opener has already
produced its inert fallback; unmatched closes remain explicit.

An exact single `[revisions]` value of `0` is the observed no-revisions state.
Nonzero, additional, malformed, or duplicate revision records remain raw,
visible semantic losses. Empty `[elay]` terminators, a single bounded `[l1]`
value, and exact bounded `[frmname]` values are typed. `[l1]` is not used to
select renderer geometry because its indexing and scope are not yet proven.

If an unterminated `[edoc]` reaches a NUL-bearing physical line, recovery keeps
the readable stream through that line and resumes at a later appended
`[Embedded]` directory when present. Any intervening bytes that cannot be
interpreted as text remain represented by a bounded byte length, SHA-256, and
source span. This opaque-tail recovery is classified as content loss, so strict
mode rejects it instead of silently accepting the omission.

Inline commands are scanned in linear time and capped at 4,095 materialized
commands per paragraph, leaving room for an initial text run within the
4,096-run renderer boundary. Materialized runs across body text, frames, tables,
and layout streams also charge the shared document record budget (a built-in
ceiling of 1,000,000 that callers may lower). If either cap is exceeded,
surrounding text and one visible marker remain while the unmaterialized
formatting semantics receive a strict semantic-loss classification. Repeated
unterminated `<` syntax and undefined-style references are coalesced into
bounded diagnostics.

Born's reverse-engineering reference identifies `<:N...` as an annotation,
`<:F` as a footnote, `<:H...` as a header, and `<:h...` as a footer. Header and
footer flag bits 4 and 8 select odd/right and even/left pages, while bit 16
denotes odd/even variants; bits 1 and 2 distinguish footer and header in the
flag word. The parser represents all four as recursive IR objects, retains each
container's direct raw fragments plus the enclosing section's complete source
stream, and preserves malformed or unsupported metadata with diagnostics.
Nested header/footer containers are retained but diagnosed because the
documented grammar forbids them.

The inspected corpora contain ten annotation records and one inline header
record. They contain no inline footnote or footer records, so inline footnote
support is specification-backed and tested synthetically rather than claimed
as corpus-verified. Annotation metadata in the observed version-4 documents has
five comma-separated fields rather than only the edit-date field described by
the older reference; those fields therefore remain opaque.

## Page geometry, frames, and tables

This implementation separates documented or corroborated fields from opaque
records. Ami Pro's first-party user guide documents page-layout concepts,
including [paper size, margins, odd/even pages, and mirrored
margins](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10701.txt),
[inserted layouts](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10702.txt),
and [fixed and floating headers](https://public.dhe.ibm.com/software/lotus/desktop/LotusDoc/10741.txt).
The byte-level mappings below are independently corroborated by Born's
reverse-engineering reference and the LGPL KOffice/KWord
[Ami Pro filter notes](https://sources.debian.org/src/koffice/1%3A1.6.3-7/filters/kword/amipro/FileFormat.txt/).
The vendor guide and Born book are copyrighted references; the toolkit cites
and paraphrases them but copies neither prose nor sample bytes.

Distances are signed ASCII integers in twips (1,440 per inch, 20 per point).
For `[lay]`, the low page-size value maps 1 through 7 to Letter, Legal, A3, A4,
A5, B5, and custom. Corroborated feature bits select landscape (256),
non-alternating pages (512), mirrored margins (1024), a second header (2048),
and a second footer (4096). The exact page on which the latter two variants
begin is not independently verified, so the parser records the bits without
inventing that transition.

The `[rght]` and `[lft]` branches describe right/odd and left/even page variants.
Their corroborated nine-number prefix is:

```text
height, width, reserved, left margin, bottom margin,
display unit, top margin, right margin, flags
```

`reserved` is a toolkit label for an unassigned third field, not a semantic
claim. Display-unit values 1, 2, 3, and 4 denote inch, centimetre, pica, and
point for the original UI; geometry itself remains integer twips. Additional
line, gutter, column, or tab fields are retained as an opaque bounded summary
plus the complete raw layout record. They do not drive allocation. The parser
derives page and content rectangles only from a complete, safe prefix. It caps
pages between 1 inch (1,440 twips) and the application's documented 22-inch
maximum (31,680 twips), requires nonnegative margins and at least 720 twips of
remaining width and height as a
toolkit safety policy, and otherwise emits a diagnostic and leaves renderers to
their fixed Letter-page fallback.

`[frm]` has a corroborated six-number prefix: page number, flag word, and left,
top, right, bottom edges in a top-left-origin coordinate system. Width and
height are derived only when right is greater than left and bottom is greater
than top. The validator bounds signed edges to -32,768 through 32,767 and each
span to 31,680 twips. Known flags identify bitmap, drawing, table, opacity,
wrap-around, repeating, text, header, footer, odd/right, border, and anchored
properties. Unknown flag bits and every raw field remain available. `[frmlay]`
is retained as opaque frame-layout metadata: public evidence corroborates its
second field as a width, but its first field does not consistently behave as a
height, so it is not used to derive geometry.

Unknown direct `[lay]` or `[frm]` subrecords, extra layout name/flag fields,
frame-layout fields, and other fixed-prefix tails remain raw and receive
semantic-loss diagnostics. Exact marker-looking lines inside a terminated text
stream remain text rather than being reclassified as structure.

`Frame` objects wrap their readable child blocks. Anchored body commands
`<:tN>` and `<:AN>` select only anchor-flagged frames by zero-based source order,
and the complete `Frame` is inserted at that exact body location. This ordering
is corpus-confirmed. Unanchored frames remain visible after the body with their
page number and validated rectangle, but current renderers deliberately reflow
them rather than reproducing overlapping absolute placement. Repeating, fixed,
and anchored status is represented when the evidenced bits permit it. The
encoding of a true page-background layer is unknown: an unanchored or opaque
frame is never labelled a background merely by inference.

Presentation renderers keep known frame metadata out of ordinary document
prose and render the children directly in source order. The CLI option
`--show-structure-labels` restores the placement/content label for audits.
Unsupported feature bits remain semantic diagnostics and typed JSON fields;
unknown placement/content types and invalid structures remain visibly marked
regardless of that option.

Page layouts contain frame-shaped `[hrght]`, `[frght]`, `[hlft]`, and `[flft]`
branches for right/odd and left/even headers and footers. Public examples and
the inspected corpora place `[lyfrm]`, `[frmlay]`, and `[txt]` either below the
branch or as following sibling records. Both shapes are bounded at the next
header/footer or ordinary page-layout branch, preventing one sibling stream
from consuming another. Layout-backed header/footer text is typed directly and
also carries a `Frame` geometry descriptor. The inspected corpora contain 129
nonempty right-header streams and 19 nonempty right-footer streams across the
installation and private samples; no left-page branches were observed.
Left-page handling is therefore documented-format-backed and synthetic-tested.

Empty header/footer streams now emit no body label; malformed geometry remains
available in diagnostics and JSON rather than becoming invented body content.
Non-promoted known streams retain their readable children without labels;
`--show-structure-labels` restores those labels without changing native page
furniture selection.

`[pg]` contents and the target-layout portion of layout-change page-break
commands are version-dependent and not publicly mapped at byte level. `[pg]`
is retained as `OpaquePageHints`; it never supplies a trusted page count,
allocation size, or inferred layout transition. A confirmed `<:p...>` command
still requests a visible page break before the following paragraph.

Tables use `[tbl]`, optional `[h]` row and `[w]` column definitions, then
`[data]` cell records. The private corpus consistently uses exact 9-, 7-, 5-,
and 12-integer record shapes respectively. The documented prefixes identify
declared dimensions, default and per-row/per-column width/gutter values, flags,
zero-based cell coordinates, connected-cell coordinates, shading, borders,
content flags, and protection. Two table-definition tail integers and three
cell tail integers observed in the corpus remain typed as reserved values; a
stable shape is not treated as proof of their meaning.

Exact, bounded records materialize declared dimensions, header-row bit 16,
cell alignment bits 8/16/24/32, and column width-plus-gutter proportions.
Renderers normalize those proportions to the actual output container. A
connected-cell anchor is applied only when its complete bounded rectangle and
all covered member records agree; otherwise ordinary cells and a semantic
diagnostic are retained. Borders, shading, protection, formulas, page-break
flags, and reserved values remain preserved without invented rendering
semantics. This semantic limitation is reported in diagnostics/JSON rather
than as an `Unsupported table fields` paragraph. Malformed or noncanonical
record shapes keep the visible atomic fallback. Duplicate coordinates retain
both readable values in source order with a visible marker and diagnostic;
cell text uses the same bounded inline parser as body text.

`[fopts]` has four bounded integers: option flags, starting number, separator
length, and indentation. Known bits request collection at the page end,
per-page numbering reset, and a separator line. Dimensions use twips. Unknown
bits and malformed fields are preserved and diagnosed.
Nonempty fields after the supported four-field prefix are retained as opaque
semantic data.

The installation corpus contains 29 validated indexed objects: 18 BMP, three
Ami Draw SDW, and eight standard WMF payloads. Each has a companion block
beginning `SS`; the highest-confidence interpreted subset is described below.
The private corpus adds BMP, SDW, WMF, and OLE1/WordArt examples.
Directory rows have the observed form:

```text
object-id .type primary-offset primary-length companion-offset companion-length
```

Normal offsets are absolute from byte zero; the preamble variant uses the
post-preamble base. Lengths and offsets are untrusted and must be range-checked.
An appended directory is recognized only when its terminal decimal pointer
matches the actual `[Embedded]` byte offset or at least one complete bounded row
provides independent evidence for a damaged pointer. A bare or corrupt marker is
unindexed trailing data. Only fully parsed, in-range primary and companion spans
are excluded from text line limits; undeclared gaps retain the normal line and
line-length ceilings. A manifest range must also lie wholly between the verified
outer `[edoc]` close and directory marker; a row cannot reclassify body or
directory bytes as an asset. The directory is capped at 4,096 physical records
(and callers may lower that ceiling) before offsets or object placeholders are
materialized. Skipped indexed spans are not implicit line breaks: undeclared
text on both sides still shares one line-length budget.

### Windows Metafile payloads

WMF support follows Microsoft's public
[MS-WMF specification](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmf/4813e7fd-52d0-4f42-965f-228c8b7488d2).
The eight installation WMFs and 102 well-formed, in-range private WMFs are
classic type-1 memory metafiles with a version-3 standard header and a final
EOF record; none has an Aldus placeable header. One additional in-range private WMF
has an impossible record size after a complete bitmap and no EOF, and one
directory entry points beyond its source file. Both remain visible
placeholders. Placeable-header handling is therefore specification-backed and
synthetic-tested, not corpus-verified.

The corpus-backed record subset is deliberately narrow: optional anisotropic
map mode before the window transform, one window origin/extent, exactly one
`DIBSTRETCHBLT` using `SRCCOPY`, bounded logical palette
creation/selection/realization, and EOF. The DIB must use the 40-byte
`BITMAPINFOHEADER`, one plane, bottom-up orientation, `BI_RGB`, exact bounded
storage, and 1-, 4-, 8-, or 24-bit pixels. Negative destination width, top-down
DIBs, and additional raster operations remain unsupported. The inspected WMFs
use only the supported operations. Their decoded
dimensions range from small installation icons through 455 by 363 pixels; a
few use a negative destination/window Y extent whose matching signs cancel in
the anisotropic mapping. The decoder requires matching source, destination,
window, and DIB extents rather than guessing a transform.

Both the optional
[placeable header](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmf/828e1864-7fe7-42d8-ab0a-1de161b32f27)
and the required
[standard header](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmf/d169108a-e3fe-436a-bb44-bea61a46ce56)
are validated before records are scanned. Record word lengths, the declared
file and maximum-record sizes, object slots, logical palettes, coordinates,
DIB stride and pixel count, and terminal EOF are all bounded or cross-checked.
Every escape or unknown operation rejects the whole preview. This is especially
important because WMF escape records can carry printer data and other active or
embedded payloads; no raw WMF is sent to a browser, operating-system graphics
API, office suite, or external converter.

A successful WMF becomes an inert IR block containing top-down RGB pixels,
dimensions, an operation summary, and the source SHA-256—not raw WMF records.
The toolkit creates a fresh deterministic PNG for HTML, PDF, ODT, and DOCX;
ODT/DOCX package it under a fixed internal path. Markdown and text emit a
dimensioned marker, while JSON reports byte lengths without inlining pixels.
Any unsupported or malformed WMF remains a digest-bearing visible placeholder
with a stable diagnostic.

### Ami Draw SDW and `SS` companion data

**Public identification evidence.** The public
[TrID definition list](https://mark0.net/soft-trid-deflist-s.html) includes the
“AmiDraw Drawing” name and reports 172 files scanned for that definition. A
[version-pinned mirror of the TrID XML definition](https://github.com/digipres/digipres.github.io/blob/00b9aea89172fde594c6e0c0d654f2204a286162/_sources/registries/trid/triddefs_xml/defs/s/sdw-amidraw.trid.xml)
records only `SM` at byte zero and `01` at byte three. That supports public
identification of the `SM ?? 01` signature family; it does not establish any
header-field, record-envelope, or drawing-operation semantics. The National
Archives'
[PRONOM x-fmt/290 outline record](https://www.nationalarchives.gov.uk/PRONOM/x-fmt/290.xml)
instead identifies AMI Draw Vector Image files with the ASCII beginning
`AMI_METAFILE_FORMAT VERSION`. That record describes a distinct, rare SDW
variant, not the binary family below. Its signature must not be used as evidence
for the binary record layout. The open sources reviewed for this work yielded
no byte-level specification or open reader for the binary family.

**Confirmed corpus observations.** The three embedded installation SDWs, 108
standalone installation SDWs, and 19 indexed private SDWs across 16 documents
all begin with the exact bytes `53 4D 02 01` (`SM 02 01`). The binary header and
recursive record envelope are consistent across those samples. The top-level
header carries two still-opaque fields, a direct-record count, signed bounds,
and a declared stream length. Type 14 contains nested records using the same
envelope immediately after its fixed 18-byte marker; nested summaries are
flattened in preorder. Each ordinary record has an exact little-endian unsigned
16-bit type and unsigned 16-bit byte length, with no inferred padding. For
record types 4 and 5, the point-count and coordinate-storage formulas agree
throughout the inspected corpus: type 4 stores an 8-bit count at record offset
23 and has length
`25 + 4 * count`, while type 5 stores a little-endian 16-bit count at offset 40
and has length `42 + 4 * count`. The validator can therefore cross-check lengths
and bound total point storage.
Those observations do not establish that types 4 or 5 represent any particular
shape, nor do they establish operation ordering, styling, fill, color, or unit
semantics. Numeric type values therefore remain numeric summaries, and the
toolkit does not render SDW vector geometry.

The implemented structural grammar deliberately accepts the observed signature
family `SM ?? 01`, not only the locally observed `SM 02 01` member. It requires a
22-byte header, ordered bounds, an exact declared envelope, the declared number
of byte-exact records, and the recursively validated type-14 rule above. The
effective built-in ceilings are 16 MiB per SDW payload or companion, 10,000
records, nesting depth 32, and 1,000,000 summarized points. Companion previews
are limited to 4,096 pixels on either axis, 4,000,000 pixels per preview, and
8,000,000 materialized SDW pixels per document. Caller-provided limits may only
lower these ceilings.

Every in-range SDW payload that fits the configured byte limit is retained in a
typed `SdwDrawing` object with its SHA-256, signature family, validation status,
header fields, bounds, and bounded recursive record summaries. Companion bytes
are likewise retained with a separate SHA-256 when available. Malformed,
unsupported, trailing, over-limit, and unavailable data produce explicit stable
diagnostics; an unavailable range is not given a hash that was never computed.
Even a structurally validated vector stream emits `sdw-vector-unsupported`,
because structural validation is not a claim that its drawing semantics are
known. JSON reports byte-array lengths without inlining the data.

**Confirmed companion subset.** Observed nonempty `SS` data has an 18-byte
envelope containing the signature, width, height, per-plane row stride, bits per
plane, plane count, and four opaque 16-bit fields. This is DDB-like metadata, but
the resemblance is not a palette or fidelity claim. The toolkit materializes a
preview only for the observed `(bits per plane, plane count)` combinations
`(1,1)`, `(1,4)`, and `(8,1)`, and only when the dimensions, stride, exact storage
length, and document-wide pixel budget validate. Rows are interpreted top-down,
packed samples are MSB-first, and multiple one-bit planes are interleaved by row.
The resulting sample or combined plane value is deliberately mapped to
grayscale as an index preview. It does not claim the original palette, colors,
or exact Ami Draw appearance. Observed 16- and 24-bit companion data is retained
and hashed but not rendered pending evidence for its channel and color layout.

Only a fresh PNG generated from a validated companion preview may enter HTML,
PDF, ODT, or DOCX. Markdown and text receive explicit grayscale/index and
vector-preservation markers, and JSON receives structured metadata with
non-inlined byte lengths.
Raw SDW or `SS` bytes never enter browser objects, PDF attachments, office
package members, operating-system graphics APIs, or external converters. No
production path invokes Ami Pro, Ami Draw, Wine, DOSBox, LibreOffice, or a
proprietary filter. OLE and equations remain inert placeholders.

The final robustness audit reconfirmed the `SS` envelope and storage findings
above across the local installation and private validation sets. It found no
additional evidence for the four opaque words, an original palette, or the
16-/24-bit color-channel layouts. Those fields and depths therefore remain
inertly preserved rather than promoted to an unsupported fidelity claim.

## Strict preservation and bounded parsing

`Diagnostic.severity` controls reporting urgency; `Diagnostic.lossiness`
independently records `none`, `semantic`, or `content`. Strict parsing fails only
for the latter two. This means an info-level fixed-frame reflow is a strict loss,
while an encoding-selection notice is not. Undecodable bytes in actual text are
content loss. Encoding detection uses only bytes through the verified outer
`[edoc]` close, so identical bytes inside either a validated payload or a damaged
post-document tail cannot corrupt the body encoding. Validated payload bytes do
not contribute textual undecodable-byte diagnostics or the strict decision.

`Document.text`, JSON, HTML, Markdown, plain text, ODT, and DOCX share the same
renderer policy: at most 1,000,000 characters from one text value and a
4,000,000-character cumulative document budget, with conservative charging for
HTML/XML escaping. A 65,536-character reserve keeps a bounded omission marker
and later ordinary text visible. Immutable text values of at least 4,096
characters are tracked by identity so an adversarial manual IR cannot multiply
one shared string through thousands of distinct owners. PDF applies its own
equivalent document-wide source-text budget and large-string alias marker before
layout, shaping, page generation, or encoded-output caps.

The decoder constructs one validated byte envelope before line-oriented parsing.
It does not split indexed binary payloads into Python strings. In local
`tracemalloc` measurements, newline-dense invented indexed payloads of 0.25, 1,
2, and 4 MiB peaked at approximately 0.3, 1, 2, and 4 MiB respectively and took
0.008, 0.026, 0.051, and 0.102 seconds. The earlier whole-payload line expansion
scaled by roughly two orders of magnitude. A committed regression warms imports
and requires a representative 0.5 MiB case to remain below eight times input
size.

Seeded deterministic tests mutate section boundaries, nested containers,
bounded and oversized numeric fields, manifests, offsets, duplicate identifiers, tables,
frames, image headers, and renderer-facing IR. The same input must produce the
same parsed text, loss classifications, controlled exception, and rendered
marker on repeated runs. Caller-provided file, line, and line-byte settings may
only lower the built-in ceilings; the same rule applies to the embedded-directory
record ceiling.

## Active content and safety

Ami Pro supported macros, frame macros, DDE, OLE, and external file references.
The converter never executes or activates them and never automatically follows
stylesheet, merge, bitmap, book, or network paths. Unknown commands are not
treated as HTML. Renderers escape source text and do not load remote resources.

## Hypotheses and open questions

- SAM format version 3 is described by secondary sources but is not represented
  in the inspected corpora. (This is separate from the version-3 WMF header.)
- CP1252 is strongly evidenced for Western documents, but other locales and
  actual non-ASCII textual bytes need targeted samples.
- Frame z-order and exact floating coordinates need more reverse engineering;
  body anchor order is recovered, but exact original pagination is not.
- The coordinate origin for overflowing/nested `<:#first,width>` regions and
  the meanings of the first three four-field `<:I...>` values need a controlled
  Ami Pro rendering oracle. Reflowed frames, native list indentation, and
  automatically sized HTML cells currently retain typed source geometry but do
  not invent a containing width.
- The meanings of SDW record type numbers, vector operation order, coordinates,
  styles, colors, and the two opaque binary-header fields remain unknown. In
  particular, the validated type 4/type 5 point formulas do not justify naming
  or drawing those records.
- The relationship between the PRONOM ASCII-header SDW variant and the common
  binary `SM 02 01` family remains unknown.
- The four opaque `SS` envelope words, original palette, and 16-/24-bit channel
  and color layout remain unknown; those depths are preserved but not rendered.
- Unindexed binary-tail recovery is possible by signatures but intentionally
  deferred until false-positive and decompression limits are defined.
