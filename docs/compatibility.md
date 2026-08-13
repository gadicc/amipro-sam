# Compatibility

This matrix describes the current direct parser and renderers. "Placeholder"
means content is kept visible and a diagnostic is emitted; it is never silently
discarded or activated.

| Ami Pro construct | Parser | HTML/PDF/ODT/DOCX | Markdown/TXT |
|---|---|---|---|
| Version 4 header and CP1252 declaration | Supported | N/A | N/A |
| Version 3 header | Tolerated, not corpus-verified | N/A | N/A |
| Paragraph text and anchored-object order | Supported | Supported | Supported |
| Named paragraph styles | Supported where inline/in-file | Reflowed | Simplified |
| Bold, italic, underline, strike | Supported | Supported | Best effort |
| Superscript and subscript | Supported | Supported | Plain text markers/flattened |
| Font family, size, RGB color | Supported | Best effort with substitution | Flattened |
| Alignment, spacing, indents | Supported subset | Reflowed | Flattened |
| Lists inferred from named styles | Best effort | Supported subset | Supported subset |
| Tables and cell text | Supported subset | Reflowed | Simple tables/TSV |
| Table formulas | Cached value recovered; formula preserved in IR | Cached value | Cached value |
| Annotations and inline footnotes | Typed recursive IR; raw metadata retained | Semantic/labeled reflow; footnotes are not native page-bottom objects | Explicit labeled reflow |
| Body and page-layout headers/footers | Typed IR with odd/even placement and raw frame records | Semantic/labeled reflow; not yet repeated in physical page margins | Explicit placement markers |
| Ordinary text frames | Text recovered | Anchored/reflowed with warnings | Reflowed |
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
| Revisions | Not interpreted | Warning/flattened | Warning/flattened |
| Unknown sections and inline tags | Preserved with source span | Warning | Warning |
| Corrupt embedded offsets | Bounds checked | Placeholder/warning | Placeholder/warning |

All paged outputs are preservation-oriented reflows, not pixel-identical Ami Pro
facsimiles. Exact line and page breaks depend on available fonts and modern font
metrics. PDF deliberately uses built-in fonts and may show missing-glyph boxes
for non-Latin scripts; ODT and DOCX preserve the Unicode text for system font
selection.

JSON exposes the typed SDW status, hashes, validated structure, and companion
metadata. Byte arrays are represented by non-inlined length descriptors; raw
SDW data is not copied into JSON or any presentation output.
