# Lotus Ami Pro SAM interoperability RFC

Status: draft 0.1, 2026-08-14. This is an independent interoperability document,
not an official Lotus specification.

The RFC describes the version-4 SAM family evidenced by this project. It favors
loss-preserving parsing: when syntax is established but semantics are uncertain, a
reader should retain the original record, expose the uncertainty, and avoid inventing
layout behavior. Claim IDs refer to [`evidence.md`](evidence.md).

Confidence is dimension-specific. A command may have confirmed grammar and a strong
semantic role while its exact Ami Pro layout or appearance remains open. No
third-party filter output is native-rendering evidence unless a cited controlled
comparison establishes that relationship.

## 1. Conventions

- Text examples show decoded characters. Byte decoding is selected from a BOM,
  `[charset]`, an explicit caller override, or a conservative fallback.
- Physical records normally use CRLF and indentation is structural. SAM headers are
  not ordinary INI files because section names repeat and indentation changes scope.
- Unless a record says otherwise, dimensions are signed decimal twips: 20 twips per
  point and 1,440 twips per inch.
- `f0`, `f1`, and so on are intentionally neutral field names where semantics are not
  established.
- “Reader output” means the semantic result expected from a preservation reader, not
  a mandate for a particular Python class or visual target.

## 2. Container

A typical document has this order (`SAM-CONTAINER-001`):

```text
[ver]
version and metadata sections
repeated [tag] style records
page layouts, frames, and tables
[edoc]
paragraph text and inline commands
optional indexed binary payloads and companion data
[Embedded]
asset directory rows
zero-padded decimal directory offset in the observed version-4 corpora
```

The outer order is `SAM-CONTAINER-001`; the terminal pointer has the narrower,
version-scoped claim `SAM-EMBEDDED-POINTER-001`.

A reader MUST validate all offsets, lengths, counts, and nesting before allocating or
slicing. It MUST NOT execute macros, DDE, OLE, dynamic fields, or external paths. An
unknown or malformed record SHOULD remain available with its source location and an
explicit preservation-loss classification.

The observed `[charset]` form is (`SAM-CHARSET-001`):

```text
[charset]
    82
    ANSI (Windows, IBM CP 1252)
```

The `82` is an Ami Pro identifier, not decimal code page 82. Other locale identifiers
are not yet catalogued well enough to make a complete value table.

## 3. Section registry

| Section/family | Established syntax or values | Meaning / expected reader output | Confidence and provenance |
|---|---|---|---|
| `[ver]` | Observed version `4`; secondary sources describe `3` | Select versioned grammar while retaining the raw value | `4` confirmed; `3` tentative, `SAM-CONTAINER-001` |
| `[charset]` | Identifier plus human-readable description | Select byte decoding without treating the identifier as a code-page number | confirmed for observed `82`/CP1252, `SAM-CHARSET-001` |
| `[tag]` | Style name/envelope followed by indented subrecords | Define a named paragraph style; retain unknown subrecords | confirmed, `SAM-STYLE-001` |
| `[lay]` | Layout flags/size followed by right/left and header/footer branches | Define page geometry and page variants | grammar confirmed; field semantics strong; native pagination/rendering open, `SAM-PAGE-001` |
| `[rght]`, `[lft]` | Nine-number geometry prefix plus possible tail | Right/odd and left/even page rectangles/margins | grammar confirmed; field semantics strong; exact native geometry open, `SAM-PAGE-001` |
| `[hrght]`, `[hlft]`, `[frght]`, `[flft]` | Layout header/footer branches | Preserve or materialize page furniture for the applicable variant | grammar/semantics strong; transition and native placement open, `SAM-PAGE-001` |
| `[frm]` | Frame envelope with nested layout/text/table/image records | Materialize a frame and its readable children; retain placement fields | strong, `SAM-FRAME-001` |
| `[pg]` | Observed page-position/layout hint records | Retain as hints; do not infer allocation or a page transition from position alone | open |
| `[edoc]` | Text stream terminated by standalone `>` | Parse paragraphs, styles, escapes, inline commands, and nested streams | stream grammar confirmed; physical-line paragraph semantics strong, `SAM-TEXT-001`, `SAM-TEXT-PARAGRAPH-001` |
| `[Embedded]` | Rows containing ID, extension, asset offset/length, companion/preview offset/length | Validate and index inert embedded payloads | row grammar/semantics confirmed, `SAM-EMBEDDED-001`; observed terminal pointer confirmed, `SAM-EMBEDDED-POINTER-001` |
| `[newmac]`, `[macro]`, `[frmmac]` | Active-content sections | Preserve bounded metadata only; never execute | syntax confirmed, byte semantics incomplete, `SAM-ACTIVE-001` |
| `[files]`, `[prn]`, `[port]`, `[book]`, `[master]`, `[recfile]` | External-file/print/book metadata | Preserve; never automatically open a document-controlled path | syntax confirmed, field semantics incomplete, `SAM-ACTIVE-001` |
| `[revisions]` | Exact single `0` is the observed no-revisions state | Record revision state; preserve nonzero/additional values as unresolved | tentative, `SAM-REVISION-001` |
| Other header sections | Repeating name plus indented content | Preserve raw with source location; absence from this table is not permission to discard | open |

## 4. Style records

`[tag]` names a style. The top-level envelope also carries a shortcut, the style to
select after a paragraph break, and observed zero sentinels; the following style is
not an inheritance parent (`SAM-STYLE-001`).

| Subrecord | Fields and known values | Meaning / expected reader output | Confidence and provenance |
|---|---|---|---|
| `[fnt]` | `family`, `size_twips`, `packed_color`, `flags`, then possible tail | Set character style. Packed color uses red bits 0–7, green 8–15, blue 16–23. Known low bits cover bold, italic, underline variants, strikeout, super/subscript. Preserve unknown bits/tail | grammar and known low-bit meanings confirmed; native metrics/appearance open, `SAM-STYLE-FONT-001` |
| `[algn]` | `flags`, `unit`, `all_indent`, `first_position`, `rest_position`, then possible tail | Low flag bits: 1 left/default, 2 right, 4 center, 8 justify. Positions are twips. Preserve nondefault/high-bit behavior not yet explained | grammar/low-bit meanings strong; exact geometry and rendering open, `SAM-STYLE-ALIGN-001` |
| `[spc]` | At least five numeric fields; common tail `1`, `100` | Record paragraph spacing. The common tail is neutral; preserve nondefault flags/tightness rather than guessing | tentative, `SAM-STYLE-SPACING-001` |
| `[brk]` | Numeric flags | Page/column break and keep behavior exists, but only separately evidenced bits should be applied | mixed confirmed/open, `SAM-STYLE-001` |
| `[line]`, `[spec]`, `[nfmt]` | Observed nested fields | Preserve raw until individual fields have ledger claims | syntax confirmed; semantics open, `SAM-STYLE-001` |

Duplicate, malformed, truncated, or trailing fields MUST NOT be silently accepted as
fully interpreted. A reader MAY still apply an independently established prefix if it
also retains and reports the remainder.

## 5. Text stream and literals

Within `[edoc]`, a blank physical line ends a paragraph. Consecutive nonblank physical
lines are storage continuations and concatenate without an invented space or newline.
A line containing only optional whitespace plus `>` closes the current text stream.
`>trailing` is text, not a close. Stream delimiters are confirmed
(`SAM-TEXT-001`); physical-line paragraph interpretation is strong but still lacks a
native behavioral observation (`SAM-TEXT-PARAGRAPH-001`).

Named paragraph styles use `@Style Name@`; `@@` is a literal at sign.

| Encoded form | Decoded output | Provenance |
|---|---|---|
| `<<` | `<` | `SAM-ESCAPE-001` |
| `<;>` | `>` | `SAM-ESCAPE-001` |
| `<[>` | `[` | `SAM-ESCAPE-001` |
| `@@` | `@` | `SAM-ESCAPE-001` |
| `</R>` | apostrophe | `SAM-ESCAPE-001` |
| `</x>` | byte derived from `x + 0x40` | `SAM-ESCAPE-001`; preserve undecodable results |
| `<\x>` | byte derived from `x OR 0x80` | `SAM-ESCAPE-001`; preserve undecodable results |

## 6. Inline command registry

The table catalogs all inline families currently interpreted or explicitly recognized
by the project. Unknown commands MUST remain visible or structurally preserved. A
field range below describes safely recognized syntax; it does not imply that all
values have established Ami Pro behavior. “Confirmed” or “strong” inline meanings do
not certify exact glyph metrics, line breaking, pagination, or visual appearance;
those rendering dimensions remain open until isolated by the native oracle.

### 6.1 Character and paragraph state

| Command | Values | Meaning / expected reader output | Confidence and provenance |
|---|---|---|---|
| `<+!>` / `<-!>` | on / off | Bold | confirmed, `SAM-INLINE-STYLE-001` |
| `<+">` / `<-">` | on / off | Italic | confirmed, `SAM-INLINE-STYLE-001` |
| `<+#>` / `<-#>` | on / off | Underline | confirmed, `SAM-INLINE-STYLE-001` |
| `<+)>` / `<-)>` | on / off | Double underline; a target MAY flatten while reporting loss | confirmed, `SAM-INLINE-STYLE-001` |
| `<+$>` / `<-$>` | on / off | Word underline; a target MAY flatten while reporting loss | confirmed, `SAM-INLINE-STYLE-001` |
| `<+&>` / `<-&>` | on / off | Superscript | confirmed, `SAM-INLINE-STYLE-001` |
| `<+'>` / `<-'>` | on / off | Subscript | confirmed, `SAM-INLINE-STYLE-001` |
| `<+%>` / `<-%>` | on / off | Strikeout | confirmed, `SAM-INLINE-STYLE-001` |
| `<+@>` | no payload | Left alignment | confirmed, `SAM-INLINE-STYLE-001` |
| `<+A>` | no payload | Right alignment | confirmed, `SAM-INLINE-STYLE-001` |
| `<+B>` | no payload | Center alignment | confirmed, `SAM-INLINE-STYLE-001` |
| `<+C>` | no payload | Justified alignment | confirmed, `SAM-INLINE-STYLE-001` |
| `<:f>` | no payload | Proposed restore of character state from the current paragraph style | tentative semantics; native behavior open, `SAM-INLINE-FONT-RESET-001` |
| `<:fSIZE>` | signed bounded twips | Set font size; omitted-property inheritance remains to be isolated | grammar confirmed; meaning strong; reset details tentative, `SAM-INLINE-FONT-001`, `SAM-INLINE-FONT-RESET-001` |
| `<:fSIZE,FAMILY>` | size and escaped family | Set size/family; leading family discriminator digits are metadata rather than family text | grammar confirmed; meaning strong; native metrics open, `SAM-INLINE-FONT-001` |
| `<:fSIZE,FAMILY,R,G,B>` | RGB channels bounded/clamped to 0–255 | Set size, family, and color | grammar confirmed; meaning strong; native appearance open, `SAM-INLINE-FONT-001` |
| `<:fSIZE,FAMILY,>` | compact observed form | Apply present size/family; proposed restoration of default color | grammar confirmed; reset semantics tentative, `SAM-INLINE-FONT-001`, `SAM-INLINE-FONT-RESET-001` |
| `<:S+-1>` / `-2` / `-3` | three sentinel values | Single / 1.5 / double line spacing | grammar confirmed; meaning strong; exact native spacing open, `SAM-INLINE-SPACING-001` |
| `<:S+N>` | bounded finite numeric value | Other spacing quantity; retain exact value and avoid stronger unit claims | syntax confirmed, semantics tentative, `SAM-INLINE-SPACING-001` |
| `<:S->` | no payload | Restore current style line spacing | grammar confirmed; meaning strong; exact native spacing open, `SAM-INLINE-SPACING-001` |
| `<:>` | no payload | Restore current style character state | grammar confirmed; meaning strong; native behavior open, `SAM-INLINE-CONTROL-001` |
| `<:s>` | no payload | Nonprinting spelling state | grammar confirmed; meaning strong; native behavior open, `SAM-INLINE-CONTROL-001` |
| `<:p>` | no payload | Page break before following visible paragraph | grammar confirmed; meaning strong; native pagination open, `SAM-INLINE-CONTROL-001` |
| `<:p...>` | noncanonical payload | Retain as an unsupported variant; do not infer payload semantics | syntax strong, payload open, `SAM-INLINE-CONTROL-001` |

### 6.2 Geometry, anchors, and dynamic content

| Command | Values | Meaning / expected reader output | Confidence and provenance |
|---|---|---|---|
| `<:#f0,f1>` | exactly two nonnegative bounded integers; `f1 > 0` | Preserve paragraph-region geometry. `f1` correlates with active measure. Do not name `f0` a general x-origin or margin | strong syntax/correlation; `f0` open, `SAM-INLINE-REGION-001` |
| `<:If0,f1,f2,f3>` | exactly four bounded integers in the established shape | Preserve one atomic indentation/layout tuple; do not partially apply unconfirmed fields | strong syntax, semantics open, `SAM-INLINE-INDENT-001` |
| `<:tN>` / `<:AN>` | nonnegative frame index | Associate the text position with an indexed anchor-eligible frame; preserve unresolved/out-of-range anchors | strong, `SAM-FRAME-001` |
| `<:D...>` | observed variants | Dynamic/current date placeholder; do not evaluate as source text | tentative meaning; exact variants open, `SAM-INLINE-DYNAMIC-001` |
| `<:P...>` | observed variants | Dynamic page-number placeholder; render only where target context safely supports it | tentative meaning; exact variants open, `SAM-INLINE-DYNAMIC-001` |
| `<:X...>` | quoted/delimited dynamic field expression | Never execute. Preserve expression; a reader MAY expose inert fallback/merge-field text | syntax strong, payload semantics open, `SAM-INLINE-DYNAMIC-001` |
| `<:X~...>` | matching dynamic-field close | Close only the matching open field; unmatched forms remain explicit | syntax strong, semantics open, `SAM-INLINE-DYNAMIC-001` |
| `<:Z...>` / `<:Z~...>` | observed dynamic/revision family | Preserve without execution or inferred effect | syntax strong, semantics open, `SAM-INLINE-DYNAMIC-001` |
| Other `<...>` | bounded content through an unescaped close | Preserve raw and surrounding text; emit an explicit unsupported record/marker | open |

### 6.3 Multiline inline containers

| Opener | Meaning / expected reader output | Confidence and provenance |
|---|---|---|
| `<:N...>` | Annotation stream with metadata and nested paragraphs | strong, `SAM-INLINE-CONTAINER-001` |
| `<:F...>` | Footnote stream | strong specification support; sparse corpus coverage, `SAM-INLINE-CONTAINER-001` |
| `<:H...>` | Header stream | strong, `SAM-INLINE-CONTAINER-001` |
| `<:h...>` | Footer stream | strong specification support; sparse corpus coverage, `SAM-INLINE-CONTAINER-001` |

Each container ends at its own standalone `>` before parsing of the outer `[edoc]`
continues. Quoted or escaped `>` bytes inside dynamic fields do not close a container.
Malformed metadata should not allow an inner close to truncate the outer document.

## 7. Page layout and frames

`[lay]` page-size codes 1–7 map to Letter, Legal, A3, A4, A5, B5, and custom.
Corroborated feature bits include landscape `256`, non-alternating pages `512`,
mirrored margins `1024`, second header `2048`, and second footer `4096`
(`SAM-PAGE-001`). These are strong semantic mappings, not native-rendering claims.
The exact page where secondary variants begin, Ami Pro pagination, and exact visible
geometry remain open.

The nine-field `[rght]`/`[lft]` prefix describes page/print rectangles and margins in
twips. Readers should validate derived rectangles before using them. Invalid,
duplicate, or incomplete geometry remains raw and MUST NOT create negative or
unbounded renderer dimensions.

Frames can contain text, tables, images, and opaque records (`SAM-FRAME-001`). Exact
floating coordinates, wrapping, repeating/fixed placement flags, and z-order remain
partly open. A reflowing target should recover readable children and report placement
loss rather than imply pixel equivalence.

## 8. Tables

The strongest current mappings are (`SAM-TABLE-001`):

| Record | Fields | Meaning | Confidence |
|---|---|---|---|
| `[tbl]` | `f0`, `f1`, then at least five additional fields; corpus commonly has nine total | `f0` declared row count, `f1` declared column count | strong |
| `[data]` | `f0`, `f1`, then cell components | `f0` zero-based row, `f1` zero-based column | strong |
| `[h]` | indexed row metric fields | Row index/metric structure exists | strong shape; individual labels mixed tentative/open |
| `[w]` | indexed column metric fields | Column index/metric structure exists | strong shape; individual labels mixed tentative/open |

Bounds checking, duplicate policy, inferred recovery, and malformed-input handling are
reader safety policy rather than format evidence. Header-row flags, general merge
orientation, border/shading/protection/formula labels, cached-value precedence, and
many tail fields remain below confirmed confidence. Preserve their raw values and cite
new claim IDs before assigning semantics.

## 9. Embedded and active content

An `[Embedded]` directory row has the shape:

```text
ID EXTENSION ASSET_OFFSET ASSET_LENGTH PREVIEW_OFFSET PREVIEW_LENGTH
```

In the observed version-4 corpora, the file ends with a zero-padded decimal directory
offset (`SAM-EMBEDDED-POINTER-001`). Born's secondary account describes an ASCII
hexadecimal locator, so readers SHOULD preserve the raw locator and MUST NOT extend
the decimal claim to unobserved versions without new evidence. A reader MUST validate
directory and payload ranges against the correct stream origin, reject
overlap/out-of-range arithmetic, cap decoded dimensions and work, and keep unknown
payloads inert.

Known payload families include BMP/WMF images, Ami Draw SDW data and `SS` companions,
OLE, and equations. Recognition of a payload signature is not permission to pass its
bytes to an operating-system graphics API, browser, office package, or external
converter. Only independently decoded, bounded safe output should be rendered.

Macros, DDE, OLE activation, dynamic fields, external stylesheets, merge sources,
books, bitmaps, and network paths (`SAM-ACTIVE-001`) MUST NOT be executed or
automatically followed.

## 10. Conformance and open work

A preservation reader conforms to this draft when it:

- recovers established text and structure without presenting open semantics as fact;
- retains bounded unknown records and source positions;
- makes semantic/content loss visible independently of diagnostic severity;
- applies strict offset, length, nesting, count, text, image, and output limits; and
- never activates document-controlled behavior or paths.

The priority open questions are the exact `<:#...>` and `<:I...>` field meanings,
table merge and cell-style components, frame placement/z-order, nondefault style
spacing and flags, SDW vector semantics, and the remaining inline families. The
controlled experiments are maintained in
[`docs/research/executable-format-re.md`](../docs/research/executable-format-re.md#prioritized-open-questions-and-oracle-experiments)
and should promote claims through [`evidence.md`](evidence.md), not by editing this
table without provenance.
