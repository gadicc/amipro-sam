# Ami Pro executable format reverse-engineering plan

Date: 2026-08-14

## Goal and boundary

Use the lawfully owned Lotus Ami Pro 3.1 installation only to establish file-format
interoperability facts that can later be implemented in the open converter.  This phase
will inventory and statically analyze the installation, correlate narrow executable data
flows with aggregate private-corpus shapes, and record reproducible evidence.  It will not
execute untrusted document content, redistribute or modify vendor assets, or change parser,
IR, renderer, or converter semantics.

The proprietary payload under `/tmp/amipro-arj/payload` is volatile input, never project
state.  Every analysis entry point must resolve `AMIPRO_PAYLOAD_DIR` (or an explicit input),
hash the exact opened bytes it analyzes, and reject a mandatory manifest mismatch after the
initial inventory.
Generated repository artifacts may contain hashes, bounded structural metadata, addresses,
cross-reference descriptions, and short evidentiary instruction windows; they may not
contain extracted segments, resources, documents, fonts, help text, executable images, or
large disassembly/decompiler listings.

## Investigation completed before this plan

- At the initial audit, the worktree was clean on `main` at `386c778`.  Repository history at
  that point contained no executable
  analysis workflow or vendor binaries.  The existing plan/implementation/documentation
  commit sequence provides a precedent for committing this plan separately.
- `AMIPRO.EXE` is 888,224 bytes and its SHA-256 is
  `555506d1558d61579d5c6fee8bf5fa9d960aa05a20a5d171240ac2e0ea73cbbd`, matching the
  supplied value.  `file` and Wine's dumper identify it as a Windows 3.x NE executable.
  Wine reports 209 segments, nine module references, entry point `5:0001`, expected Windows
  version 3.0, and exported entry points including `SAMMYTEXTPROC` and file/OLE callbacks.
- The installed GNU Binutils 2.46.1 and LLVM 22.1.8 object dumpers reject the NE container;
  GNU `objdump` can still act as a raw `binary`/i8086 decoder after a segment has been mapped.
  The installed `pefile` 2024.8.26 rejects its non-PE signature.  Wine's `winedump` 11.14
  reads the NE header, resources, exports, segment metadata, and relocations.  NASM's
  `ndisasm` 3.02 can
  decode 16-bit instruction bytes but does not understand NE segments, iterated data,
  relocations, imports, or cross-segment control flow.  Capstone 5.0.9 is available through
  `cstool` with the same raw-byte limitation.  Ghidra, radare2, and Rizin are not installed.
  Preliminary, unpinned source inspection of the Ghidra NE loader and radare2 NE plugin
  indicates that each maps a segment's stored file range directly and does not expand
  iterated/self-loading segment data.  The research ledger must pin the exact revision, path,
  and relevant lines before relying on that finding.  Until then, installing either risks a
  misleading memory map for `AMIPRO.EXE` unless a validated expansion/preprocessing layer is
  supplied.
- `winedump` reports that all 209 `AMIPRO.EXE` segments carry the `SELFLOAD` flag, 208 carry
  `ITERATED`, and 206 are discardable.  The entry segment alone stores `0x404c` bytes for a
  `0x5cef` allocation.  These counts require independent-indexer validation, but already show
  why direct disassembly of the stored range cannot be presumed to represent the loaded
  program.
- Two smaller conversion filters are higher-value first targets.  `W4W33F.DLL` (108,176
  bytes, SHA-256 `a2f34efd5191ee1bd71e7410a689266f861932e1c6f734ff7a3c47f3dcd95759`)
  exports `FILTERFROM`/`WFWFROM`, identifies its input as Ami Pro SAM, and contains `[tbl]`,
  `[h]`, `[w]`, `[data]`, and `[frmlay]` parser references.  `W4W33T.DLL` (110,960 bytes,
  SHA-256 `0a1fd9f33120f16f2b96a5eb7716ee35d52ad2829fc1d610eb17302bd9c8eba1`)
  exports `FILTERTO`/`WFWTO` and contains matching serializer references plus `[algn]` and
  `[spc]`.  Bounded raw 16-bit disassembly shows code loading the exact token offsets and
  passing them to common routines; relocation-aware call targets and downstream field flow
  still require validation.  This evidence establishes relevant parser/serializer roles,
  not field meanings.
- The payload contains 143 NE containers: 136 `.EXE`/`.DLL`/`.FLT` targets, six `.FON`
  resource DLLs, and one resource-only `.STR` file.  No PE target was found.  The manifest
  should inventory this signature-selected set, but code tracing should exclude resource-only
  fonts/messages unless a later cross-reference makes them relevant.
- `AMIFM.EXE` (62,208 bytes, SHA-256
  `6a77d5f0d737319fbe7a886f08911eef24eaa116659dc278d604e76c61caea62`)
  contains a pointer-indexed table of native SAM section names and direct document validation
  messages, making it a useful structural corroborator.  `AMIPROUI.DLL` exports narrow table,
  frame, character-spacing, and indentation dialog procedures.  The filter pair remains the
  first target, followed by `AMIFM.EXE`, the relevant UI procedures, and only then the packed
  primary executable.
- `AMIPROUI.DLL` contains table, formula, protection, gutter, row/column, and frame-related
  user-interface strings, but proximity is not field evidence.  `AMIPRO.EXE` contains the
  core document identity and exports, while `AMIPRINT.EXE` is a separate background print
  module.  Module roles must be established from imports, exports, relocations, and callers,
  not filenames or UI strings alone.
- The existing converter deliberately leaves all table-definition fields and all `[data]`
  fields after row/column opaque.  It infers dimensions from the greatest observed cell
  coordinate, always marks inferred row zero as a header, and does not interpret `[h]` or
  `[w]`.  Renderers add equal widths, grids, padding, and header emphasis that are not source
  evidence.  These behaviors are hazards for an oracle comparison, not expected Ami Pro
  semantics.
- The private-corpus observations support only the four-field shape of `<:I...>` and the zero
  fourth field.  They do not establish the first three meanings.  Paragraph-region evidence
  also has an unresolved wording discrepancy: a five-twip aggregate observation band versus
  the current three-twip renderer tolerance.  The investigation must report those separately.
- Existing frame, inline-command, and style claims mix public documentation, corpus
  correlation, and inferred behavior.  The new ledger will identify provenance per claim so
  “confirmed” cannot silently mean different things.

## Evidence model and acceptance gates

Every proposed meaning receives an evidence packet with:

1. claim identifier, exact field/bit/command syntax, and current converter behavior;
2. module name, byte size, SHA-256, NE segment and offset, corresponding file offset when
   meaningful, and the tool/version that produced the observation;
3. a bounded byte/disassembly window and its relocation/import annotations;
4. a cross-reference path from a parser/serializer/formatter/file-I/O entry or call site to
   the field load, store, comparison, branch, or emitted token;
5. inferred behavior, competing explanations, negative evidence, and unresolved assumptions;
6. corroboration classified as one of: static executable, controlled Ami Pro oracle,
   aggregate private corpus, public primary documentation, or public reverse engineering,
   together with a dependency rationale for every source pair;
7. confidence of `confirmed`, `strong`, `tentative`, `contradicted`, or `open`.

An invented synthetic document is an experimental input, not evidence until Ami Pro produces
an observed output.  Current converter behavior is baseline context and can never corroborate
Ami Pro semantics.  `confirmed` requires either two genuinely independent sources with an
explicit dependency analysis or one controlled Ami Pro observation whose input and output
isolate the field.  Two nearby strings, two tools decoding the same bytes, multiple call sites
derived from one routine, or a public implementation derived from the same secondary reference
are not independent evidence.  Static control/data flow alone normally reaches `strong`, not
`confirmed`.

Confidence terms are operational:

- `confirmed`: meets the independent-source or controlled-observation gate above;
- `strong`: a complete reproducible parser/serializer-to-consumer static data-flow path, but
  no independent behavioral corroboration;
- `tentative`: partial static flow or discriminating corpus correlation with live competing
  explanations;
- `contradicted`: at least one reproducible observation falsifies the claim as stated;
- `open`: no discriminating evidence, or the search/decoding boundary prevented a conclusion.

No converter change enters the prioritized implementation list unless it is `confirmed`;
lower-confidence findings remain research hypotheses.

## Work plan

The reviewed-plan commit stages only this file.  Any pre-existing or concurrent plan,
converter, model, parser, renderer, fixture, or working-tree change remains outside this
research sequence and must not be staged into either research commit.

### 1. Establish a reproducible, non-proprietary input and tool manifest

- Add an explicit ignored research-cache path and a staging audit that fails if tracked or
  staged blobs or relevant nonignored untracked candidates look like vendor modules,
  extracted segments/resources, disk images, help files, filters, or private documents.  It
  must never traverse or read ignored `mydocs/`.  It will use explicit allowlists for the
  repository's licensed open fonts and invented text SAM fixture; extensions alone are not a
  safety verdict.  The audit will inspect paths and bounded content signatures without
  deleting anything.
- Build a bounded manifest command that inventories signature-confirmed NE modules and filters,
  recording names, sizes, SHA-256 values, detected container type, NE header summary, and
  explicit role evidence.  The inventory is stage one of the hash gate: it opens each regular
  non-symlink module once, hashes those bytes, and parses that same opened file descriptor,
  with before/after `fstat` checks.  It rejects more than 4,096 directory entries, names over
  255 bytes, files over 64 MiB, or more than 512 MiB total, and does not recurse below the
  supplied payload directory.
- Every later analyzer is stage two of the hash gate: it requires the generated manifest,
  refuses any unlisted module or digest/size mismatch, and analyzes the same opened bytes it
  rehashes.  The supplied `AMIPRO.EXE` digest is the initial trust anchor; the first inventory
  records observed digests for the other modules, after which no secondary module hash is
  optional.
- Record tool basename, version, executable digest where practical, exact probe arguments,
  availability, and observed behavior.  Absolute resolved tool paths belong only in an
  ephemeral run log, not deterministic committed output.  Distinguish a missing tool from a
  present tool that rejects NE.
- Generate `docs/research/module-manifest.json` from the volatile payload.  Hashes and
  metadata are durable; the modules are not.

The module-role portion of the ledger begins with this gate rather than filename inference:

| Candidate | Current inclusion evidence | Required next decision |
|---|---|---|
| `W4W33F.DLL` / `W4W33T.DLL` | SAM resource identity, directional exports, file-I/O imports, and direct section-token references | Trace native reader/writer fields first |
| `AMIFM.EXE` | Pointer-indexed native SAM section table and document validation | Use for structural traversal corroboration |
| `AMIPROUI.DLL` | Named table/frame/spacing/indent dialog exports and matching UI concepts | Trace control state into core calls; labels alone remain non-evidence |
| `AMIPRO.EXE` | Primary document core, native exports/resources, and callbacks | Defer code flow until packed-memory reconstruction is validated |
| `AMIPRINT.EXE` | Low-level read/seek imports and explicit background print-spool identity | Determine spool versus SAM input and trace only relevant formatting consumers |
| `AMIENV.DLL` | Paper-size exports only | Exclude unless a geometry call path reaches it |
| `AMIFONT.DLL` | Metrics, kerning, and microjustification exports | Include only for disputed spacing/measurement consumers |
| `AMILOTUS.DLL` | Output-window identity without native parser anchors | Exclude pending a contrary cross-reference |
| Other filters/support DLLs | Signature inventory only | Include only with export/import/token/xref evidence recorded in the ledger |

### 2. Implement and cross-check a validating NE indexer for ordinary filters

- Parse only the MZ fields needed to locate the NE header, then bounds-check the NE header,
  segment table, entry table, resident/nonresident names, module references, imported names,
  resources, and relocation records.  Reject overlaps, out-of-range counts/offsets, impossible
  shifts, truncated Pascal strings, and expansions above explicit per-segment and total caps.
- Normalize exports, imports by name/ordinal, segment flags, entry points, and internal versus
  imported relocations into deterministic JSON.  Cross-check invariant fields against
  `winedump`; disagreements become manifest warnings and block higher-confidence claims.
- Make the first code-analysis milestone the ordinary three-segment W4W filters.  Validate that
  each analyzed segment representation is direct/non-iterated before disassembly; otherwise
  stop at structural metadata.  Parse and annotate every relocation used by an evidence path
  rather than treating unresolved raw call operands as targets.
- Generate invented byte-level NE fixtures at test runtime for normal and malformed headers,
  names, entries, direct segments, iterated records, and relocation forms.  Do not commit
  binary fixture blobs, and never derive fixtures from Ami Pro bytes.

### 3. Add bounded search, cross-reference, and disassembly adapters

- Search decoded segment views for exact SAM section/command tokens, length-prefixed token
  tables, format fragments, import sites, and narrowly selected constants.  Report every hit
  as `segment:offset` plus mapped file provenance; never infer meaning from the hit alone.
- Use relocation records and 16-bit instruction decoding to identify references to candidate
  data and calls to file read/write/seek, parser, serializer, table, layout, and print paths.
  Keep code/data distinctions explicit and record ambiguous decodes.
- Wrap `ndisasm`, `cstool`, and GNU `objdump -b binary -m i8086` as independently formatted
  raw-byte decoders.  Decoder agreement does not validate the NE mapping or fixups.  Use
  temporary directories created mode `0700`, restrictive output permissions, automatic
  cleanup, subprocess timeouts, and isolated tool cache/config locations where supported.
  External analysis commands run without network access and may not write user-level project
  or cache state.
- Provide a command that emits one reviewable evidence-packet skeleton at a time, with hard
  built-in maxima that no command-line option can raise: 64 raw bytes, 32 decoded
  instructions, four strings of at most 96 characters each, cross-reference depth four, and
  fan-out 24 per packet; at most 48 committed packets, 3,072 raw bytes, and 1,536 decoded
  instructions across the ledger.  Bulk disassembly and proprietary resource extraction are
  out of scope.
- Evaluate resource tooling explicitly: record `winedump`'s metadata/resource behavior,
  whether installed `7z` recognizes NE structure, and the absence or behavior of `wrestool`,
  `rabin2`, `rz-bin`, and equivalents.  Resource-table output is limited to type/id,
  offset/size, and digest metadata; names and content are omitted by default, and raw resource
  extraction is disabled.
- Pin exact source revisions and paths for the Ghidra, radare2, and—if assessed—Rizin NE
  loaders.  Rizin may be reported as unavailable and unassessed rather than assigned a
  capability by analogy.  A modern decompiler remains a navigation aid, never primary
  evidence without byte/fixup verification.

### 4. Gate packed-primary reconstruction as optional follow-up

- Attempt `AMIPRO.EXE` iterated/self-loading reconstruction only if the ordinary filters and
  UI/core call boundaries cannot answer a priority question.  Decode only publicly specified
  and independently validated iterated-record forms into memory or `/tmp`, validate the result
  against allocation metadata, and preserve a logical-offset-to-source-record map.  If
  `SELFLOAD` adds another representation, report it as unsupported rather than guessing.
- Evaluate an optional pinned Ghidra headless run only through a custom loader/import step that
  presents validated expanded bytes and segmented addresses.  Compare its map with the local
  indexer and `winedump`; never use the built-in Ghidra or radare2 NE mapping as evidence for
  this image.  A custom loader is not an exit criterion.
- Never write expanded proprietary segments into the repository or a durable cache.  Any
  temporary expansion is mode-restricted, automatically removed, and represented durably only
  by a hash, mapping summary, and the globally capped evidence excerpts.

### 5. Trace the prioritized format questions

Proceed through `W4W33F/T`, `AMIFM`, relevant `AMIPROUI` procedures, and `AMIPRO.EXE` in that
order, stopping a branch when the cross-reference chain becomes speculative:

1. `[tbl]`, `[h]`, `[w]`, and `[data]`: declared row/column counts; row heights; column
   widths; horizontal/vertical gutters; table and cell alignment; row/column spans or merge
   topology; border sides/styles/colors; shading; formula source and cached value; text and
   protection flags; heading/repeat behavior; coordinate bases; and every remaining tail.
   Load and save paths are traced separately so defaulting, normalization, and lossy
   serialization are visible.
2. `<:I...>`: locate the four-field parser and every downstream use of all four destinations.
   Test competing indent/tab/list hypotheses against value ranges and formatter consumers;
   do not promote the trailing zero from “observed invariant” to “reserved” without a use or
   serializer rule.
3. `<:#x,width>` paragraph regions: trace coordinate conversion and containing-object lookup
   for page body, columns, table cells, frames, headers/footers, and overflowing cases.
   Separate source rounding behavior from the converter's safety tolerance.
4. `[frm]` and `[frmlay]`: verify header ordering, flags, coordinate origin, size fields,
   anchor selection, fixed-page versus repeating placement, header/footer variants, z-order,
   and any serializer normalization.  Do not infer background status from opacity or lack of
   an anchor.
5. Inline `:Z`, `:r`, `:O`, `:b`, `:R`, and noncanonical `:p` forms: distinguish an opener,
   closer, literal escape, state mutation, generated field, or container boundary using both
   parser and emitter paths.  Keep `</R>` separate from `<:R...>`.
6. `[algn]`, `[spc]`, and style flags: trace field storage into paragraph/formatter state,
   units and conversions, multi-bit precedence, all-indent/right-indent behavior, exact
   spacing, before/after spacing, tightness, baseline class markers, and unknown tails.
   Frequency in the corpus is never evidence that a bit is inert.

For any field that reaches print-only formatting state, trace the boundary into
`AMIPRINT.EXE`, `AMIFONT.DLL`, or another module only after proving the handoff format and
relocation/call path.  First establish whether `AMIPRINT.EXE` reads native SAM or a prepared
spool stream; do not classify spool fields as SAM fields by proximity.

For each branch, collect aggregate private-corpus distributions only when they discriminate
between concrete hypotheses.  Reports may contain counts, bounded numeric ranges, and
sanitized shape histograms, never filenames, prose, metadata, or source records.  Synthetic
one-feature SAM designs for a later emulator oracle will use invented text and values and will
be documented, but vendor execution is not required for this static-analysis phase.

A negative-search packet is reproducible only when it records module digests, exact decoded
segment/address ranges, searched encodings and patterns, tool versions, and every range/count
limit.  “Not found” without complete search scope is not evidence.

### 6. Assemble and adversarially review the deliverables

- Write `docs/research/executable-format-re.md` as the evidence ledger, including the tool
  limitations, module-role decision, evidence packets, negative findings, and confidence
  table.  Update the README's historical “no disassembly” statement without rewriting the
  dated earlier plan.
- Add a prioritized converter-change list containing only gated, field-specific changes and
  the synthetic tests each would require.  Put all other hypotheses in a separately ranked
  open-question list with the next discriminating experiment.
- Run unit tests for the research tooling, Ruff, deterministic reruns of the manifest and
  evidence generation, a fresh-payload hash check, and a repository/staging safety audit.
- Perform an adversarial review that tries to falsify each high-confidence packet by checking
  segment decoding, relocation interpretation, load/save asymmetry, signedness, units,
  structure packing, default-value branches, table-renderer inventions, and shared helper
  routines.  Downgrade any claim whose cross-reference path cannot be reproduced from the
  documented command line.
- Commit research scripts, invented fixtures/tests, manifest, and research documentation in
  a commit separate from this plan.  Leave `src/amipro_sam/` and converter tests unchanged.

## Tooling and repository constraints

- Use only Python's standard library for the committed structural indexer where practical.
  External tools are adapters discovered at runtime, not silently required dependencies.
- All counts, lengths, offsets, shifts, string sizes, decoded segment sizes, relocation
  counts, xref fan-out, recursion, and emitted evidence are capped before allocation or
  iteration.  Tool subprocesses receive timeouts and write temporary data only beneath a
  newly created mode-`0700` `/tmp` directory.
- Commands default to read-only operation.  They may use `winedump` as a file parser but never
  execute vendor code through `wine`, `winedbg`, DOSBox, or QEMU, and never launch Ami Pro,
  macros, OLE/DDE objects, filters, help viewers, installers, or printer drivers.
- Scripts do not accept a repository directory as a raw extraction destination.  Any future
  oracle cache must be separately designed, ignored, network-disabled, and populated from the
  user's owned media; it is not part of this phase.
- The manifest and ledger must not include absolute payload paths, private corpus filenames,
  source document text, vendor UI/help prose, or secrets.  Bounded instruction bytes are
  included only when necessary to reproduce a field-level claim.
- Deterministic regeneration means byte-identical output for the same manifest schema,
  selected payload, module digests, and toolchain.  A different available-tool set is a
  different recorded toolchain, not nondeterminism.

## Adversarial failure modes to test

- A tool may parse the NE header while mishandling iterated segment bodies, allocation size,
  self-loader metadata, or chained fixups.  Cross-tool agreement on header fields does not
  validate decoded code.
- A reference to `[tbl]` or a UI label may lead to dialog/macro/filter code rather than the
  native document reader or writer.  Require a path to file I/O or formatter state.
- Load code can validate or skip a field without revealing its semantics; save code can emit
  a default without showing how it is rendered.  Trace both directions and the consumer.
- Field arrays may be one-based internally while SAM coordinates are zero-based, or may mix
  byte, word, and signed values.  Require explicit conversion instructions and boundary
  behavior.
- One routine may serve table, column, frame, and page geometry.  A constant such as 1,440 or
  a multiply/divide by 20 is not a unit proof without the calling context.
- Corpus correlations can be dominated by application defaults.  Common zeroes, `1,100`,
  `0xc000`, and `0x10`/`0x20` must retain competing explanations until a consuming branch or
  controlled differential exists.
- The converter's current first-row header, table grid, equal widths, padding, frame reflow,
  and three-twip tolerance can contaminate visual comparisons.  Oracle expectations must be
  derived from Ami Pro output, not current renderer output.
- Decompiled C can invent types, merge variables, flatten far pointers, and obscure segment
  overrides.  The evidence packet must remain reproducible from raw bytes, NE fixups, and
  bounded 16-bit disassembly.
- A hash-only manifest can still drift if module selection or tool invocation changes.  Sort
  inputs, version the manifest schema, record selection rules, and make regenerated output
  byte-for-byte comparable.
- Safety checks based only on filename extensions can both miss renamed binaries and reject
  legitimate open fonts or invented SAM fixtures.  Validate staged blob signatures and use a
  narrow reviewed allowlist without opening ignored private-corpus paths.

## Exit criteria

This phase is complete when the expected primary hash and every analyzed module hash are
verified through the two-stage gate; module and tool manifests regenerate deterministically
for the recorded payload/toolchain; the structural indexer and ordinary-filter relocation
analysis are tested and cross-checked; each priority question has at least one reproducible
evidence packet or an explicit bounded negative-search/open result; all claims carry provenance
and confidence; the converter-change list contains only evidence-gated items; the safety audit
finds no tracked or staged proprietary/private artifact; research tests and lint pass; and the
research work is committed separately without modifying converter semantics.  Successful
packed `AMIPRO.EXE` reconstruction or a custom Ghidra loader is not required; an exact,
evidence-backed account of that boundary is an acceptable open result.
