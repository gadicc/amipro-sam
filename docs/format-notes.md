# Lotus Ami Pro SAM format notes

These notes distinguish observations from hypotheses. They describe enough of
the format to explain the parser; they are not a complete vendor specification.

## Evidence and provenance

Confirmed behavior was cross-checked among:

- 13 documents from a locally owned Ami Pro 3.1 installation, inspected only
  for interoperability and never copied into this repository;
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
mistaken for the outer document terminator. The parser recovers their readable
text as labeled reflowed content. Other commands remain in the intermediate
representation and generate diagnostics.

## Frames, tables, and embedded objects

Frames use `[frm]` with nested layout and content records. `[txt]` contains an
ordinary Ami Pro text stream. Tables use `[tbl]`, optional row/column definition
records, then `[data]` records whose first two integers are zero-based row and
column coordinates. Body commands `<:tN>` and `<:AN>` refer to frames whose
anchor flag is set; the zero-based `N` selects those frames in source order. In
the inspected corpora these references determine table and floating-frame order.
The current table reader recovers rectangular cell text but does not yet
reproduce every border, merge, formula, or page coordinate.

The installation corpus contains 29 validated indexed objects: 18 BMP, three
Ami Draw SDW, and eight standard WMF payloads. Each has an opaque companion block
beginning `SS`. The private corpus adds BMP, SDW, WMF, and OLE1/WordArt examples.
Directory rows have the observed form:

```text
object-id .type primary-offset primary-length companion-offset companion-length
```

Normal offsets are absolute from byte zero; the preamble variant uses the
post-preamble base. Lengths and offsets are untrusted and must be range-checked.
Only bounded BMP payloads are currently made available to renderers. WMF, SDW,
OLE, equations, and companion data are inert placeholders.

## Active content and safety

Ami Pro supported macros, frame macros, DDE, OLE, and external file references.
The converter never executes or activates them and never automatically follows
stylesheet, merge, bitmap, book, or network paths. Unknown commands are not
treated as HTML. Renderers escape source text and do not load remote resources.

## Hypotheses and open questions

- Version 3 is described by secondary sources but is not represented in the
  inspected corpora.
- CP1252 is strongly evidenced for Western documents, but other locales and
  actual non-ASCII textual bytes need targeted samples.
- Frame z-order and exact floating coordinates need more reverse engineering;
  body anchor order is recovered, but exact original pagination is not.
- The semantics of opaque `SS` companion blocks remain unknown.
- Unindexed binary-tail recovery is possible by signatures but intentionally
  deferred until false-positive and decompression limits are defined.
