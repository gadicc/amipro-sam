# Corpus fidelity cleanup plan

Date: 2026-08-14

## Goal

Remove false or avoidable preservation placeholders from converted Ami Pro
documents, fix the paragraph-region bug that squeezes text into the right edge,
and retain explicit loss reporting for constructs whose semantics are still
genuinely unknown.

This is a parser/IR correction, not a facsimile-layout project. The private
`mydocs` corpus remains an uncommitted validation corpus; regression fixtures
must contain invented content only.

## Evidence gathered before implementation

- Baseline: 383 of 384 private SAM files parse; `sacb.sam` is empty. The test
  suite passes with `PYTHONPATH=src`: 468 passed and 27 optional-dependency
  skips.
- The parser emits 3,673 visible `style flag bits` blocks, 3,673 visible
  `style fields` blocks, 402 visible `revision state` blocks, and 784 visible
  `unknown section` blocks across the parseable corpus.
- All 402 `[revisions]` sections contain the single value `0`; they do not
  contain a revision payload.
- The common style flags are the documented Ami Pro baseline values: font bits
  `0x4000`/`0x8000` and spacing bits `0x10`/`0x20`. The common style envelope is
  a shortcut number, a following-style name, and two zero fields. It is
  metadata, not body content.
- There are 11,446 `<:#x,width>` commands in 299 files. The second value equals
  the validated body width exactly or within three twips in 77.1% of cases.
  Two-column documents reuse a width near half the body width with different
  x positions; one source even labels the later region as its second column.
  The current parser instead assigns the second value as the left indent.
  This gives 6,641 paragraphs an indent above three inches and reproduces the
  reported right-edge sliver in PDF, HTML, ODT, and DOCX.
- All 750 corpus `<:I...>` commands use four fields. The parser expects three,
  emits a visible unsupported marker, but still applies part of the malformed
  interpretation.
- `<:s>` is a nonprinting spelling-state command and occurs 1,516 times. Common
  three-field font commands such as `<:f240, Wingdings,>` carry a size/family
  with an empty optional color tail; they are currently marked unsupported.
- Empty `[elay]` terminators occur 403 times. `[l1]` contains a single bounded
  integer (`0` in 380 files and `1` in one two-layout file), but its exact
  selector/index semantics are not sufficiently corroborated.
- `[frmname]` accounts for 388 of 390 opaque frame subrecords and contains a
  bounded ordinary frame name. Paired `:X~...` field terminators are currently
  printed even though the opening field already emitted its inert fallback.
- Repository history contains no Ami Pro executable, emulator, disassembler,
  or decompiler workflow. The implementation was built from public format
  references, an LGPL KOffice importer, locally owned installation samples,
  and corpus-based interoperability observations.

## Implementation

### 1. Correct typed metadata without hiding real loss

- Treat `[revisions]` containing exactly one bounded zero as an explicit
  no-revisions state. Keep nonzero, malformed, duplicate, or additional data
  preserved and visibly loss-classified.
- Extend `StyleDefinition` with bounded shortcut/following-style metadata.
  Parse only the exact documented envelope (shortcut, following style, and two
  structural zero sentinels) and stop misusing the following-style name as an
  inheritance parent. Preserve unexpected fields as `UnknownRecord` entries and
  keep their semantic-loss diagnostics.
- Distinguish structural/class-marker flag bits from output-affecting bits.
  Quiet only corroborated baseline marker bits or behavior the renderers
  implement; retain raw flags and semantic diagnostics for unimplemented visual
  behavior such as all-caps, spacing control, and both-side indentation.
- Recognize an empty `[elay]` terminator. Parse the exact single-integer `[l1]`
  shape as raw typed metadata without allowing it to choose renderer geometry;
  its indexing, scope, and relation to `[pg]` remain unproved. Malformed forms
  remain explicit losses.
- Type a bounded `[frmname]` value as frame metadata. Preserve malformed,
  duplicate, or additional fields as opaque subrecords.

### 2. Correct inline commands and paragraph regions

- Treat `<:s>` as a supported nonprinting spelling-state command.
- Accept and atomically preserve the observed four-field `<:I...>` shape when
  all fields are bounded and the trailing field is zero, but do not apply its
  geometry until the field meanings are independently corroborated. Invalid
  variants remain unsupported and cannot partially mutate paragraph state.
- Accept the observed compact font form `<:fsize,family,>` without inventing a
  color.
- Reinterpret `<:#x,width>` as bounded, typed paragraph-region geometry. Never
  assign the width to `left_indent_in`. Resolve it only against an explicit
  containing width (page body, frame text area, table cell, or header/footer)
  and retain the raw x/width in the IR. If no container is known, or the region
  is impossible beyond a documented tolerance, preserve it with a diagnostic
  and do not fabricate indents. Keep region resolution centralized so style and
  inline-indent interactions use the same rules in every renderer.
- Consume a paired `:X~...` field terminator without printing a second marker
  after its opening field fallback; unmatched or malformed field commands stay
  explicit.
- Keep hostile, overlong, nonnumeric, extra-field, and impossible forms visible
  and strict-mode lossy. Do not relax resource limits.

### 3. Regression coverage

- Add invented SAM fixtures/tests for: zero versus nonzero revisions; standard
  versus opaque style envelopes; baseline versus unknown style bits; empty and
  malformed structural markers; typed-but-unapplied four-field indent commands;
  compact font commands; named frames; paired fields; full-width and two-column
  paragraph regions; and malformed numeric variants.
- Assert IR geometry directly, including that `<:#426,9025>` cannot become a
  6.267-inch left indent.
- Assert HTML CSS, PDF text bounding boxes, and ODT/DOCX package geometry where
  optional dependencies are available. Render representative PDFs to PNG with
  Poppler and visually check page size, margins, readable line measure, and
  absence of the four reported false placeholders. Include body, header/frame,
  table-cell, reset, and custom-page-width cases so body-width assumptions do
  not leak into nested containers.
- Run the full unit suite, Ruff, deterministic PDF checks, CLI smoke tests, and
  a before/after aggregate scan of all 384 private files. The corpus gate is no
  parse regression and a large reduction in false visible markers; remaining
  markers must map to genuinely unsupported constructs.

### 4. Documentation and commits

- Update compatibility/format notes with the evidence level for the corrected
  fields, the distinction between metadata diagnostics and body placeholders,
  and the remaining unsupported command/section inventory.
- Commit this plan before implementation. Then use separate implementation and
  documentation/verification commits when the diff separates cleanly.

## Adversarial guardrails

- A frequent value is not sufficient evidence by itself. Only standard shapes
  corroborated by public importer notes, cross-field geometry, or controlled
  corpus relationships become supported.
- Removing a body placeholder must not silently remove its raw bytes or
  structured representation. Unknown variants stay in JSON and diagnostics.
- Do not treat `[revisions] 0` as permission to discard actual inline revision
  commands; those remain independently classified.
- Paragraph-region arithmetic is checked against the containing width and
  clamped only for safe rendering. It must not turn malformed negative or huge
  values into apparently valid layout.
- Multiple page layouts, frames, table cells, inherited styles, and strict mode
  receive explicit tests so the common-case cleanup does not broaden false
  claims of fidelity.
- A lower visible-marker count is not itself success. Loss accounting remains
  in diagnostics/raw JSON, and only exact benign syntax or implemented behavior
  may lose a rendered placeholder. Acceptance is scoped to specific diagnostic
  classes and verified text/geometry behavior rather than a zero-warning target.
- Generated PDF bytes are not a visual oracle. Verify extracted positions and
  rendered pages; do not compare nondiagnostic PDF internals as layout truth.

## Deferred oracle work

An Ami Pro/Windows 3.1 emulator is high-value follow-up, but is intentionally
outside this implementation. A reproducible oracle should use lawfully owned
media in a network-disabled, version-pinned DOSBox-X image; invented one-feature
SAM documents; a fixed PostScript printer driver/PPD; captured PostScript as the
primary artifact; and derived PDF/PNG outputs with pinned Ghostscript/Poppler.
Tests should compare text order, bounding boxes, page counts, and tolerant
rasters rather than PDF bytes. Vendor executables, disk images, fonts, manuals,
and private documents must not be committed.
