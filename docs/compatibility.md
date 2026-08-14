# Compatibility

This matrix describes the current direct parser and renderers. "Placeholder"
means content is kept visible and a diagnostic is emitted; it is never silently
discarded or activated.

Diagnostic severity and lossiness are orthogonal. Strict parsing rejects
`semantic` loss (meaning/layout/behavior approximated) and `content` loss
(material unavailable or unrepresented), but not a `none` diagnostic. This is
parser/IR preservation strictness, not a claim that every target format is a
facsimile.

| Ami Pro construct | Parser | HTML/PDF/ODT/DOCX | Markdown/TXT |
|---|---|---|---|
| Version 4 header and CP1252 declaration | Supported | N/A | N/A |
| Version 3 header | Tolerated, not corpus-verified | N/A | N/A |
| Paragraph text and anchored-object order | Supported | Supported | Supported |
| Named paragraph styles | Character/paragraph subset plus exact shortcut/following-style envelope; raw behavioral flags retained | Reflowed; unimplemented spacing/all-indent semantics remain diagnostics, not body paragraphs | Simplified |
| Bold, italic, underline, strike | Supported | Supported | Best effort |
| Superscript and subscript | Supported | Supported | Plain text markers/flattened |
| Font family, size, RGB color | Supported | Best effort with substitution | Flattened |
| BMP Unicode text | Preserved in IR | PDF uses cmap-gated fixed bundled fonts for Latin, Greek, Cyrillic, documented SC-default CJK, Hebrew, and Arabic subsets; ODT/DOCX retain Unicode for consumer font selection; HTML uses browser fallback | Preserved as Unicode |
| RTL Hebrew/Arabic | Preserved in logical order | PDF uses bounded line-level bidi ordering and shaping for whitespace-separated directional runs, flattens inline style within an RTL paragraph, and records logical `ActualText`; unsupported no-space mixed-direction tokens receive an explicit marker; HTML/ODT/DOCX delegate layout to the consumer | Preserved in logical order |
| Non-BMP scalars, lone surrogates, noncharacters, and unsupported directional controls in PDF | Preserved by parser/IR where representable | PDF emits a visible U+FFFD for an unsupported scalar/control; no false glyph or `ToUnicode` claim | Preserved or escaped according to the target format |
| Alignment, spacing, indents | Documented style positions plus bounded `<:#first,width>` regions; observed four-field `<:I...>` is typed but deliberately unapplied | Full measures retain a first-line position; narrower regions resolve only against an explicit page/cell width; impossible or unknown-container geometry safely falls back | Flattened; typed data remains in JSON |
| Lists inferred from named styles | Best effort | Supported subset | Supported subset |
| Tables and cell text | Supported subset | Reflowed | Simple tables/TSV |
| Table formulas | Cached value recovered; formula preserved in IR | Cached value | Cached value |
| Annotations and inline footnotes | Typed recursive IR; raw metadata retained | Semantic/labeled reflow; footnotes are not native page-bottom objects | Explicit labeled reflow |
| Page size and margins (`[lay]`, `[rght]`, `[lft]`) | Typed nine-field twip prefix; opaque tail is retained with semantic-loss classification; impossible geometry diagnosed | First renderer-valid odd/right geometry, then even/left, controls page/print size and margins; otherwise a fixed Letter fallback | Typed data remains in JSON; prose formats do not simulate a page box |
| Page-layout headers/footers | Typed odd/even IR with bounded `[lyfrm]` geometry; nested and sibling stream shapes supported | PDF/ODT/DOCX promote a narrow unambiguous, layout-matched, size-bounded subset to repeated page furniture; HTML and all ambiguous, malformed, complex, or body-stream variants remain visibly reflowed | Explicit placement markers |
| Anchored body frames | Typed `Frame` at the original zero-based `<:tN>`/`<:AN>` anchor location; opaque header tails and `[frmlay]` fields are classified | Contents rendered once in source order; bounded width may guide safe reflow, but absolute coordinates and overlap are not reproduced | Explicit frame marker and source-order contents |
| Fixed-page and repeating frames | Page/flag/rectangle metadata typed when bounded; raw fields retained | Visible labeled source-order reflow; original fixed/repeating placement is not reproduced | Explicit frame marker and source-order contents |
| Background frames and z-order | No byte-level mapping claimed; `layer_role` remains `unknown` | No inferred background or stacking behavior | No inferred background or stacking behavior |
| Explicit page breaks | Paragraph break request retained; unknown layout target remains opaque | Preserved between visible content; redundant leading/trailing breaks may be dropped | Explicit break marker where the format supports it |
| `[pg]` page hints | Preserved as opaque typed raw data; never trusted as page count or allocation input | Not used for pagination | Available only through structured preservation output |
| Embedded BMP | Safely indexed and bounded | HTML embeds; paged formats use placeholder | Placeholder |
| WMF type-1 SRCCOPY DIB subset | Strict bounded standard/placeable validation; one bottom-up 1/4/8/24-bit BI_RGB raster decoded to inert RGB IR | Fresh internal PNG in HTML/PDF/ODT/DOCX | Explicit dimensions marker |
| Other/malformed WMF | Digest-bearing inert placeholder and diagnostic | Visible placeholder; never activated | Visible placeholder |
| Ami Draw binary `SM ?? 01` preservation subset (locally observed as `SM 02 01`) | Bounded recursive-envelope validation; typed raw payload, SHA-256, header fields, bounds, and numeric record summaries; vector operation/style semantics intentionally unsupported | Explicit vector-preservation marker; a supported companion may supply a fresh preview | Explicit vector-preservation and companion-preview markers |
| SDW `SS` companion `(bits per plane, planes)` = `(1,1)`, `(1,4)`, or `(8,1)` | Strict 18-byte DDB-like envelope/storage validation; bounded top-down, MSB-first, row-interleaved index preview | Fresh internal grayscale/index PNG only; no source palette or color-fidelity claim | Explicit grayscale/index marker |
| Other or malformed SDW/companion data, including 16-/24-bit companions and the distinct PRONOM ASCII-header variant | In-range, within-limit bytes and SHA-256 retained in typed IR with an explicit diagnostic; unavailable ranges remain explicit without a false hash | Vector status remains visible; an independently valid companion may still supply a fresh preview; source data is never activated or packaged | Explicit vector status; independently valid companion marked separately |
| OLE/WordArt | Never activated | Placeholder | Placeholder |
| Equations | Preserved as unsupported object | Placeholder | Placeholder |
| Macros, DDE, active fields | Never executed/followed | Inert placeholder/fallback | Inert placeholder/fallback |
| External `.sty`, merge, master/book links | Reference preserved; never followed | Warning | Warning |
| Revisions | Exact single `[revisions] 0` is a no-revisions state; other/duplicate states are not interpreted | Noncanonical state remains warning/flattened | Noncanonical state remains warning/flattened |
| Unknown sections and inline tags | Preserved with source span and classified semantic loss; exact empty `[elay]`, bounded `[l1]`, spelling state, compact font forms, and matched field closes are recognized | Genuine unknown content stays a visible inert placeholder; metadata-only losses stay in diagnostics/JSON instead of becoming body paragraphs | Genuine unknown content stays visible |
| Corrupt embedded offsets | Bounds checked | Placeholder/warning | Placeholder/warning |

All paged outputs are preservation-oriented reflows, not pixel-identical Ami Pro
facsimiles. Exact line and page breaks depend on modern font metrics. PDF uses
only fixed, openly licensed in-package fonts and never resolves a source font
name as a path. Its supported subset is BMP-only. The CJK face uses SC-default
unified-Han forms and does not claim vertical text, ruby, variation-sequence, or
locale-specific glyph fidelity. RTL paragraphs use the best-coverage single
DejaVu face whose cmap is checked per character, so per-run font/style changes,
unmapped Hebrew/Arabic extensions, mixed RTL/CJK shaping, and direction changes
inside one whitespace-free token are conservatively flattened, replaced, or
visibly marked rather than rendered with false fidelity. The PDF stores
line-level logical `ActualText`, but third-party extractor support varies; JSON,
TXT, ODT, and DOCX remain the authoritative logical-text alternatives.

JSON exposes the typed SDW status, hashes, validated structure, and companion
metadata. Byte arrays are represented by non-inlined length descriptors; raw
SDW data is not copied into JSON or any presentation output.
