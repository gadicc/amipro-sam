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

## Styles and measurements

`[tag]` records define named styles. Observed subrecords include `[fnt]`,
`[algn]`, `[spc]`, `[brk]`, `[line]`, `[spec]`, and `[nfmt]`. Font sizes and
layout dimensions use twips: 20 twips per point and 1,440 per inch. Packed style
colors use red in bits 0-7, green in 8-15, and blue in 16-23.

Confirmed character flag bits include bold, italic, underline, word underline,
and double underline. Confirmed alignment bits represent left, right, center, and
justified paragraphs. Break flags can request page/column breaks and paragraph
keep behavior; only the currently verified subset is rendered.

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

## Frames, tables, and embedded objects

Frames use `[frm]` with nested layout and content records. `[txt]` contains an
ordinary Ami Pro text stream. Tables use `[tbl]`, optional row/column definition
records, then `[data]` records whose first two integers are zero-based row and
column coordinates. Body commands `<:tN>` and `<:AN>` refer to frames whose
anchor flag is set; the zero-based `N` selects those frames in source order. In
the inspected corpora these references determine table and floating-frame order.
The current table reader recovers rectangular cell text but does not yet
reproduce every border, merge, formula, or page coordinate.

Page-layout records contain frame-shaped `[hrght]`, `[frght]`, `[hlft]`, and
`[flft]` branches for right/odd and left/even headers and footers. Each branch
may contain `[lyfrm]`/`[frmlay]` placement records followed by a bounded `[txt]`
stream. Layout-backed header/footer text is represented explicitly and its raw
geometry is retained for later page-layout work. The inspected corpora contain
129 nonempty right-header streams and 19 nonempty right-footer streams across
the installation and private samples; no left-page branches were observed.
Left-page handling is therefore documented-format-backed and synthetic-tested.

`[fopts]` has four bounded integers: option flags, starting number, separator
length, and indentation. Known bits request collection at the page end,
per-page numbering reset, and a separator line. Dimensions use twips. Unknown
bits and malformed fields are preserved and diagnosed.

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
